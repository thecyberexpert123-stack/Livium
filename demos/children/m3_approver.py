"""Decide a pending approval as a named human: the human half of demonstration M3.

The agent has no route to this code path. It runs as a separate process, with the
approver's name supplied on the command line, exactly as `livium approvals` does.
"""

import json
import sys

from livium.approvals import ApprovalQueue
from livium.config import Config
from livium.store import Store


def main() -> None:
    approver = sys.argv[1]
    config = Config.load()
    config.ensure_dirs()
    queue = ApprovalQueue(Store(config.paths.db))
    pending = queue.pending()
    if not pending:
        print(json.dumps({"decided": False, "why": "nothing is waiting for a decision"}))
        return
    decided = queue.decide(pending[0].id, approved=True, by=approver, note="demonstration")
    print(json.dumps({"decided": True, "id": decided.id, "status": decided.status,
                      "by": decided.decided_by}))


if __name__ == "__main__":
    main()
