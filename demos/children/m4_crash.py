"""Record a write-ahead intent and a file, then die before confirming the intent.

This is the durable state a crash leaves behind: the intent row says the operation
was started, the file may or may not be complete, and nothing has resolved it. Run
by ``demos/run_mission_demos.py`` (M4) in a separate process.
"""

import hashlib
import os
import signal

from livium.config import Config
from livium.store import Store

CONTENT = b"half written\n"
TASK_ID = "m4-task"


def main() -> None:
    config = Config.load()
    config.ensure_dirs()
    store = Store(config.paths.db)
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO task(id, objective, acceptance, status, created_at, "
            "updated_at) VALUES(?,?,?,'in_progress','now','now')",
            (TASK_ID, "write the interruption note", "[]"),
        )

    target = config.paths.workspace / "interrupted.md"
    digest = hashlib.sha256(CONTENT).hexdigest()
    intent_id = store.record_intent(
        "write_file", {"path": str(target), "bytes": len(CONTENT), "sha256": digest},
        task_id=TASK_ID, target=str(target),
        expected=f"{target} exists with sha256 {digest}",
    )
    with open(target, "wb") as handle:
        handle.write(CONTENT)
    print(f"intent {intent_id} recorded and the file landed; killing the process before the "
          f"intent could be confirmed", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    main()
