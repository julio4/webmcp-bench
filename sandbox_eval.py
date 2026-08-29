"""Generic sandbox CLI; project behavior comes from an external adapter."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType


def now() -> str:
    return datetime.now(UTC).isoformat()


def load_adapter(path: Path) -> ModuleType:
    module_spec = importlib.util.spec_from_file_location(
        f"statelab_project_{abs(hash(path.resolve()))}", path
    )
    if module_spec is None or module_spec.loader is None:
        raise ValueError(f"Could not load project adapter: {path}")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    for name in ("resolve_state", "choose_tool", "invoke", "transition_checks"):
        if not callable(getattr(module, name, None)):
            raise ValueError(f"Project adapter must define {name}()")
    return module


def available_tools(spec: dict, state_id: str) -> list[str]:
    state = next((state for state in spec["states"] if state["id"] == state_id), None)
    return state.get("tools", []) if state else []


def evaluate(data: dict, task: dict, spec: dict, adapter: ModuleType) -> dict:
    before = json.loads(json.dumps(data))
    initial_state = adapter.resolve_state(data)
    available = available_tools(spec, initial_state)
    selected = adapter.choose_tool(task["prompt"], available)
    trace = []
    error = None

    if selected is None:
        error = "The scripted actor could not map the user goal to an available tool."
    else:
        started = now()
        try:
            result = adapter.invoke(selected, data, task.get("inputs", {}))
            trace.append(
                {
                    "tool": selected,
                    "input": task.get("inputs", {}),
                    "output": result,
                    "started_at": started,
                    "completed_at": now(),
                }
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            error = str(exc)
            trace.append(
                {
                    "tool": selected,
                    "input": task.get("inputs", {}),
                    "error": error,
                    "started_at": started,
                    "completed_at": now(),
                }
            )

    return {
        "actor": spec["versions"]["actor"],
        "initial_state": initial_state,
        "declared_tools": available,
        "selected_tool": selected,
        "tool_trace": trace,
        "adapter_events": [
            {"at": now(), "event": "declared_tools_loaded", "tools": available},
            {
                "at": now(),
                "event": "state_resolved",
                "state": adapter.resolve_state(data),
            },
        ],
        "before": before,
        "after": data,
        "observed_final_state": adapter.resolve_state(data),
        "agent_response": error or "Done.",
        "actor_error": error,
    }


def main() -> None:
    if len(sys.argv) != 6 or sys.argv[3] not in {"verify", "act"}:
        raise SystemExit(
            "usage: sandbox_eval.py ADAPTER.py PROJECT.json verify|act STATE.json ARG"
        )

    adapter_path, project_path, action, state_path, argument = sys.argv[1:]
    adapter = load_adapter(Path(adapter_path))
    spec = json.loads(Path(project_path).read_text())
    path = Path(state_path)
    data = json.loads(path.read_text())

    if action == "verify":
        actual = adapter.resolve_state(data)
        print(
            json.dumps(
                {
                    "expected_state": argument,
                    "observed_state": actual,
                    "available_tools": available_tools(spec, actual),
                    "passed": actual == argument,
                },
                separators=(",", ":"),
            )
        )
        return

    task = json.loads(Path(argument).read_text())
    evidence = evaluate(data, task, spec, adapter)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")))
    temporary.replace(path)
    print(json.dumps(evidence, separators=(",", ":")))


if __name__ == "__main__":
    main()
