"""Capability: memory_recent -- enumerate recent memory without knowing a keyword.

Requirement (from the proposal): a returning agent must be able to ask "what did I
learn recently?" without already knowing which words appear in those records.
``memory_search`` requires a query and ranks by term match, so that question returns
nothing; no existing tool reaches the memory table.

The tool is read-only. It lists active records ordered by most recent update, with
the trust information (verification state, confidence, provenance) attached, because
a recent record is not automatically a true one.
"""

from __future__ import annotations

CAPABILITY = {
    "name": "memory_recent",
    "requirement": (
        "Enumerate recently stored memory without supplying keywords, so an agent "
        "starting a new session can re-apply what it learned in the last one."
    ),
    "description": "Lists the most recently updated memory records, newest first.",
    "tools": [
        {
            "name": "memory_recent",
            "description": (
                "List the most recently updated memory records, newest first. Use this at the "
                "start of a session (or when resuming work) to see what you learned before. "
                "Each row shows its kind, verification state and confidence: a recent record is "
                "not necessarily a true one. Prefer memory_search when you do know a keyword."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many records to return, 1-50. Default 10.",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["foundational", "capability", "experience", "decision",
                                     "failure", "task"],
                        },
                        "description": "Restrict to these memory kinds. Default: all kinds.",
                    },
                },
                "required": [],
            },
        }
    ],
}

MAX_LIMIT = 50


def handle(tool_name, args, ctx):
    from livium.tools import ToolResult

    if tool_name != "memory_recent":
        return ToolResult(tool=tool_name, ok=False, output=f"unknown tool {tool_name!r}",
                          error="unknown_tool")

    memory = (ctx.services or {}).get("memory") if ctx is not None else None
    if memory is None:
        return ToolResult(
            tool=tool_name, ok=False,
            output="no memory service is available on this run, so recency cannot be queried",
            error="no_memory_service",
        )

    raw_limit = args.get("limit", None)
    if raw_limit is None or raw_limit == "":
        raw_limit = 10
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return ToolResult(tool=tool_name, ok=False, output="limit must be an integer",
                          error="bad_arguments")
    # `x or default` would silently turn an explicit 0 into the default; clamping is
    # the documented behaviour, so 0 becomes 1 and very large values are capped.
    limit = max(1, min(limit, MAX_LIMIT))

    kinds = args.get("kinds") or None
    if kinds is not None and not isinstance(kinds, list):
        return ToolResult(tool=tool_name, ok=False, output="kinds must be a list of strings",
                          error="bad_arguments")

    records = memory.recent(kinds=kinds, limit=limit)

    if not records:
        return ToolResult(
            tool=tool_name, ok=True,
            output="No memory records have been stored yet. That is an empty store, not an "
                   "error: nothing has been learned here so far.",
            data={"records": [], "count": 0},
        )

    lines = [f"{len(records)} most recent memory record(s), newest first:"]
    for record in records:
        lines.append(
            f"  [mem:{record.id}] {record.kind}"
            + (f"/{record.key}" if record.key else "")
            + f" | updated={record.updated_at} | verification={record.verification}"
            + f" | confidence={record.confidence:.2f} | status={record.status}"
        )
        lines.append(f"      {record.content}")
        if record.evidence:
            lines.append(f"      evidence: {record.evidence[:160]}")
    return ToolResult(
        tool=tool_name, ok=True, output="\n".join(lines),
        data={
            "count": len(records),
            "records": [
                {"id": r.id, "kind": r.kind, "key": r.key, "status": r.status,
                 "verification": r.verification, "confidence": r.confidence,
                 "updated_at": r.updated_at, "content": r.content}
                for r in records
            ],
        },
    )
