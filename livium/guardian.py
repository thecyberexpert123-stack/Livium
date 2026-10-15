"""The Growth Guardian.

Its job, from the specification: make poor engineering decisions have concrete
consequences rather than being noted and forgotten. It is implemented mostly as
persistent instructions (``livium/prompts/GROWTH.md``, loaded into memory as
protected records) plus the small set of mechanisms below that instructions
cannot provide on their own:

1. **Consequence gating.** A task in which a boundary was violated, or in which a
   capability's tests failed, cannot promote anything. This is checked at
   promotion time from the journal, not from the agent's recollection.
2. **Protected-file integrity.** A baseline of SHA-256 digests for the files that
   encode Livium's instructions and permissions is recorded by a human. The agent
   has no tool that writes those files and no tool that re-baselines them. Tamper
   detection runs at startup, after every command execution and before every
   promotion, so a bypass attempt is caught immediately rather than eventually.
3. **Self-freeze.** Once integrity fails, the run stops. The agent cannot clear
   the flag; only a human re-baselining can.

Honest scope: (2) is *detection plus refusal to proceed*, not prevention. A
subprocess launched through ``run_command`` runs with the Livium process's
operating-system privileges, so an agent determined to overwrite a protected file
from inside a Python one-liner can do so -- and will then be caught by the check
that follows. Prevention against a hostile model requires an OS boundary around
the whole process (container/VM), which docs/LIMITATIONS.md states plainly rather
than pretending the tool layer supplies it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
from typing import Any

from .config import Config
from .memory import Memory
from .permissions import PermissionEngine
from .store import Store, now


@dataclasses.dataclass
class IntegrityReport:
    ok: bool
    detail: str
    checked: list[str]
    mismatches: list[dict[str, str]]
    missing_baseline: list[str]
    # "ok" | "unbaselined" | "tampered". The distinction matters: no baseline is a
    # first-run setup step, whereas a mismatch is evidence of a real modification.
    # Treating them the same either blocks every fresh install or hides tampering.
    status: str = "ok"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Consequence:
    observation: str
    response: str
    enforced: bool
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class GrowthGuardian:
    def __init__(
        self,
        config: Config,
        engine: PermissionEngine,
        store: Store,
        memory: Memory,
    ):
        self.config = config
        self.engine = engine
        self.store = store
        self.memory = memory
        self._baseline_digests: dict[str, str] = {}
        self._frozen_reason: str | None = None
        self._unbaselined_warned = False
        self.load_baseline()

    # -- protected-file integrity -----------------------------------------
    def protected_files(self) -> list[pathlib.Path]:
        """Every file that encodes Livium's instructions or its own boundaries."""

        files: list[pathlib.Path] = []
        for path in self.engine.protected_files():
            if path.exists():
                files.append(path)
        prompt_dir = self.config.paths.prompts
        if prompt_dir.exists():
            files.extend(sorted(p for p in prompt_dir.glob("*.md") if p.is_file()))
        return sorted(set(files))

    @staticmethod
    def _digest(path: pathlib.Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def load_baseline(self) -> dict[str, str]:
        rows = self.store.query("SELECT path, sha256 FROM guardian_baseline")
        self._baseline_digests = {row["path"]: row["sha256"] for row in rows}
        return dict(self._baseline_digests)

    def record_baseline(self, *, approved_by: str) -> dict[str, Any]:
        """Trusted operation. Called from the CLI by a human, never by the agent."""

        if not approved_by or approved_by.strip().lower() in ("agent", "livium", "self", "auto"):
            raise PermissionError(
                "re-baselining the guardian requires an explicit human approver name"
            )
        files = self.protected_files()
        if not files:
            raise PermissionError("no protected files found to baseline; refusing to record an empty baseline")
        with self.store.transaction() as conn:
            for path in files:
                conn.execute(
                    "INSERT INTO guardian_baseline(path, sha256, recorded_at) VALUES(?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, "
                    "recorded_at=excluded.recorded_at",
                    (str(path), self._digest(path), now()),
                )
        self.store.append_event(
            "guardian_rebaselined",
            {"approved_by": approved_by, "files": [str(p) for p in files]},
        )
        self.load_baseline()
        return {"baseline_files": len(files), "approved_by": approved_by}

    def integrity(self) -> IntegrityReport:
        files = self.protected_files()
        mismatches: list[dict[str, str]] = []
        missing: list[str] = []
        checked: list[str] = []
        for path in files:
            key = str(path)
            checked.append(key)
            recorded = self._baseline_digests.get(key)
            if recorded is None:
                missing.append(key)
                continue
            current = self._digest(path)
            if current != recorded:
                mismatches.append({"path": key, "recorded": recorded, "current": current})
        if mismatches:
            return IntegrityReport(
                ok=False, status="tampered",
                detail=(
                    "protected files differ from the recorded baseline: "
                    + "; ".join(f"{m['path']}" for m in mismatches)
                    + ". These files encode Livium's permissions and instructions; a change to "
                      "them must be deliberate and human-approved."
                ),
                checked=checked, mismatches=mismatches, missing_baseline=missing,
            )
        if missing:
            return IntegrityReport(
                ok=False, status="unbaselined",
                detail=(
                    f"{len(missing)} protected file(s) have no recorded baseline, so tampering "
                    f"could not be detected: {', '.join(missing)}. A human must run "
                    f"`livium guardian baseline --approved-by <name>` once to establish it."
                ),
                checked=checked, mismatches=[], missing_baseline=missing,
            )
        return IntegrityReport(
            ok=True, status="ok",
            detail=f"{len(checked)} protected file(s) match the recorded baseline",
            checked=checked, mismatches=[], missing_baseline=[],
        )

    def watch(self, *, task_id: str | None, trigger: str) -> IntegrityReport:
        """Check integrity and freeze the run if it fails.

        Called after every command execution and every write attempt. Hashing a
        few tens of kilobytes costs well under a millisecond, so there is no
        reason to sample it.
        """

        report = self.integrity()
        if report.status == "tampered":
            self._frozen_reason = report.detail
            self.store.append_event(
                "boundary_violation",
                {"tool": trigger, "category": "protected", "reason": report.detail,
                 "mismatches": report.mismatches, "missing_baseline": report.missing_baseline},
                task_id=task_id,
            )
            self.store.append_event(
                "guardian_freeze", {"reason": report.detail, "trigger": trigger},
                task_id=task_id,
            )
        elif report.status == "unbaselined" and not self._unbaselined_warned:
            # Warned once per process, not once per command: the condition is a
            # setup state, and a wall of identical events would bury the signal.
            self._unbaselined_warned = True
            self.store.append_event(
                "guardian_integrity_warning",
                {"tool": trigger, "reason": report.detail, "missing_baseline": report.missing_baseline},
                task_id=task_id,
            )
        return report

    @property
    def frozen(self) -> bool:
        return self._frozen_reason is not None

    @property
    def frozen_reason(self) -> str | None:
        return self._frozen_reason

    def freeze_status(self) -> dict[str, Any]:
        return {"frozen": self.frozen, "reason": self._frozen_reason}

    # -- consequence evaluation -------------------------------------------
    def consequences(self, task_id: str | None) -> list[Consequence]:
        """Work out which consequences apply, from the journal rather than from
        the agent's summary of what happened."""

        out: list[Consequence] = []
        if task_id:
            violations = self.store.events(task_id=task_id, event_type="boundary_violation", limit=50)
            if violations:
                out.append(
                    Consequence(
                        observation=f"{len(violations)} boundary violation(s) in this task",
                        response=(
                            "Nothing produced in this task may be promoted. The violations are "
                            "recorded permanently and the affected operation must not be retried "
                            "through another route."
                        ),
                        enforced=True,
                        evidence=f"event ids {[v['id'] for v in violations]}",
                    )
                )
            failures = self.store.events(task_id=task_id, event_type="regression_failed", limit=50)
            if failures:
                out.append(
                    Consequence(
                        observation=f"{len(failures)} failing regression check(s)",
                        response="Promotion blocked until the regression passes.",
                        enforced=True,
                        evidence=f"event ids {[f['id'] for f in failures]}",
                    )
                )
            rejections = self.memory.search(
                "rejected change complexity regression", kinds=["failure"], limit=3
            )
            if rejections:
                out.append(
                    Consequence(
                        observation="recent recorded failures are relevant to this task",
                        response="Retrieve and apply them before repeating the same approach.",
                        enforced=False,
                        evidence="; ".join(f"mem:{r.id}" for r in rejections),
                    )
                )
        duplicates = self.memory.find_duplicates()
        if duplicates:
            out.append(
                Consequence(
                    observation=f"{len(duplicates)} group(s) of duplicate memory records",
                    response="Investigate consolidation; run `livium memory consolidate`.",
                    enforced=False,
                    evidence=f"fingerprints {[d['fingerprint'] for d in duplicates][:5]}",
                )
            )
        if self.frozen:
            out.append(
                Consequence(
                    observation="protected-file integrity failed during this run",
                    response=(
                        "The run is frozen: no further work is accepted and no capability may be "
                        "promoted. A human must inspect the protected files and re-baseline "
                        "deliberately."
                    ),
                    enforced=True,
                    evidence=self._frozen_reason or "",
                )
            )
        return out

    def promotion_gate(self, name: str) -> dict[str, Any]:
        """The check ``CapabilityRegistry.promote`` calls. Returns, never raises."""

        integrity = self.integrity()
        blockers: list[str] = []
        if not integrity.ok:
            # Promotion is gated on *both* states: an unverifiable integrity state is
            # not good enough to mint a new active capability.
            blockers.append(integrity.detail)
        if self.frozen:
            blockers.append(f"guardian is frozen: {self._frozen_reason}")
        violations = self.store.events(event_type="boundary_violation", limit=200)
        related = [
            row for row in violations
            if name in row["payload"] or name in (row["task_id"] or "")
        ]
        if any(json_has(row["payload"], "capability") for row in related):
            blockers.append(
                f"{len(related)} boundary violation(s) reference this capability"
            )
        return {
            "ok": not blockers,
            "detail": " | ".join(blockers) if blockers else "integrity intact; no violations recorded",
            "integrity": integrity.as_dict(),
            "integrity_status": integrity.status,
            "frozen": self.frozen,
        }

    # -- instruction self-improvement (proposal only) ---------------------
    def propose_instruction_change(
        self,
        *,
        target_file: str,
        observed_failure: str,
        suspected_cause: str,
        proposed_change: str,
        evidence: str,
        occurrence_count: int,
        previous_version_digest: str,
    ) -> dict[str, Any]:
        """Record a proposal. Never writes the instruction file.

        The checks the specification asks for are performed here and their results
        are stored with the proposal, so a reviewer can see them without trusting
        the agent's account: is the lesson supported by repetition, does it
        contradict an existing rule, and is the previous version recorded.
        """

        contradictions = self._contradiction_scan(proposed_change)
        existing = self.memory.search(observed_failure, kinds=["foundational"], limit=3)
        ready = occurrence_count >= 2 and not contradictions
        record_id, _ = self.memory.remember(
            "decision",
            f"Proposed instruction change to {target_file}",
            key=f"instruction_proposal:{target_file}",
            detail=(
                f"observed failure: {observed_failure}\n"
                f"suspected cause: {suspected_cause}\n"
                f"proposed change: {proposed_change}\n"
                f"occurrences: {occurrence_count}\n"
                f"previous version digest: {previous_version_digest}\n"
                f"contradiction scan: {contradictions or 'none found'}\n"
                f"review state: {'ready for human review' if ready else 'not yet justified'}"
            ),
            tags=["instruction-proposal", target_file],
            confidence=0.4 if ready else 0.2,
            verification="unverified",
            evidence=evidence,
            source="agent:guardian",
        )
        self.store.append_event(
            "instruction_change_proposed",
            {"target_file": target_file, "memory_id": record_id, "ready": ready,
             "occurrences": occurrence_count, "contradictions": contradictions,
             "previous_version_digest": previous_version_digest},
        )
        return {
            "memory_id": record_id,
            "ready_for_review": ready,
            "contradictions": contradictions,
            "existing_related_rules": [r.id for r in existing],
            "note": (
                "Recorded as a proposal only. The instruction file was not modified: it is a "
                "protected path and the write tool refuses it. A human applies the change and "
                "re-baselines."
            ),
        }

    def _contradiction_scan(self, proposed_change: str) -> list[str]:
        """Flag overlap with the existing rules so a contradiction is visible.

        The scan reads the *instruction files* rather than the protected memory
        records: the records deliberately hold a pointer plus a digest (the full
        text reaches the model through the system prompt), so checking them would
        have compared a proposal against almost no text and reported "no overlap"
        for an obvious restatement of rule 1.

        Deliberately lexical, not semantic: it will miss subtle contradictions and
        it will flag some harmless overlaps. That is an honest limitation of a cheap
        check, and the finding is presented as a prompt for human review rather than
        as a verdict.
        """

        words = {
            w for w in "".join(c if c.isalnum() else " " for c in proposed_change.lower()).split()
            if len(w) > 4
        }
        if not words:
            return []

        sources: list[tuple[str, str]] = []
        prompt_dir = self.config.paths.prompts
        for path in sorted(prompt_dir.glob("*.md")) if prompt_dir.exists() else []:
            try:
                sources.append((path.name, path.read_text(encoding="utf-8").lower()))
            except OSError:
                continue
        for record in self.memory.protected():
            sources.append(
                (record.key or f"mem:{record.id}",
                 (record.content + " " + (record.detail or "")).lower())
            )

        findings: list[str] = []
        for name, text in sources:
            overlap = sorted(w for w in words if w in text)
            if len(overlap) >= 3:
                findings.append(
                    f"overlaps {name} on {', '.join(overlap[:6])} — check whether the existing "
                    f"rule already covers this before adding another"
                )
        return findings


def json_has(payload: str, needle: str) -> bool:
    return needle in (payload or "")
