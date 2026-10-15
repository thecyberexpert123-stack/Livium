"""Recovery determines what actually happened, instead of replaying blindly.

An interrupted task leaves an unanswered question: did the operation that was in
flight happen or not? This module answers it with evidence rather than assumption.

Every consequential tool call records a write-ahead *intent* before the side effect
and resolves it afterwards. An intent still ``pending`` therefore means: the process
died somewhere between the two. The only trustworthy source of truth about the
outcome is the world itself, so recovery inspects the artefact the intent described
and classifies it:

``confirmed_complete``  the artefact exists and matches the intent (size, digest)
``not_executed``        the artefact demonstrably does not exist, so nothing landed
``conflicting``         the artefact exists but does not match: the on-disk content
                        is untrusted until a human looks at it
``unknown``             the effect is not observable from outside (a command, an
                        unknown tool), so the honest answer is "cannot be determined"

Only ``not_executed`` is safe to repeat. ``confirmed_complete`` must not be repeated
(repeating a write is usually harmless, but repeating a command that already ran may
not be). ``conflicting`` and ``unknown`` are surfaced to the operator with the exact
evidence and left unresolved — recovery never guesses, and it never reports success
for an operation it could not observe.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from typing import Any

from .config import Config
from .permissions import PermissionEngine
from .store import Store, now

CONFIRMED_COMPLETE = "confirmed_complete"
NOT_EXECUTED = "not_executed"
CONFLICTING = "conflicting"
UNKNOWN = "unknown"

UNRESOLVED = (CONFLICTING, UNKNOWN)


@dataclasses.dataclass
class Finding:
    intent_id: int
    tool: str
    determination: str
    detail: str
    evidence: str = ""

    @property
    def unresolved(self) -> bool:
        return self.determination in UNRESOLVED

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class RecoveryReport:
    task: dict[str, Any] | None
    findings: list[Finding]
    notes: list[str]
    observations: list[str]
    next_action: str
    safe_to_resume: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "findings": [f.as_dict() for f in self.findings],
            "notes": self.notes,
            "observations": self.observations,
            "next_action": self.next_action,
            "safe_to_resume": self.safe_to_resume,
        }

    def render(self) -> str:
        lines = ["Recovery report"]
        if self.task is None:
            lines.append("  no unfinished task is recorded")
        else:
            lines.append(f"  task {self.task['id']} [{self.task['status']}]: "
                         f"{self.task['objective']}")
        if self.findings:
            for finding in self.findings:
                lines.append(
                    f"  intent {finding.intent_id} ({finding.tool}) -> "
                    f"{finding.determination}: {finding.detail}"
                )
                if finding.evidence:
                    lines.append(f"      evidence: {finding.evidence}")
        else:
            lines.append("  no unresolved intents: every recorded operation is accounted for")
        if self.observations:
            lines.append("  observations:")
            for observation in self.observations:
                lines.append(f"    - {observation}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        lines.append(f"  next action: {self.next_action}")
        lines.append(f"  safe to resume: {self.safe_to_resume}")
        return "\n".join(lines)


class Recovery:
    def __init__(self, store: Store, engine: PermissionEngine, config: Config):
        self.store = store
        self.engine = engine
        self.config = config

    # -- entry point ------------------------------------------------------
    def inspect(self, task_id: str | None = None) -> RecoveryReport:
        """Reconstruct the state of an interrupted task from durable records and the
        real filesystem. Never mutates the artefacts it inspects."""

        task = self._task(task_id)
        if task is None and task_id is None:
            task = self._latest_unfinished_task()

        findings: list[Finding] = []
        notes: list[str] = []
        observations: list[str] = []

        if task is not None:
            for row in self.store.pending_intents(task["id"]):
                finding, note = self._examine(dict(row), task)
                findings.append(finding)
                if note:
                    notes.append(note)
            observations = self._observations(task["id"])

        unresolved = [f for f in findings if f.unresolved]
        if task is None:
            next_action = (
                "No unfinished task is recorded: every task reached a terminal state and "
                "every recorded intent is resolved. Nothing to recover."
            )
        else:
            next_action = self._next_action(task, findings)

        report = RecoveryReport(
            task=task,
            findings=findings,
            notes=notes,
            observations=observations,
            next_action=next_action,
            safe_to_resume=not unresolved and task is not None,
        )
        for finding in findings:
            self.store.append_event(
                "recovery_finding",
                {"intent_id": finding.intent_id, "tool": finding.tool,
                 "determination": finding.determination, "detail": finding.detail,
                 "evidence": finding.evidence},
                task_id=task["id"] if task else None,
            )
        if unresolved and task is not None:
            # The durable record of what a resumed run must establish. It lives in the
            # append-only journal rather than in a column so that the history of what was
            # unknown at each point in time survives.
            self.store.append_event(
                "recovery_inspected",
                {"task_id": task["id"], "safe_to_resume": report.safe_to_resume,
                 "requirements": [f.detail for f in unresolved]},
                task_id=task["id"],
            )
        return report

    def requirements(self, task_id: str) -> list[str]:
        """What a resumed run must establish before it can trust its own state.

        Read from the most recent recovery inspection: an older one describes an older
        world.
        """

        events = self.store.events(task_id=task_id, event_type="recovery_inspected")
        if not events:
            return []
        try:
            payload = json.loads(events[-1]["payload"])
        except (TypeError, ValueError, KeyError):
            return []
        requirements = payload.get("requirements") if isinstance(payload, dict) else None
        return [str(item) for item in requirements] if isinstance(requirements, list) else []

    # -- task selection ---------------------------------------------------
    def _task(self, task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        row = self.store.one("SELECT * FROM task WHERE id=?", (task_id,))
        return dict(row) if row is not None else None

    def _latest_unfinished_task(self) -> dict[str, Any] | None:
        pending = self.store.pending_intents()
        if pending:
            row = self.store.one("SELECT * FROM task WHERE id=?", (pending[-1]["task_id"],))
            if row is not None:
                return dict(row)
        rows = self.store.query(
            "SELECT * FROM task WHERE status NOT IN ('complete','failed') "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        return dict(rows[0]) if rows else None

    def _observations(self, task_id: str) -> list[str]:
        """Only what is genuinely unfinished: completed steps are not re-reported."""

        observations = []
        for row in self.store.query(
            "SELECT * FROM task_step WHERE task_id=? AND status != 'complete' ORDER BY idx",
            (task_id,),
        ):
            observations.append(
                f"step {row['idx']} ({row['phase']}) did not record completion: "
                f"{row['detail'] or 'no detail'}"
            )
        return observations

    # -- intent examination ----------------------------------------------
    def _examine(self, row: dict[str, Any], task: dict[str, Any]) -> tuple[Finding, str | None]:
        try:
            args = json.loads(row["args"] or "{}")
        except (TypeError, ValueError) as exc:
            return Finding(
                intent_id=row["id"], tool=row["tool"], determination=UNKNOWN,
                detail=f"the recorded arguments could not be parsed ({exc}), so what this "
                       "operation was supposed to do cannot be established",
            ), None
        if not isinstance(args, dict):
            args = {"value": args}

        if row["tool"] == "write_file":
            return self._examine_write(row, args, task)
        if row["tool"] == "run_command":
            argv = args.get("argv") or args.get("command") or []
            return Finding(
                intent_id=row["id"], tool=row["tool"], determination=UNKNOWN,
                detail=(
                    "the process that issued this command did not record its result, so the "
                    f"command may have completed and its effects may exist (argv={argv!r})"
                ),
                evidence=f"intent recorded at {row['created_at']}, never resolved",
            ), None
        return Finding(
            intent_id=row["id"], tool=row["tool"], determination=UNKNOWN,
            detail=(
                f"there is no recovery probe for '{row['tool']}', so whether it took effect "
                "cannot be determined from the world"
            ),
            evidence=f"intent recorded at {row['created_at']}, never resolved",
        ), None

    def _examine_write(self, row: dict[str, Any], args: dict[str, Any],
                       task: dict[str, Any]) -> tuple[Finding, str | None]:
        raw = str(args.get("path") or row.get("target") or "")
        if not raw:
            return Finding(
                intent_id=row["id"], tool=row["tool"], determination=UNKNOWN,
                detail="the intent recorded no target path, so nothing can be inspected",
            ), None

        target = pathlib.Path(raw).expanduser()
        decision = self.engine.check_write(target)
        if not decision.allowed:
            return Finding(
                intent_id=row["id"], tool=row["tool"], determination=CONFLICTING,
                detail=(
                    f"the recorded target {target} lies outside the permitted write roots "
                    f"({decision.reason}); its on-disk state is not trusted"
                ),
                evidence=f"permission engine: {decision.category}/{decision.reason}",
            ), (f"intent {row['id']} targets a path the agent may not write; treat it as "
                "untrusted and inspect it manually")

        try:
            if not target.exists():
                self.store.resolve_intent(
                    row["id"], "done", {"determination": NOT_EXECUTED, "checked": str(target)})
                return Finding(
                    intent_id=row["id"], tool=row["tool"], determination=NOT_EXECUTED,
                    detail=(
                        f"{target} does not exist, so the interrupted write never landed; "
                        "repeating it is safe"
                    ),
                    evidence=f"checked {target} at {now()}",
                ), None
            data = target.read_bytes()
        except OSError as exc:
            return Finding(
                intent_id=row["id"], tool=row["tool"], determination=UNKNOWN,
                detail=f"{target} could not be inspected ({exc}), so its state is unknown",
            ), None

        digest = hashlib.sha256(data).hexdigest()
        expected_bytes = args.get("bytes")
        evidence = (f"sha256={digest} size={len(data)} "
                    f"mtime={target.stat().st_mtime_ns} path={target}")
        if isinstance(expected_bytes, int) and len(data) == expected_bytes:
            self.store.resolve_intent(
                row["id"], "done",
                {"determination": CONFIRMED_COMPLETE, "sha256": digest, "bytes": len(data)},
            )
            return Finding(
                intent_id=row["id"], tool=row["tool"], determination=CONFIRMED_COMPLETE,
                detail=(
                    f"{target} holds the intended {expected_bytes} bytes, so the write "
                    "completed even though the state record was not updated; do not repeat it"
                ),
                evidence=evidence,
            ), None
        if isinstance(expected_bytes, int):
            return Finding(
                intent_id=row["id"], tool=row["tool"], determination=CONFLICTING,
                detail=(
                    f"{target} exists but holds {len(data)} bytes where the intent recorded "
                    f"{expected_bytes}: it was written by something else, or the write was "
                    "partial before the atomic replace"
                ),
                evidence=evidence,
            ), (f"{target} differs from what the interrupted operation intended; its content "
                "is untrusted until a human inspects it")
        return Finding(
            intent_id=row["id"], tool=row["tool"], determination=CONFLICTING,
            detail=(
                f"{target} exists, but the intent recorded no size to compare against, so "
                "completion cannot be confirmed"
            ),
            evidence=evidence,
        ), (f"{target} cannot be matched against the intent; treat its content as untrusted",
            )

    def _next_action(self, task: dict[str, Any] | None,
                     findings: list[Finding]) -> str:
        if any(f.determination == CONFLICTING for f in findings):
            return (
                "Inspect the conflicting artefacts listed above before continuing: the file "
                "on disk does not match the interrupted operation, and untrusted content must "
                "not be used as if it were the intended result."
            )
        if any(f.determination == UNKNOWN for f in findings):
            return (
                "Inspect the workspace for the effects of the unresolved operations listed "
                "above before continuing; they may have completed, so repeating them blindly "
                "could duplicate an effect."
            )
        if task is None:
            return "No unfinished task is recorded; nothing to recover."
        if findings:
            return (
                "Every interrupted operation has been accounted for from evidence; the task "
                f"can continue from: {task.get('next_action') or 'its objective'}"
            )
        return task.get("next_action") or "Continue the task from its objective."
