"""Reading-list behavior used by the local site and sandbox evaluator."""

ARTICLE = {"id": "article-42", "title": "State Machines for Agents"}


def resolve_state(data: dict) -> str:
    items = data.get("items", [])
    if not items:
        return "empty"
    if len(items) != 1:
        return "unresolved"
    item = items[0]
    if item.get("status") == "read" and item.get("priority") is False:
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
        # ("done", "mark_read")
    ):
        if marker in text and tool in available:
            return tool
    return None


def invoke(tool: str, data: dict, inputs: dict) -> dict:
    if not isinstance(inputs, dict):
        raise ValueError("tool input must be an object")
    if tool == "save_article":
        if data.get("items"):
            raise ValueError("reading list is not empty")
        if inputs.get("article_id", ARTICLE["id"]) != ARTICLE["id"]:
            raise ValueError("unknown article_id")
        data["items"] = [{**ARTICLE, "status": "saved", "priority": False}]
    elif tool == "prioritize_article":
        if resolve_state(data) != "saved":
            raise ValueError("article is not in the saved state")
        data["items"][0]["priority"] = True
    elif tool == "remove_article":
        if resolve_state(data) != "saved":
            raise ValueError("article is not removable in the current state")
        data["items"] = []
    elif tool == "mark_read":
        if resolve_state(data) not in {"saved", "prioritized"}:
            raise ValueError("article is not unread")
        data["items"][0]["status"] = "read"
        data["items"][0]["priority"] = False
    else:
        raise ValueError(f"unknown tool: {tool}")
    return {"ok": True, "resolved_state": resolve_state(data)}


def transition_checks(initial: dict, final: dict, transition: dict) -> list[dict]:
    item_ids = [item.get("id") for item in final.get("items", [])]
    checks = [
        {
            "id": "user-preserved",
            "label": "Active user is preserved",
            "passed": final.get("user_id") == initial.get("user_id"),
            "observed": final.get("user_id"),
        },
        {
            "id": "no-duplicates",
            "label": "No duplicate article IDs",
            "passed": len(item_ids) == len(set(item_ids)),
            "observed": item_ids,
        },
    ]
    if transition["tool"] != "remove_article" and initial.get("items"):
        checks.append(
            {
                "id": "identity-preserved",
                "label": "Article identity and title are preserved",
                "passed": bool(final.get("items"))
                and {key: final["items"][0].get(key) for key in ("id", "title")}
                == {key: initial["items"][0].get(key) for key in ("id", "title")},
                "observed": {
                    key: final.get("items", [{}])[0].get(key) for key in ("id", "title")
                },
            }
        )
    item = final.get("items", [{}])[0] if final.get("items") else {}
    expected_effect = {
        "save_article": item == {**ARTICLE, "status": "saved", "priority": False},
        "prioritize_article": item.get("status") == "saved"
        and item.get("priority") is True,
        "remove_article": final.get("items") == [],
        "mark_read": item.get("status") == "read" and item.get("priority") is False,
    }[transition["tool"]]
    checks.append(
        {
            "id": "expected-effect",
            "label": transition["expected_effects"][0],
            "passed": expected_effect,
            "observed": item or final.get("items"),
        }
    )
    return checks
