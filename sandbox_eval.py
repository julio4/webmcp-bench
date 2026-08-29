"""Tiny deterministic actor and reference website state used inside Daytona."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


TOOLS_BY_STATE = {
    "empty": ["save_article"],
    "saved": ["prioritize_article", "remove_article", "mark_read"],
    "prioritized": ["mark_read"],
    "read": [],
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def resolve_state(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return "empty"
    if len(items) != 1:
        return "unresolved"
    item = items[0]
    if item.get("status") == "read":
        return "read"
    if item.get("status") == "saved" and item.get("priority") is True:
        return "prioritized"
    if item.get("status") == "saved" and item.get("priority") is False:
        return "saved"
    return "unresolved"


def choose_tool(prompt: str, available: list[str]) -> str | None:
    text = prompt.lower()
    for marker, tool in (
        ("priorit", "prioritize_article"),
        ("remove", "remove_article"),
        ("mark", "mark_read"),
        ("read", "mark_read"),
        ("save", "save_article"),
    ):
        if marker in text and tool in available:
            return tool
    return None


def invoke(tool: str, data: dict, inputs: dict) -> dict:
    if tool == "save_article":
        if data["items"]:
            raise ValueError("reading list is not empty")
        data["items"] = [{**inputs["article"], "status": "saved", "priority": False}]
    elif tool == "prioritize_article":
        data["items"][0]["priority"] = True
    elif tool == "remove_article":
        data["items"] = []
    elif tool == "mark_read":
        data["items"][0]["status"] = "read"
        data["items"][0]["priority"] = False
    else:
        raise ValueError(f"unknown tool: {tool}")
    return {"ok": True, "resolved_state": resolve_state(data)}


def evaluate(data: dict, task: dict) -> dict:
    before = json.loads(json.dumps(data))
    initial_state = resolve_state(data)
    available = TOOLS_BY_STATE.get(initial_state, [])
    selected = choose_tool(task["prompt"], available)
    trace = []
    error = None

    if selected is None:
        error = "The scripted actor could not map the user goal to an available tool."
    else:
        started = now()
        try:
            result = invoke(selected, data, task.get("inputs", {}))
            trace.append(
                {
                    "tool": selected,
                    "input": task.get("inputs", {}),
                    "output": result,
                    "started_at": started,
                    "completed_at": now(),
                }
            )
        except (KeyError, IndexError, ValueError) as exc:
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
        "actor": "deterministic-keyword-v1",
        "initial_state": initial_state,
        "discovered_tools": available,
        "selected_tool": selected,
        "tool_trace": trace,
        "page_events": [
            {"at": now(), "event": "tools_discovered", "tools": available},
            {"at": now(), "event": "state_rendered", "state": resolve_state(data)},
        ],
        "before": before,
        "after": data,
        "observed_final_state": resolve_state(data),
        "agent_response": error or "Done.",
        "actor_error": error,
    }


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in {"verify", "act"}:
        raise SystemExit("usage: sandbox_eval.py verify|act STATE.json ARG")

    action, state_path, argument = sys.argv[1:]
    path = Path(state_path)
    data = json.loads(path.read_text())

    if action == "verify":
        actual = resolve_state(data)
        print(
            json.dumps(
                {
                    "expected_state": argument,
                    "observed_state": actual,
                    "available_tools": TOOLS_BY_STATE.get(actual, []),
                    "passed": actual == argument,
                },
                separators=(",", ":"),
            )
        )
        return

    task = json.loads(Path(argument).read_text())
    evidence = evaluate(data, task)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")))
    temporary.replace(path)
    print(json.dumps(evidence, separators=(",", ":")))


if __name__ == "__main__":
    main()
