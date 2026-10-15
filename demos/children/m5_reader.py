"""Run a later task in a separate process and report what the ledger recorded.

Process two of demonstration M5: it never learns anything from the parent except the
shared home, so any reuse it shows has to come from durable state.
"""

import json

from livium.agent import LiviumAgent
from livium.config import Config
from livium.ledger import Ledger

SCRIPT = [
    {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "m5.txt", "content": "one two three four\n", "purpose": "input"}}]},
    {"tool_calls": [{"name": "word_count", "arguments": {"path": "m5.txt"}}]},
    {"tool_calls": [{"name": "finish_task", "arguments": {
        "summary": "counted with the existing capability", "status": "complete"}}]},
]


def main() -> None:
    config = Config.load()
    config.ensure_dirs()
    agent = LiviumAgent(config, script=SCRIPT)
    agent.start()
    if not agent.guardian.integrity().ok:
        agent.guardian.record_baseline(approved_by="Demonstration runner")
    run = agent.run_task("count the words in m5.txt using what already exists",
                         [{"kind": "event", "event_type": "tool_call"}])
    row = Ledger(agent.store).rows(limit=1)[0]
    latest = agent.store.one(
        "SELECT memory_ids, experience_evidence FROM ledger ORDER BY id DESC LIMIT 1")
    print(json.dumps({
        "status": run.status,
        "verification": run.verification["state"],
        "capability_reused": bool(row.capability_reused),
        "new_development": bool(row.new_development),
        "experience_influenced": bool(row.experience_influenced),
        "memory_ids": json.loads(latest["memory_ids"]),
        "experience_evidence": json.loads(latest["experience_evidence"]),
        "tools_executed": [json.loads(r["payload"])["tool"] for r in agent.store.query(
            "SELECT payload FROM event WHERE type='tool_call' ORDER BY id")],
        "route_decisions": [(d["step"], d["decision"]) for d in run.route_decisions],
    }))


if __name__ == "__main__":
    main()
