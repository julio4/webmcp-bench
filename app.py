"""WebMCP-Bench: run a trusted local project in Daytona and inspect it locally."""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import threading
import uuid
import webbrowser
from datetime import UTC, datetime
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote, urlparse

from sandbox_eval import available_tools, evaluate, load_adapter


ROOT = Path(__file__).resolve().parent
LOCK = threading.RLock()
RUNS: dict[str, dict] = {}
ACTIVE_RUN_ID: str | None = None
PROJECT_DIR: Path
RUNS_DIR: Path
SPEC: dict
ADAPTER: ModuleType
# ponytail: one shared local demo state; add sessions only if this becomes multi-user.
DEMO_STATE: dict


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]


def project_artifacts() -> dict[str, dict[str, str]]:
    artifacts = {}
    for relative in ("project.json", "adapter.py", "site/index.html"):
        content = (PROJECT_DIR / relative).read_text()
        artifacts[relative] = {
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
        }
    return artifacts


def default_project() -> Path:
    projects = sorted((ROOT / "examples").glob("*/project.json"))
    if not projects:
        raise ValueError("No project supplied and no example project was found.")
    return projects[0].parent


def configure_project(path: Path) -> None:
    global PROJECT_DIR, RUNS_DIR, SPEC, ADAPTER, DEMO_STATE, ACTIVE_RUN_ID
    project_dir = path.resolve()
    project_file = project_dir / "project.json"
    adapter_file = project_dir / "adapter.py"
    site_file = project_dir / "site" / "index.html"
    missing = [str(item) for item in (project_file, adapter_file, site_file) if not item.is_file()]
    if missing:
        raise ValueError("Project is missing: " + ", ".join(missing))

    spec = json.loads(project_file.read_text())
    adapter = load_adapter(adapter_file)
    errors = validate_spec(spec, adapter)
    if errors:
        raise ValueError("Invalid project: " + "; ".join(errors))
    entry = next((state for state in spec["states"] if state.get("kind") == "entry"), spec["states"][0])
    PROJECT_DIR = project_dir
    SPEC = spec
    ADAPTER = adapter
    RUNS_DIR = ROOT / "runs" / SPEC["id"]
    DEMO_STATE = copy.deepcopy(entry["fixture"]["data"])
    with LOCK:
        RUNS.clear()
        ACTIVE_RUN_ID = None


def state_map(spec: dict | None = None) -> dict[str, dict]:
    selected = spec if spec is not None else SPEC
    return {state["id"]: state for state in selected.get("states", [])}


def transition_map(spec: dict | None = None) -> dict[str, dict]:
    selected = spec if spec is not None else SPEC
    return {
        transition["id"]: transition
        for transition in selected.get("transitions", [])
    }


def active_tasks(spec: dict | None = None) -> list[dict]:
    selected = spec if spec is not None else SPEC
    return [task for task in selected.get("tasks", []) if task.get("status") == "active"]


def tasks_for_transition(transition_id: str, spec: dict | None = None) -> list[dict]:
    return [
        task
        for task in active_tasks(spec)
        if task.get("transition_id") == transition_id
    ]


def validate_spec(spec: dict, adapter: ModuleType | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["Project specification must be a JSON object."]
    for key in ("id", "name", "version", "description", "versions", "tools", "states", "transitions", "tasks"):
        if key not in spec:
            errors.append(f"Missing top-level field: {key}.")
    if errors:
        return errors

    for key in ("id", "name", "version", "description"):
        if not isinstance(spec[key], str) or not spec[key]:
            errors.append(f"Top-level field {key} must be a non-empty string.")
    if not isinstance(spec["versions"], dict):
        errors.append("Top-level field versions must be an object.")
    else:
        for key in ("dataset", "website", "actor", "verifier"):
            if not isinstance(spec["versions"].get(key), str) or not spec["versions"].get(key):
                errors.append(f"Missing project version: {key}.")
    for key in ("tools", "states", "transitions", "tasks"):
        if not isinstance(spec[key], list):
            errors.append(f"Top-level field {key} must be an array.")
    if errors:
        return errors
    if not spec["states"]:
        errors.append("Project must declare at least one state.")

    collections = {
        "state": spec["states"],
        "tool": spec["tools"],
        "transition": spec["transitions"],
        "task": spec["tasks"],
    }
    required = {
        "state": ("id", "name", "kind", "x", "y", "description", "invariants", "tools", "fixture"),
        "tool": ("id", "title", "description", "mutates", "input_schema"),
        "transition": ("id", "source", "target", "tool", "intent", "expected_effects", "forbidden_effects", "postconditions"),
        "task": ("id", "version", "status", "transition_id", "prompt", "inputs", "expected_outcome"),
    }
    for label, items in collections.items():
        if any(not isinstance(item, dict) for item in items):
            errors.append(f"Every {label} must be an object.")
            continue
        for index, item in enumerate(items):
            for field in required[label]:
                if field not in item:
                    errors.append(f"{label.title()} at index {index} is missing {field}.")
    if errors:
        return errors
    for label, items in collections.items():
        if any(not isinstance(item["id"], str) or not item["id"] for item in items):
            errors.append(f"Every {label} id must be a non-empty string.")
    if errors:
        return errors

    for tool in spec["tools"]:
        if any(not isinstance(tool[key], str) or not tool[key] for key in ("id", "title", "description")):
            errors.append(f"Tool {tool['id']} requires string id, title, and description.")
        if not isinstance(tool["input_schema"], dict) or not isinstance(tool["mutates"], bool):
            errors.append(f"Tool {tool['id']} has an invalid schema or mutates flag.")
    for state in spec["states"]:
        fixture = state["fixture"]
        if any(not isinstance(state[key], str) or not state[key] for key in ("id", "name", "kind", "description")):
            errors.append(f"State {state['id']} requires string id, name, kind, and description.")
        if not isinstance(state["tools"], list) or not isinstance(state["invariants"], list):
            errors.append(f"State {state['id']} tools and invariants must be arrays.")
        if not isinstance(state["x"], (int, float)) or not isinstance(state["y"], (int, float)):
            errors.append(f"State {state['id']} coordinates must be numbers.")
        if not isinstance(fixture, dict) or any(key not in fixture for key in ("id", "version", "data")):
            errors.append(f"State {state['id']} has an invalid fixture.")
        elif any(not isinstance(fixture[key], str) or not fixture[key] for key in ("id", "version")) or not isinstance(fixture["data"], dict):
            errors.append(f"State {state['id']} fixture requires id, version, and object data.")
    for transition in spec["transitions"]:
        if any(not isinstance(transition[key], str) or not transition[key] for key in ("id", "source", "target", "tool", "intent")):
            errors.append(f"Transition {transition['id']} requires string ids, tool, and intent.")
        if any(not isinstance(transition[key], list) for key in ("expected_effects", "forbidden_effects", "postconditions")):
            errors.append(f"Transition {transition['id']} effects and postconditions must be arrays.")
    for task in spec["tasks"]:
        if any(not isinstance(task[key], str) or not task[key] for key in ("id", "version", "status", "transition_id", "prompt")) or not isinstance(task["inputs"], dict):
            errors.append(f"Task {task['id']} requires version, prompt, and object inputs.")
    if errors:
        return errors

    for label, items in collections.items():
        ids = [item.get("id") for item in items]
        errors.extend(f"Duplicate {label} id: {item_id}." for item_id in sorted({item_id for item_id in ids if item_id and ids.count(item_id) > 1}))
        if any(not item_id for item_id in ids):
            errors.append(f"A {label} is missing its id.")

    states = state_map(spec)
    tools = {tool["id"] for tool in spec["tools"]}
    transitions = transition_map(spec)
    for state in spec["states"]:
        if not state.get("fixture", {}).get("id"):
            errors.append(f"State {state['id']} has no fixture.")
        for tool in state.get("tools", []):
            if tool not in tools:
                errors.append(f"State {state['id']} references missing tool {tool}.")
        try:
            resolved = (adapter or ADAPTER).resolve_state(copy.deepcopy(state["fixture"]["data"]))
            if resolved != state["id"]:
                errors.append(f"Fixture for {state['id']} resolves to {resolved}.")
        except Exception as exc:
            errors.append(f"Fixture for {state['id']} cannot be resolved: {exc}")

    for transition in spec["transitions"]:
        source, target, tool = transition.get("source"), transition.get("target"), transition.get("tool")
        if source not in states:
            errors.append(f"Transition {transition['id']} has missing source {source}.")
        if target not in states:
            errors.append(f"Transition {transition['id']} has missing target {target}.")
        if tool not in tools:
            errors.append(f"Transition {transition['id']} references missing tool {tool}.")
        elif source in states and tool not in states[source].get("tools", []):
            errors.append(f"Transition {transition['id']} uses unavailable tool {tool} in {source}.")
        if not tasks_for_transition(transition["id"], spec):
            errors.append(f"Transition {transition['id']} has no active task.")

    for task in spec["tasks"]:
        if task.get("transition_id") not in transitions:
            errors.append(f"Task {task['id']} references missing transition {task.get('transition_id')}.")
        if task.get("expected_outcome") not in {"passed", "failed"}:
            errors.append(f"Task {task['id']} has invalid expected_outcome.")
    return errors


def env_value(name: str) -> str | None:
    if value := os.environ.get(name):
        return value
    path = ROOT / ".env"
    if not path.exists():
        return None
    for raw in path.read_text().splitlines():
        key, separator, value = raw.partition("=")
        if separator and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def error_text(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for name in ("DAYTONA_API_KEY", "DAYTONA_API"):
        if secret := env_value(name):
            message = message.replace(secret, "[redacted]")
    return message


def jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if dataclasses.is_dataclass(value):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def sandbox_observation(sandbox) -> dict:
    fields = (
        "id", "name", "state", "desired_state", "target", "cpu", "memory", "disk",
        "created_at", "updated_at", "last_activity_at", "auto_destroy_at", "daemon_version", "labels",
    )
    return {field: jsonable(getattr(sandbox, field, None)) for field in fields}


def transition_checks(initial: dict, final: dict, transition: dict) -> list[dict]:
    checks = [
        {
            "id": "target-state",
            "label": f"Final state is {transition['target']}",
            "passed": ADAPTER.resolve_state(final) == transition["target"],
            "observed": ADAPTER.resolve_state(final),
        }
    ]
    project_checks = ADAPTER.transition_checks(initial, final, transition)
    if not isinstance(project_checks, list):
        raise TypeError("Project adapter transition_checks() must return a list")
    return checks + project_checks


def verify_attempt(initial: dict, final: dict, transition: dict, actor: dict) -> tuple[str, str, str, list[dict]]:
    checks = transition_checks(initial, final, transition)
    if all(check["passed"] for check in checks):
        return "passed", "none", "All authoritative-state checks passed.", checks
    if actor.get("selected_tool") is None:
        category, reason = "wrong_or_unavailable_tool", actor.get("actor_error") or "No tool was selected."
    elif actor.get("actor_error"):
        category, reason = "tool_invocation_error", actor["actor_error"]
    elif next(check for check in checks if check["id"] == "target-state")["passed"]:
        category = "postcondition_or_invariant_violated"
        reason = "Failed checks: " + ", ".join(check["label"] for check in checks if not check["passed"])
    else:
        category = "expected_target_not_reached"
        reason = f"Expected {transition['target']}; observed {ADAPTER.resolve_state(final)}."
    return "failed", category, reason, checks


def command_evidence(sandbox, stage: str, command: str) -> tuple[dict, object]:
    started = now()
    response = sandbox.process.exec(command, timeout=60)
    return {
        "stage": stage,
        "command": command,
        "started_at": started,
        "completed_at": now(),
        "exit_code": response.exit_code,
        "stdout": response.result,
    }, response


def add_event(attempt: dict, stage: str, detail: str) -> None:
    attempt["stage"] = stage
    attempt["lifecycle"].append({"stage": stage, "at": now(), "detail": detail})


def record_command(attempt: dict, sandbox, stage: str, command: str) -> object:
    record, response = command_evidence(sandbox, stage, command)
    attempt["commands"].append(record)
    if response.exit_code != 0:
        raise RuntimeError(f"{stage} exited {response.exit_code}: {response.result}")
    return response


def run_attempt(daytona, run: dict, transition: dict, task: dict) -> dict:
    from daytona import CreateSandboxFromSnapshotParams

    state = state_map()[transition["source"]]
    initial = copy.deepcopy(state["fixture"]["data"])
    versions = SPEC["versions"]
    attempt = {
        "id": f"attempt-{uuid.uuid4().hex[:10]}",
        "run_id": run["id"],
        "task_id": task["id"],
        "transition_id": transition["id"],
        "source_state": transition["source"],
        "expected_target_state": transition["target"],
        "prompt": task["prompt"],
        "expected_outcome": task["expected_outcome"],
        "outcome": "running",
        "stage": "queued",
        "lifecycle": [],
        "commands": [],
        "sandbox": {"provider": "daytona", "cleanup": "pending"},
        "versions": {
            "project": SPEC["id"], "spec": SPEC["version"], "spec_digest": digest(SPEC),
            "project_bundle_digest": run["project_bundle_digest"],
            "fixture": state["fixture"]["version"], "task": task["version"],
            "website": versions["website"], "actor": versions["actor"], "verifier": versions["verifier"],
        },
    }
    sandbox = None
    add_event(attempt, "queued", "Attempt created.")
    run["attempts"].append(attempt)
    checkpoint(run)

    try:
        add_event(attempt, "provisioning", "Creating a fresh Daytona sandbox.")
        checkpoint(run)
        sandbox = daytona.create(
            CreateSandboxFromSnapshotParams(
                language="python", ttl_minutes=10,
                labels={"app": "webmcp-bench", "project": SPEC["id"], "run": run["id"], "attempt": attempt["id"]},
            ),
            timeout=90,
        )
        sandbox.refresh_data()
        attempt["sandbox"]["created"] = sandbox_observation(sandbox)

        add_event(attempt, "fixture_setup", "Uploading the selected project, website, fixture, and generic runner.")
        checkpoint(run)
        record_command(attempt, sandbox, "fixture_setup", "mkdir -p /tmp/statelab/project/site")
        uploads = {
            ROOT / "app.py": "/tmp/statelab/app.py",
            ROOT / "index.html": "/tmp/statelab/index.html",
            ROOT / "sandbox_eval.py": "/tmp/statelab/sandbox_eval.py",
        }
        for source, destination in uploads.items():
            sandbox.fs.upload_file(source.read_bytes(), destination)
        for relative, artifact in run["project_artifacts"].items():
            sandbox.fs.upload_file(artifact["content"].encode(), f"/tmp/statelab/project/{relative}")
        sandbox.fs.upload_file(json.dumps(initial).encode(), "/tmp/statelab/state.json")
        sandbox.fs.upload_file(json.dumps(task).encode(), "/tmp/statelab/task.json")

        add_event(attempt, "website_start", "Starting the selected project's real demo website in the sandbox.")
        checkpoint(run)
        record_command(
            attempt, sandbox, "website_start",
            "cd /tmp/statelab && python3 app.py project --host 0.0.0.0 --port 8080 --no-open >/tmp/statelab/site.log 2>&1 & echo $!",
        )
        add_event(attempt, "website_smoke", "Checking the project server and WebMCP registration code over HTTP.")
        checkpoint(run)
        record_command(
            attempt, sandbox, "website_smoke",
            "sleep 1 && python3 -c \"import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8080/health')); p=urllib.request.urlopen('http://127.0.0.1:8080/demo/').read().decode(); ok=h.get('ok') is True and 'document.modelContext.registerTool' in p; print(json.dumps({'health':h,'webmcp_page':ok})); assert ok\"",
        )

        add_event(attempt, "initial_state_verification", "Resolving the fixture inside the sandbox before actor execution.")
        checkpoint(run)
        response = record_command(
            attempt, sandbox, "initial_state_verification",
            f"python3 /tmp/statelab/sandbox_eval.py /tmp/statelab/project/adapter.py /tmp/statelab/project/project.json verify /tmp/statelab/state.json {transition['source']}",
        )
        initial_evidence = json.loads(response.result.strip().splitlines()[-1])
        attempt["initial_verification"] = initial_evidence
        if not initial_evidence["passed"]:
            raise RuntimeError(f"Fixture resolved to {initial_evidence['observed_state']}, expected {transition['source']}")

        add_event(attempt, "agent_execution", "Running the scripted goal-oriented actor.")
        checkpoint(run)
        response = record_command(
            attempt, sandbox, "agent_execution",
            "python3 /tmp/statelab/sandbox_eval.py /tmp/statelab/project/adapter.py /tmp/statelab/project/project.json act /tmp/statelab/state.json /tmp/statelab/task.json",
        )
        actor = json.loads(response.result.strip().splitlines()[-1])
        attempt["actor"] = actor

        add_event(attempt, "final_state_verification", "Downloading authoritative state for host-side verification.")
        checkpoint(run)
        final_bytes = sandbox.fs.download_file("/tmp/statelab/state.json")
        if final_bytes is None:
            raise RuntimeError("Final authoritative state artifact was unavailable.")
        final = json.loads(final_bytes)
        outcome, category, reason, checks = verify_attempt(initial, final, transition, actor)
        attempt.update(
            {
                "observed_initial_state": ADAPTER.resolve_state(initial),
                "observed_final_state": ADAPTER.resolve_state(final),
                "authoritative_state": {"before": initial, "after": final},
                "checks": checks, "outcome": outcome, "failure_category": category, "reason": reason,
            }
        )

        add_event(attempt, "evidence_collection", "Collecting sandbox metadata and latest resource metrics.")
        try:
            sandbox.refresh_data()
            attempt["sandbox"]["observed"] = sandbox_observation(sandbox)
            attempt["sandbox"]["metrics"] = jsonable(sandbox.get_metrics_latest())
        except Exception as exc:  # Metrics are diagnostic; they do not change the semantic verdict.
            attempt["sandbox"]["metrics_error"] = error_text(exc)
        add_event(attempt, "completed" if outcome == "passed" else "failed", reason)
    except Exception as exc:
        attempt.update({"outcome": "infrastructure_error", "failure_category": "sandbox_or_fixture_error", "reason": error_text(exc)})
        add_event(attempt, "errored", attempt["reason"])
    finally:
        if sandbox is not None:
            try:
                daytona.delete(sandbox, timeout=90, wait=True)
                attempt["sandbox"]["cleanup"] = "deleted"
                attempt["lifecycle"].append({"stage": "sandbox_cleanup", "at": now(), "detail": "Daytona sandbox deleted."})
            except Exception as exc:
                attempt["sandbox"].update({"cleanup": "failed", "cleanup_error": error_text(exc)})
                attempt.update(
                    {
                        "semantic_outcome": attempt["outcome"], "outcome": "infrastructure_error",
                        "failure_category": "sandbox_cleanup_failure", "stage": "errored",
                        "reason": "Evaluation finished, but the Daytona sandbox could not be deleted.",
                    }
                )
                attempt["lifecycle"].append({"stage": "sandbox_cleanup_error", "at": now(), "detail": attempt["sandbox"]["cleanup_error"]})
        else:
            attempt["sandbox"]["cleanup"] = "not_created"
        attempt["completed_at"] = now()
        checkpoint(run)
    return attempt


def coverage() -> dict:
    covered = {task["transition_id"] for task in active_tasks()}
    declared = len(SPEC["transitions"])
    return {
        "declared": declared,
        "with_active_task": len(covered),
        "percent": round(100 * len(covered) / declared) if declared else 0,
    }


def new_run() -> dict:
    task_count = len(active_tasks())
    artifacts = project_artifacts()
    return {
        "id": f"run-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "project_id": SPEC["id"], "status": "running", "started_at": now(), "completed_at": None,
        "spec_version": SPEC["version"], "dataset_version": SPEC["versions"]["dataset"],
        "website_version": SPEC["versions"]["website"], "agent": SPEC["versions"]["actor"],
        "sandbox_provider": "daytona", "spec_snapshot": copy.deepcopy(SPEC),
        "project_artifacts": artifacts, "project_bundle_digest": digest(artifacts), "coverage": coverage(),
        "counts": {"queued": task_count, "running": 0, "passed": 0, "failed": 0, "infrastructure_error": 0},
        "attempts": [],
    }


def refresh_counts(run: dict) -> None:
    outcomes = [attempt["outcome"] for attempt in run["attempts"]]
    run["counts"] = {
        "queued": len(active_tasks()) - len(outcomes), "running": outcomes.count("running"),
        "passed": outcomes.count("passed"), "failed": outcomes.count("failed"),
        "infrastructure_error": outcomes.count("infrastructure_error"),
    }


def checkpoint(run: dict) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    refresh_counts(run)
    encoded = json.dumps(run, indent=2)
    temporary, destination = RUNS_DIR / f".{run['id']}.tmp", RUNS_DIR / f"{run['id']}.json"
    with LOCK:
        temporary.write_text(encoded)
        temporary.replace(destination)
        RUNS[run["id"]] = json.loads(encoded)


def execute_run(run: dict) -> dict:
    global ACTIVE_RUN_ID
    try:
        errors = validate_spec(SPEC)
        if errors:
            raise ValueError("Invalid project: " + "; ".join(errors))
        api_key = env_value("DAYTONA_API_KEY") or env_value("DAYTONA_API")
        if not api_key:
            raise RuntimeError("Set DAYTONA_API (or DAYTONA_API_KEY) in .env.")
        from daytona import Daytona, DaytonaConfig

        daytona = Daytona(DaytonaConfig(api_key=api_key))
        transitions = transition_map()
        # ponytail: sequential attempts keep v1 observable; add a worker pool when suite latency matters.
        for task in active_tasks():
            run_attempt(daytona, run, transitions[task["transition_id"]], task)
        run["status"] = "completed"
    except Exception as exc:
        run.update({"status": "errored", "error": error_text(exc)})
    finally:
        run["completed_at"] = now()
        checkpoint(run)
        with LOCK:
            ACTIVE_RUN_ID = None
    return run


def start_run(background: bool = True) -> dict:
    global ACTIVE_RUN_ID
    with LOCK:
        if ACTIVE_RUN_ID:
            raise RuntimeError(f"Run {ACTIVE_RUN_ID} is already active.")
        run = new_run()
        ACTIVE_RUN_ID = run["id"]
    checkpoint(run)
    if background:
        threading.Thread(target=execute_run, args=(run,)).start()
    else:
        execute_run(run)
    return run


def load_runs() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK:
        RUNS.clear()
        for path in RUNS_DIR.glob("run-*.json"):
            try:
                run = json.loads(path.read_text())
                if run.get("project_id") == SPEC["id"]:
                    if run.get("status") == "running":
                        completed = now()
                        for attempt in run.get("attempts", []):
                            if attempt.get("outcome") == "running":
                                attempt.update(
                                    {
                                        "outcome": "infrastructure_error", "stage": "errored",
                                        "failure_category": "evaluator_interrupted",
                                        "reason": "The local evaluator stopped before this attempt completed.",
                                        "completed_at": completed,
                                    }
                                )
                        run.update(
                            {
                                "status": "errored", "completed_at": completed,
                                "error": "The local evaluator stopped before this run completed.",
                            }
                        )
                        refresh_counts(run)
                        path.write_text(json.dumps(run, indent=2))
                    RUNS[run["id"]] = run
            except (OSError, json.JSONDecodeError, KeyError):
                continue


def demo_snapshot() -> dict:
    with LOCK:
        state = copy.deepcopy(DEMO_STATE)
    resolved = ADAPTER.resolve_state(state)
    return {"state": state, "resolved_state": resolved, "tools": available_tools(SPEC, resolved)}


def reset_demo_state(state_id: str) -> dict:
    global DEMO_STATE
    state = state_map().get(state_id)
    if state is None:
        raise ValueError(f"Unknown state: {state_id}")
    with LOCK:
        DEMO_STATE = copy.deepcopy(state["fixture"]["data"])
    return demo_snapshot()


def invoke_demo_tool(tool: str, inputs: dict) -> dict:
    global DEMO_STATE
    with LOCK:
        resolved = ADAPTER.resolve_state(DEMO_STATE)
        if tool not in available_tools(SPEC, resolved):
            raise RuntimeError(f"Tool {tool} is unavailable in state {resolved}.")
        state = copy.deepcopy(DEMO_STATE)
        result = ADAPTER.invoke(tool, state, inputs)
        DEMO_STATE = state
    final = ADAPTER.resolve_state(state)
    return {"result": jsonable(result), "state": state, "resolved_state": final, "tools": available_tools(SPEC, final)}


def self_test() -> None:
    assert validate_spec(SPEC) == []
    broken = copy.deepcopy(SPEC)
    broken["states"].append(copy.deepcopy(broken["states"][0]))
    assert any("Duplicate state id" in error for error in validate_spec(broken))
    for path in (("versions", "actor"), ("tasks", 0, "prompt"), ("states", 0, "fixture", "version")):
        broken = copy.deepcopy(SPEC)
        target = broken
        for key in path[:-1]:
            target = target[key]
        target.pop(path[-1])
        assert validate_spec(broken), path
    for collection, field, value in (("transitions", "source", {}), ("tasks", "transition_id", [])):
        broken = copy.deepcopy(SPEC)
        broken[collection][0][field] = value
        assert validate_spec(broken), (collection, field)
    for artifact in project_artifacts().values():
        assert hashlib.sha256(artifact["content"].encode()).hexdigest() == artifact["sha256"]
    transitions, states = transition_map(), state_map()
    for task in active_tasks():
        transition = transitions[task["transition_id"]]
        initial = copy.deepcopy(states[transition["source"]]["fixture"]["data"])
        final = copy.deepcopy(initial)
        actor = evaluate(final, task, SPEC, ADAPTER)
        outcome, _, _, _ = verify_attempt(initial, final, transition, actor)
        assert outcome == task["expected_outcome"], task["id"]
    first_task = active_tasks()[0]
    first_transition = transitions[first_task["transition_id"]]
    reset_demo_state(first_transition["source"])
    assert invoke_demo_tool(first_transition["tool"], first_task.get("inputs", {}))["resolved_state"] == first_transition["target"]
    print(f"self-test passed: {SPEC['id']} ({len(SPEC['states'])} states, {len(SPEC['transitions'])} transitions, {len(active_tasks())} tasks)")


def demo_gate(run: dict) -> list[str]:
    attempts, errors = run["attempts"], []
    if run["status"] != "completed":
        errors.append(f"Run status is {run['status']}.")
    if digest(run.get("spec_snapshot")) != digest(SPEC):
        errors.append("The run does not retain the exact project specification snapshot.")
    if run.get("project_artifacts") != project_artifacts():
        errors.append("The run does not retain the exact executed project files.")
    tasks = {task["id"]: task for task in active_tasks()}
    attempt_task_ids = [attempt.get("task_id") for attempt in attempts]
    if len(attempt_task_ids) != len(tasks) or set(attempt_task_ids) != set(tasks):
        errors.append("The run does not cover every active task exactly once.")
    if {attempt.get("transition_id") for attempt in attempts} != set(transition_map()):
        errors.append("The run does not cover every declared transition.")
    if any(attempt.get("outcome") != tasks.get(attempt.get("task_id"), {}).get("expected_outcome") for attempt in attempts):
        errors.append("At least one attempt differs from its declared demonstration outcome.")
    sandbox_ids = [attempt.get("sandbox", {}).get("created", {}).get("id") for attempt in attempts]
    if None in sandbox_ids or len(set(sandbox_ids)) != len(attempts):
        errors.append("Attempts do not have unique Daytona sandbox IDs.")
    if any(attempt.get("sandbox", {}).get("cleanup") != "deleted" for attempt in attempts):
        errors.append("At least one Daytona sandbox was not deleted.")
    if any(not isinstance(attempt.get("sandbox", {}).get("metrics"), dict) for attempt in attempts):
        errors.append("At least one attempt lacks structured Daytona metrics.")
    if any(not any(command.get("stage") == "website_smoke" and command.get("exit_code") == 0 for command in attempt.get("commands", [])) for attempt in attempts):
        errors.append("At least one attempt did not start and smoke the project website.")
    return errors


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, path: Path) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.send_header("Permissions-Policy", "tools=(self)")
        self.send_header("Origin-Agent-Cluster", "?1")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if not 0 <= length <= 65536:
            raise ValueError("Invalid JSON body size")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        port = self.server.server_port
        return not origin or origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self.send_html(ROOT / "index.html")
        elif path == "/demo":
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", "/demo/")
            self.end_headers()
        elif path == "/demo/":
            self.send_html(PROJECT_DIR / "site" / "index.html")
        elif path == "/api/state":
            with LOCK:
                runs = sorted(RUNS.values(), key=lambda run: run["started_at"], reverse=True)
                active = ACTIVE_RUN_ID
            self.send_json({"spec": SPEC, "validation_errors": validate_spec(SPEC), "active_run_id": active, "runs": runs})
        elif path == "/api/project":
            self.send_json(SPEC)
        elif path == "/api/demo/state":
            self.send_json(demo_snapshot())
        elif path == "/health":
            self.send_json({"ok": True, "project": SPEC["id"]})
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if not self.origin_allowed():
            self.send_json({"error": "origin not allowed"}, HTTPStatus.FORBIDDEN)
            return
        try:
            if path == "/api/runs":
                run = start_run(background=True)
                self.send_json({"run": {"id": run["id"]}}, HTTPStatus.ACCEPTED)
            elif path == "/api/demo/reset":
                body = self.read_json()
                self.send_json(reset_demo_state(str(body.get("state", ""))))
            elif path.startswith("/api/demo/tools/"):
                tool = path.removeprefix("/api/demo/tools/")
                self.send_json(invoke_demo_tool(tool, self.read_json()))
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")


def serve(host: str, port: int, open_dashboard: bool) -> None:
    errors = validate_spec(SPEC)
    if errors:
        raise ValueError("Invalid project: " + "; ".join(errors))
    load_runs()
    server = ThreadingHTTPServer((host, port), Handler)
    dashboard_url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Dashboard: {dashboard_url}")
    print(f"Demo website: {dashboard_url}demo/")
    if open_dashboard:
        threading.Timer(0.2, webbrowser.open, args=(dashboard_url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", nargs="?", type=Path, help="Trusted project bundle directory (defaults to the first example).")
    parser.add_argument("--run", "--demo", dest="run_once", action="store_true", help="Run the complete Daytona suite once and exit.")
    parser.add_argument("--self-test", action="store_true", help="Run the local no-network project check and exit.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the dashboard in a browser.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    configure_project(args.project or default_project())
    if args.self_test:
        self_test()
    elif args.run_once:
        load_runs()
        run = start_run(background=False)
        gate_errors = demo_gate(run)
        print(json.dumps({"run_id": run["id"], "project": SPEC["id"], "status": run["status"], "counts": run["counts"], "demo_gate": "passed" if not gate_errors else gate_errors}, indent=2))
        raise SystemExit(bool(gate_errors))
    else:
        serve(args.host, args.port, not args.no_open)


if __name__ == "__main__":
    main()
