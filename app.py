"""WebMCP StateLab MVP: Daytona-backed evaluations and a local dashboard."""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import threading
import uuid
from datetime import UTC, datetime
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from sandbox_eval import TOOLS_BY_STATE, evaluate, resolve_state


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
LOCK = threading.RLock()
RUNS: dict[str, dict] = {}
ACTIVE_RUN_ID: str | None = None

ARTICLE = {"id": "article-42", "title": "State Machines for Agents"}
SPEC = {
    "id": "reading-list",
    "name": "Agent Reading List",
    "version": "spec-1",
    "description": "A cooperative four-state reading-list flow for the Daytona MVP.",
    "tools": [
        {"id": "save_article", "description": "Save one article for later.", "mutates": True},
        {"id": "prioritize_article", "description": "Make the saved article the next priority.", "mutates": True},
        {"id": "remove_article", "description": "Remove the saved article.", "mutates": True},
        {"id": "mark_read", "description": "Mark the article as read.", "mutates": True},
    ],
    "states": [
        {
            "id": "empty", "name": "Empty", "kind": "entry", "x": 90, "y": 210,
            "description": "No article is saved.",
            "invariants": ["The active user is demo-user.", "No duplicate article IDs exist."],
            "tools": ["save_article"],
            "fixture": {"id": "fixture-empty", "version": "1", "data": {"user_id": "demo-user", "items": []}},
        },
        {
            "id": "saved", "name": "Saved", "kind": "normal", "x": 310, "y": 210,
            "description": "One unread article is saved without priority.",
            "invariants": ["Exactly one article is saved.", "The active user is demo-user."],
            "tools": ["prioritize_article", "remove_article", "mark_read"],
            "fixture": {"id": "fixture-saved", "version": "1", "data": {"user_id": "demo-user", "items": [{**ARTICLE, "status": "saved", "priority": False}]}},
        },
        {
            "id": "prioritized", "name": "Prioritized", "kind": "normal", "x": 530, "y": 90,
            "description": "The unread article is the next priority.",
            "invariants": ["Exactly one article is saved.", "The active user is demo-user."],
            "tools": ["mark_read"],
            "fixture": {"id": "fixture-prioritized", "version": "1", "data": {"user_id": "demo-user", "items": [{**ARTICLE, "status": "saved", "priority": True}]}},
        },
        {
            "id": "read", "name": "Read", "kind": "terminal", "x": 750, "y": 210,
            "description": "The article has been read.",
            "invariants": ["The article identity is preserved.", "The active user is demo-user."],
            "tools": [],
            "fixture": {"id": "fixture-read", "version": "1", "data": {"user_id": "demo-user", "items": [{**ARTICLE, "status": "read", "priority": False}]}},
        },
    ],
    "transitions": [
        {
            "id": "save", "source": "empty", "target": "saved", "tool": "save_article",
            "intent": "Save article", "expected_effects": ["The article is saved once."],
            "forbidden_effects": ["Do not change the active user.", "Do not create duplicates."],
            "postconditions": ["State resolves to saved."],
            "task": {"id": "task-save", "version": "1", "status": "active", "prompt": "Save ‘State Machines for Agents’ to my reading list.", "inputs": {"article": ARTICLE}, "expected_outcome": "passed"},
        },
        {
            "id": "prioritize", "source": "saved", "target": "prioritized", "tool": "prioritize_article",
            "intent": "Prioritize article", "expected_effects": ["The article priority becomes true."],
            "forbidden_effects": ["Do not add or remove articles."], "postconditions": ["State resolves to prioritized."],
            "task": {"id": "task-prioritize", "version": "1", "status": "active", "prompt": "Make my saved article the next priority.", "inputs": {}, "expected_outcome": "passed"},
        },
        {
            "id": "remove", "source": "saved", "target": "empty", "tool": "remove_article",
            "intent": "Remove article", "expected_effects": ["The reading list becomes empty."],
            "forbidden_effects": ["Do not change the active user."], "postconditions": ["State resolves to empty."],
            "task": {"id": "task-remove", "version": "1", "status": "active", "prompt": "Remove the saved article from my list.", "inputs": {}, "expected_outcome": "passed"},
        },
        {
            "id": "finish-saved", "source": "saved", "target": "read", "tool": "mark_read",
            "intent": "Finish saved article", "expected_effects": ["The article status becomes read."],
            "forbidden_effects": ["Do not remove the article."], "postconditions": ["State resolves to read."],
            "task": {"id": "task-finish-ambiguous", "version": "1", "status": "active", "prompt": "I’m done with that article.", "inputs": {}, "expected_outcome": "failed", "note": "Intentional baseline failure: the scripted actor does not understand ‘done’."},
        },
        {
            "id": "finish-priority", "source": "prioritized", "target": "read", "tool": "mark_read",
            "intent": "Finish priority article", "expected_effects": ["The article status becomes read and priority clears."],
            "forbidden_effects": ["Do not remove the article."], "postconditions": ["State resolves to read."],
            "task": {"id": "task-finish-priority", "version": "1", "status": "active", "prompt": "Mark the priority article as read.", "inputs": {}, "expected_outcome": "passed"},
        },
    ],
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def validate_spec(spec: dict) -> list[str]:
    errors: list[str] = []
    states = [state["id"] for state in spec.get("states", [])]
    tools = [tool["id"] for tool in spec.get("tools", [])]
    transitions = [transition["id"] for transition in spec.get("transitions", [])]
    for label, values in (("state", states), ("tool", tools), ("transition", transitions)):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        errors.extend(f"Duplicate {label} id: {value}" for value in duplicates)
    state_map = {state["id"]: state for state in spec.get("states", [])}
    for state in spec.get("states", []):
        if not state.get("fixture", {}).get("id"):
            errors.append(f"State {state['id']} has no fixture.")
        for tool in state.get("tools", []):
            if tool not in tools:
                errors.append(f"State {state['id']} references missing tool {tool}.")
        if state.get("tools", []) != TOOLS_BY_STATE.get(state["id"]):
            errors.append(f"State {state['id']} tool surface differs from the sandbox website.")
    for transition in spec.get("transitions", []):
        source = transition.get("source")
        target = transition.get("target")
        tool = transition.get("tool")
        if source not in state_map:
            errors.append(f"Transition {transition['id']} has missing source {source}.")
        if target not in state_map:
            errors.append(f"Transition {transition['id']} has missing target {target}.")
        if tool not in tools:
            errors.append(f"Transition {transition['id']} references missing tool {tool}.")
        elif source in state_map and tool not in state_map[source].get("tools", []):
            errors.append(f"Transition {transition['id']} uses unavailable tool {tool} in {source}.")
        if transition.get("task", {}).get("status") != "active":
            errors.append(f"Transition {transition['id']} has no active task.")
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
    item_ids = [item.get("id") for item in final.get("items", [])]
    checks = [
        {"id": "target-state", "label": f"Final state is {transition['target']}", "passed": resolve_state(final) == transition["target"], "observed": resolve_state(final)},
        {"id": "user-preserved", "label": "Active user is preserved", "passed": final.get("user_id") == initial.get("user_id"), "observed": final.get("user_id")},
        {"id": "no-duplicates", "label": "No duplicate article IDs", "passed": len(item_ids) == len(set(item_ids)), "observed": item_ids},
    ]
    if transition["tool"] != "remove_article" and initial.get("items"):
        checks.append({"id": "identity-preserved", "label": "Article identity is preserved", "passed": bool(final.get("items")) and final["items"][0].get("id") == initial["items"][0].get("id"), "observed": final.get("items", [])})
    item = final.get("items", [{}])[0] if final.get("items") else {}
    expected_effect = {
        "save_article": item == {**ARTICLE, "status": "saved", "priority": False},
        "prioritize_article": item.get("status") == "saved" and item.get("priority") is True,
        "remove_article": final.get("items") == [],
        "mark_read": item.get("status") == "read" and item.get("priority") is False,
    }[transition["tool"]]
    checks.append({"id": "expected-effect", "label": transition["expected_effects"][0], "passed": expected_effect, "observed": item or final.get("items")})
    return checks


def verify_attempt(initial: dict, final: dict, transition: dict, actor: dict) -> tuple[str, str, str, list[dict]]:
    checks = transition_checks(initial, final, transition)
    if all(check["passed"] for check in checks):
        return "passed", "none", "All authoritative-state checks passed.", checks
    if actor.get("selected_tool") is None:
        category = "wrong_or_unavailable_tool"
        reason = actor.get("actor_error") or "No tool was selected."
    elif actor.get("actor_error"):
        category = "tool_invocation_error"
        reason = actor["actor_error"]
    elif next(check for check in checks if check["id"] == "target-state")["passed"]:
        category = "postcondition_or_invariant_violated"
        reason = "Failed checks: " + ", ".join(check["label"] for check in checks if not check["passed"])
    else:
        category = "expected_target_not_reached"
        reason = f"Expected {transition['target']}; observed {resolve_state(final)}."
    return "failed", category, reason, checks


def command_evidence(sandbox, stage: str, command: str) -> tuple[dict, object]:
    started = now()
    response = sandbox.process.exec(command, timeout=60)
    record = {
        "stage": stage,
        "command": command,
        "started_at": started,
        "completed_at": now(),
        "exit_code": response.exit_code,
        "stdout": response.result,
    }
    return record, response


def add_event(attempt: dict, stage: str, detail: str) -> None:
    attempt["stage"] = stage
    attempt["lifecycle"].append({"stage": stage, "at": now(), "detail": detail})


def run_attempt(daytona, run: dict, transition: dict) -> dict:
    from daytona import CreateSandboxFromSnapshotParams

    state = next(state for state in SPEC["states"] if state["id"] == transition["source"])
    task = transition["task"]
    initial = copy.deepcopy(state["fixture"]["data"])
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
            "spec": SPEC["version"], "spec_digest": digest(SPEC), "fixture": state["fixture"]["version"],
            "task": task["version"], "website": "reference-reading-list-1", "actor": "deterministic-keyword-v1",
            "verifier": "authoritative-state-verifier-2",
        },
    }
    sandbox = None
    add_event(attempt, "queued", "Attempt created.")
    run["attempts"].append(attempt)
    checkpoint(run)

    try:
        add_event(attempt, "provisioning", "Creating a fresh Daytona sandbox.")
        checkpoint(run)
        sandbox = daytona.create(CreateSandboxFromSnapshotParams(
            language="python", ttl_minutes=10,
            labels={"app": "webmcp-statelab", "run": run["id"], "attempt": attempt["id"]},
        ), timeout=90)
        sandbox.refresh_data()
        attempt["sandbox"]["created"] = sandbox_observation(sandbox)

        add_event(attempt, "fixture_setup", "Uploading the deterministic fixture and actor.")
        checkpoint(run)
        sandbox.fs.upload_file((ROOT / "sandbox_eval.py").read_bytes(), "/tmp/sandbox_eval.py")
        sandbox.fs.upload_file(json.dumps(initial).encode(), "/tmp/state.json")
        sandbox.fs.upload_file(json.dumps(task).encode(), "/tmp/task.json")

        add_event(attempt, "initial_state_verification", "Resolving state inside the sandbox before actor execution.")
        checkpoint(run)
        command, response = command_evidence(sandbox, "initial_state_verification", f"python3 /tmp/sandbox_eval.py verify /tmp/state.json {transition['source']}")
        attempt["commands"].append(command)
        if response.exit_code != 0:
            raise RuntimeError(f"Initial verifier exited {response.exit_code}: {response.result}")
        initial_evidence = json.loads(response.result.strip().splitlines()[-1])
        attempt["initial_verification"] = initial_evidence
        if not initial_evidence["passed"]:
            raise RuntimeError(f"Fixture resolved to {initial_evidence['observed_state']}, expected {transition['source']}")

        add_event(attempt, "agent_execution", "Running the scripted goal-oriented actor.")
        checkpoint(run)
        command, response = command_evidence(sandbox, "agent_execution", "python3 /tmp/sandbox_eval.py act /tmp/state.json /tmp/task.json")
        attempt["commands"].append(command)
        if response.exit_code != 0:
            raise RuntimeError(f"Actor exited {response.exit_code}: {response.result}")
        actor = json.loads(response.result.strip().splitlines()[-1])
        attempt["actor"] = actor

        add_event(attempt, "final_state_verification", "Downloading authoritative state for host-side verification.")
        checkpoint(run)
        final_bytes = sandbox.fs.download_file("/tmp/state.json")
        if final_bytes is None:
            raise RuntimeError("Final authoritative state artifact was unavailable.")
        final = json.loads(final_bytes)
        outcome, category, reason, checks = verify_attempt(initial, final, transition, actor)
        attempt.update({
            "observed_initial_state": resolve_state(initial),
            "observed_final_state": resolve_state(final),
            "authoritative_state": {"before": initial, "after": final},
            "checks": checks,
            "outcome": outcome,
            "failure_category": category,
            "reason": reason,
        })

        add_event(attempt, "evidence_collection", "Collecting sandbox metadata and latest resource metrics.")
        try:
            sandbox.refresh_data()
            attempt["sandbox"]["observed"] = sandbox_observation(sandbox)
            attempt["sandbox"]["metrics"] = jsonable(sandbox.get_metrics_latest())
        except Exception as exc:  # Metrics are diagnostic; they do not change the semantic verdict.
            attempt["sandbox"]["metrics_error"] = error_text(exc)
        add_event(attempt, "completed" if outcome == "passed" else "failed", reason)
    except Exception as exc:
        attempt.update({
            "outcome": "infrastructure_error",
            "failure_category": "sandbox_or_fixture_error",
            "reason": error_text(exc),
        })
        add_event(attempt, "errored", attempt["reason"])
    finally:
        if sandbox is not None:
            try:
                daytona.delete(sandbox, timeout=90, wait=True)
                attempt["sandbox"]["cleanup"] = "deleted"
                attempt["lifecycle"].append({"stage": "sandbox_cleanup", "at": now(), "detail": "Daytona sandbox deleted."})
            except Exception as exc:
                attempt["sandbox"]["cleanup"] = "failed"
                attempt["sandbox"]["cleanup_error"] = error_text(exc)
                attempt["semantic_outcome"] = attempt["outcome"]
                attempt["outcome"] = "infrastructure_error"
                attempt["failure_category"] = "sandbox_cleanup_failure"
                attempt["reason"] = "Evaluation finished, but the Daytona sandbox could not be deleted."
                attempt["stage"] = "errored"
                attempt["lifecycle"].append({"stage": "sandbox_cleanup_error", "at": now(), "detail": attempt["sandbox"]["cleanup_error"]})
        else:
            attempt["sandbox"]["cleanup"] = "not_created"
        attempt["completed_at"] = now()
        checkpoint(run)
    return attempt


def new_run() -> dict:
    transition_count = len(SPEC["transitions"])
    return {
        "id": f"run-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "status": "running",
        "started_at": now(),
        "completed_at": None,
        "spec_version": SPEC["version"],
        "dataset_version": "dataset-1",
        "website_version": "reference-reading-list-1",
        "agent": "deterministic-keyword-v1",
        "sandbox_provider": "daytona",
        "spec_snapshot": copy.deepcopy(SPEC),
        "coverage": {"declared": transition_count, "with_active_task": transition_count, "percent": 100},
        "counts": {"queued": transition_count, "running": 0, "passed": 0, "failed": 0, "infrastructure_error": 0},
        "attempts": [],
    }


def refresh_counts(run: dict) -> None:
    outcomes = [attempt["outcome"] for attempt in run["attempts"]]
    run["counts"] = {
        "queued": len(SPEC["transitions"]) - len(outcomes),
        "running": outcomes.count("running"),
        "passed": outcomes.count("passed"),
        "failed": outcomes.count("failed"),
        "infrastructure_error": outcomes.count("infrastructure_error"),
    }


def checkpoint(run: dict) -> None:
    RUNS_DIR.mkdir(exist_ok=True)
    refresh_counts(run)
    encoded = json.dumps(run, indent=2)
    temporary = RUNS_DIR / f".{run['id']}.tmp"
    destination = RUNS_DIR / f"{run['id']}.json"
    with LOCK:
        temporary.write_text(encoded)
        temporary.replace(destination)
        RUNS[run["id"]] = json.loads(encoded)


def execute_run(run: dict) -> dict:
    global ACTIVE_RUN_ID
    try:
        errors = validate_spec(SPEC)
        if errors:
            raise ValueError("Invalid specification: " + "; ".join(errors))
        api_key = env_value("DAYTONA_API_KEY") or env_value("DAYTONA_API")
        if not api_key:
            raise RuntimeError("Set DAYTONA_API (or DAYTONA_API_KEY) in .env.")
        from daytona import Daytona, DaytonaConfig
        daytona = Daytona(DaytonaConfig(api_key=api_key))
        # ponytail: sequential attempts keep v1 observable; add a worker pool when suite latency matters.
        for transition in SPEC["transitions"]:
            run_attempt(daytona, run, transition)
        run["status"] = "completed"
    except Exception as exc:
        run["status"] = "errored"
        run["error"] = error_text(exc)
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
    RUNS_DIR.mkdir(exist_ok=True)
    for path in RUNS_DIR.glob("run-*.json"):
        try:
            run = json.loads(path.read_text())
            RUNS[run["id"]] = run
        except (OSError, json.JSONDecodeError, KeyError):
            continue


def self_test() -> None:
    assert validate_spec(SPEC) == []
    broken = copy.deepcopy(SPEC)
    broken["states"].append(copy.deepcopy(broken["states"][0]))
    assert any("Duplicate state id" in error for error in validate_spec(broken))
    state_map = {state["id"]: state for state in SPEC["states"]}
    for transition in SPEC["transitions"]:
        initial = copy.deepcopy(state_map[transition["source"]]["fixture"]["data"])
        final = copy.deepcopy(initial)
        actor = evaluate(final, transition["task"])
        outcome, _, _, _ = verify_attempt(initial, final, transition, actor)
        assert outcome == transition["task"]["expected_outcome"], transition["id"]
    initial = copy.deepcopy(state_map["empty"]["fixture"]["data"])
    wrong_article = {"user_id": "demo-user", "items": [{"id": "wrong", "title": "Wrong", "status": "saved", "priority": False}]}
    outcome, category, _, _ = verify_attempt(initial, wrong_article, SPEC["transitions"][0], {"selected_tool": "save_article"})
    assert (outcome, category) == ("failed", "postcondition_or_invariant_violated")
    print("self-test passed: valid 4-state/5-transition spec and deterministic verdicts")


def demo_gate(run: dict) -> list[str]:
    attempts = run["attempts"]
    errors = []
    if run["status"] != "completed":
        errors.append(f"Run status is {run['status']}.")
    if digest(run.get("spec_snapshot")) != digest(SPEC):
        errors.append("The run does not retain the exact current specification snapshot.")
    if len(attempts) != len(SPEC["transitions"]):
        errors.append(f"Expected {len(SPEC['transitions'])} attempts; found {len(attempts)}.")
    sandbox_ids = [attempt.get("sandbox", {}).get("created", {}).get("id") for attempt in attempts]
    if None in sandbox_ids or len(set(sandbox_ids)) != len(attempts):
        errors.append("Attempts do not have unique Daytona sandbox IDs.")
    if any(attempt.get("sandbox", {}).get("cleanup") != "deleted" for attempt in attempts):
        errors.append("At least one Daytona sandbox was not deleted.")
    outcomes = [attempt.get("outcome") for attempt in attempts]
    if "passed" not in outcomes or "failed" not in outcomes:
        errors.append("The demonstration must contain both a pass and an intentional failure.")
    if "infrastructure_error" in outcomes:
        errors.append("The run contains an infrastructure error.")
    expected = {transition["id"]: transition["task"]["expected_outcome"] for transition in SPEC["transitions"]}
    if {attempt.get("transition_id") for attempt in attempts} != set(expected):
        errors.append("The run does not cover every declared transition exactly once.")
    if any(attempt.get("outcome") != expected.get(attempt.get("transition_id")) for attempt in attempts):
        errors.append("At least one attempt differs from its declared demonstration outcome.")
    if any("expected-effect" not in {check.get("id") for check in attempt.get("checks", [])} for attempt in attempts):
        errors.append("At least one attempt lacks the transition-specific effect check.")
    if any(attempt.get("versions", {}).get("verifier") != "authoritative-state-verifier-2" for attempt in attempts):
        errors.append("At least one attempt lacks the current verifier version.")
    if any(not isinstance(attempt.get("sandbox", {}).get("metrics"), dict) for attempt in attempts):
        errors.append("At least one attempt lacks structured Daytona resource metrics.")
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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            with LOCK:
                runs = sorted(RUNS.values(), key=lambda run: run["started_at"], reverse=True)
                active = ACTIVE_RUN_ID
            self.send_json({"spec": SPEC, "validation_errors": validate_spec(SPEC), "active_run_id": active, "runs": runs})
        elif path == "/health":
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/runs":
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.send_json({"error": "origin not allowed"}, HTTPStatus.FORBIDDEN)
            return
        try:
            run = start_run(background=True)
            self.send_json({"run": {"id": run["id"]}}, HTTPStatus.ACCEPTED)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")


def serve(port: int) -> None:
    load_runs()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"WebMCP StateLab: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Run the complete suite once and exit.")
    parser.add_argument("--self-test", action="store_true", help="Run the local no-network check and exit.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.self_test:
        self_test()
    elif args.demo:
        load_runs()
        run = start_run(background=False)
        gate_errors = demo_gate(run)
        print(json.dumps({"run_id": run["id"], "status": run["status"], "counts": run["counts"], "demo_gate": "passed" if not gate_errors else gate_errors}, indent=2))
        raise SystemExit(bool(gate_errors))
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
