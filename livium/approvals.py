"""Human authorization for consequential operations.

The permission engine already distinguishes three outcomes: allow, deny, and
``approval_required``. Denial is final — the deny-list and the shell rule cannot be
argued with from inside the process. Approval is the middle case: an operation that is
not forbidden, but that a human should authorise for *this* task, with *these*
arguments, once.

This module is the other half of that outcome. When a tool call comes back
``approval_required``:

* the tool layer records a request here and refuses to execute;
* the run stops in the honest state ``awaiting_approval`` rather than pretending to
  have failed or to have succeeded;
* a human moves the request to ``approved`` or ``rejected`` from the CLI (there is no
  HTTP route and no agent tool for it — that is the point);
* a later run of the same task may consume an approval, which marks it used so a
  single decision cannot authorise a second execution.

An approval matches on (task, tool, exact arguments). Broadening it would make
"approved" mean "approved for something like this", which is not a control a human can
reason about.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

from .permissions import UNATTRIBUTED_NAMES
from .store import Store, now

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
CONSUMED = "consumed"


def canonical(arguments: Any) -> str:
    return json.dumps(arguments or {}, sort_keys=True, default=str, separators=(",", ":"))


def arguments_digest(arguments: Any) -> str:
    return hashlib.sha256(canonical(arguments).encode("utf-8")).hexdigest()[:32]


@dataclasses.dataclass
class Approval:
    id: int
    task_id: str | None
    tool: str
    arguments: str
    reason: str
    status: str
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    note: str | None = None

    def render(self) -> str:
        decision = ""
        if self.decided_at:
            decision = f" by {self.decided_by or 'unknown'} at {self.decided_at}"
            if self.note:
                decision += f" ({self.note})"
        return (f"#{self.id} [{self.status}{decision}] task={self.task_id or '-'} "
                f"tool={self.tool} args={self.arguments[:160]} — {self.reason}")

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class ApprovalQueue:
    def __init__(self, store: Store):
        self.store = store

    # -- agent side --------------------------------------------------------
    def request(self, *, task_id: str | None, tool: str, arguments: Any,
                reason: str) -> Approval:
        """Record a request. Idempotent per (task, tool, arguments) while pending."""

        payload = canonical(arguments)
        existing = self.store.one(
            "SELECT * FROM approval WHERE task_id IS ? AND tool=? AND arguments=? "
            "AND status='pending' ORDER BY id DESC LIMIT 1",
            (task_id, tool, payload),
        )
        if existing is not None:
            return self._row(existing)
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO approval(task_id, tool, arguments, reason, status, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (task_id, tool, payload, reason, PENDING, now()),
            )
            approval_id = int(cur.lastrowid or 0)
        self.store.append_event(
            "approval_requested",
            {"approval_id": approval_id, "tool": tool, "arguments": payload, "reason": reason},
            task_id=task_id,
        )
        return self.get(approval_id)

    def consume(self, *, task_id: str | None, tool: str, arguments: Any) -> Approval | None:
        """Take an approved request for exactly this operation, if one exists.

        One approval authorises one execution: the row moves to ``consumed`` so it
        cannot silently cover a second call with the same arguments.
        """

        payload = canonical(arguments)
        row = self.store.one(
            "SELECT * FROM approval WHERE task_id IS ? AND tool=? AND arguments=? "
            "AND status='approved' ORDER BY id LIMIT 1",
            (task_id, tool, payload),
        )
        if row is None:
            return None
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE approval SET status=?, decided_at=? WHERE id=? AND status='approved'",
                (CONSUMED, now(), row["id"]),
            )
        self.store.append_event(
            "approval_consumed",
            {"approval_id": row["id"], "tool": tool, "approved_by": row["decided_by"]},
            task_id=task_id,
        )
        return self.get(int(row["id"]))

    def matching_status(self, *, task_id: str | None, tool: str, arguments: Any) -> str | None:
        payload = canonical(arguments)
        row = self.store.one(
            "SELECT status FROM approval WHERE task_id IS ? AND tool=? AND arguments=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, tool, payload),
        )
        return str(row["status"]) if row is not None else None

    # -- human side --------------------------------------------------------
    def decide(self, approval_id: int, *, approved: bool, by: str, note: str = "") -> Approval:
        if not by or not by.strip():
            raise ValueError("a decision must be attributed to a named human")
        if by.strip().lower() in UNATTRIBUTED_NAMES:
            raise ValueError(
                f"'{by.strip()}' is not a person: a decision must name the human who "
                f"made it, so that approving something later costs someone a name"
            )
        current = self.get(approval_id)
        if current.status != PENDING:
            raise ValueError(f"approval #{approval_id} is already {current.status}")
        status = APPROVED if approved else REJECTED
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE approval SET status=?, decided_at=?, decided_by=?, note=? WHERE id=?",
                (status, now(), by.strip(), note, approval_id),
            )
        self.store.append_event(
            "approval_decided",
            {"approval_id": approval_id, "status": status, "by": by.strip(), "note": note},
            task_id=current.task_id,
        )
        return self.get(approval_id)

    # -- reading -----------------------------------------------------------
    def get(self, approval_id: int) -> Approval:
        row = self.store.one("SELECT * FROM approval WHERE id=?", (approval_id,))
        if row is None:
            raise KeyError(f"no such approval: {approval_id}")
        return self._row(row)

    def pending(self) -> list[Approval]:
        return [self._row(r) for r in self.store.query(
            "SELECT * FROM approval WHERE status='pending' ORDER BY id")]

    def recent(self, limit: int = 50) -> list[Approval]:
        return [self._row(r) for r in self.store.query(
            "SELECT * FROM approval ORDER BY id DESC LIMIT ?", (limit,))]

    def for_task(self, task_id: str) -> list[Approval]:
        return [self._row(r) for r in self.store.query(
            "SELECT * FROM approval WHERE task_id=? ORDER BY id", (task_id,))]

    @staticmethod
    def _row(raw: Any) -> Approval:
        return Approval(
            id=raw["id"], task_id=raw["task_id"], tool=raw["tool"],
            arguments=raw["arguments"], reason=raw["reason"], status=raw["status"],
            created_at=raw["created_at"], decided_at=raw["decided_at"],
            decided_by=raw["decided_by"], note=raw["note"],
        )


def is_approved(queue: ApprovalQueue, *, task_id: str | None, tool: str,
                arguments: Any) -> bool:
    return queue.matching_status(task_id=task_id, tool=tool, arguments=arguments) == APPROVED


def is_rejected(queue: ApprovalQueue, *, task_id: str | None, tool: str,
                arguments: Any) -> bool:
    """True when a human said no to exactly this operation and nothing has changed."""

    return queue.matching_status(task_id=task_id, tool=tool, arguments=arguments) == REJECTED
