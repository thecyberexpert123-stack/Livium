"""Recovery determines what actually happened, instead of replaying blindly."""

from __future__ import annotations

import json
import unittest

from support import LiviumTestCase

from livium.memory import Memory
from livium.permissions import PermissionEngine
from livium.recovery import (
    CONFIRMED_COMPLETE,
    CONFLICTING,
    NOT_EXECUTED,
    UNKNOWN,
    Recovery,
)
from livium.store import Store


class RecoveryFixture(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.paths.db)
        self.addCleanup(self.store.close)
        self.recovery = Recovery(self.store, PermissionEngine(self.cfg), self.cfg)
        self.workspace = self.cfg.paths.workspace

    def open_task(self, task_id: str = "t-rec", next_action: str | None = "continue step 3") -> None:
        self.store.execute(
            "INSERT INTO task(id, objective, acceptance, status, created_at, updated_at, next_action)"
            " VALUES(?,?,'[]','active','now','now',?)",
            (task_id, "a multi-step task", next_action),
        )

    def step(self, index: int, status: str, detail: str = "") -> None:
        self.store.execute(
            "INSERT INTO task_step(task_id, idx, phase, status, detail, started_at) "
            "VALUES('t-rec',?, 'model_call', ?, ?, 'now')",
            (index, status, detail),
        )


class WriteIntentRecoveryTests(RecoveryFixture):
    def test_an_interrupted_write_that_landed_is_confirmed_complete(self) -> None:
        """The process died before recording success; the file is the evidence."""

        payload = "written but never acknowledged\n"
        # Write exact bytes: Windows text mode translates LF to CRLF.
        (self.workspace / "half.md").write_bytes(payload.encode("utf-8"))
        self.store.record_intent(
            "write_file", {"path": str(self.workspace / "half.md"), "bytes": len(payload.encode())},
            task_id="t-rec", target=str(self.workspace / "half.md"),
        )
        self.open_task()

        report = self.recovery.inspect("t-rec")
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertEqual(finding.determination, CONFIRMED_COMPLETE)
        self.assertIn("completed even though the state record was not updated", finding.detail)
        self.assertIn("sha256=", finding.evidence)
        # And the intent is resolved so the question is not asked twice.
        self.assertEqual(self.store.pending_intents("t-rec"), [])
        self.assertEqual(self.store.one("SELECT status FROM intent ORDER BY id")["status"], "done")

    def test_an_interrupted_write_that_never_landed_is_not_executed(self) -> None:
        self.store.record_intent(
            "write_file", {"path": str(self.workspace / "never.md"), "bytes": 10},
            task_id="t-rec",
        )
        self.open_task()
        report = self.recovery.inspect("t-rec")
        self.assertEqual(report.findings[0].determination, NOT_EXECUTED)
        self.assertIn("does not exist", report.findings[0].detail)

    def test_a_differing_file_is_reported_as_conflicting_not_as_success(self) -> None:
        (self.workspace / "changed.md").write_text("something else entirely", encoding="utf-8")
        self.store.record_intent(
            "write_file", {"path": str(self.workspace / "changed.md"), "bytes": 4},
            task_id="t-rec",
        )
        self.open_task()
        report = self.recovery.inspect("t-rec")
        self.assertEqual(report.findings[0].determination, CONFLICTING)
        self.assertTrue(any("untrusted" in note for note in report.notes))


class CommandIntentRecoveryTests(RecoveryFixture):
    def test_a_command_is_reported_as_unknown_rather_than_assumed(self) -> None:
        self.store.record_intent(
            "run_command", {"argv": ["python3", "-m", "unittest"], "timeout": 60},
            task_id="t-rec",
        )
        self.open_task()
        report = self.recovery.inspect("t-rec")
        finding = report.findings[0]
        self.assertEqual(finding.determination, UNKNOWN)
        self.assertIn("may have completed", finding.detail)
        self.assertIn("inspect", report.next_action.lower())
        self.assertFalse(any("completed successfully" in f.detail for f in report.findings))

    def test_unknown_tool_intent_is_reported_as_unrecoverable(self) -> None:
        self.store.record_intent("future_tool", {"x": 1}, task_id="t-rec")
        self.open_task()
        report = self.recovery.inspect("t-rec")
        self.assertEqual(report.findings[0].determination, UNKNOWN)
        self.assertIn("no recovery probe", report.findings[0].detail)


class TaskStateReconstructionTests(RecoveryFixture):
    def test_unfinished_steps_are_surfaced(self) -> None:
        self.open_task()
        self.step(1, "complete", "wrote notes.md")
        self.step(2, "in_progress", "was counting lines")
        report = self.recovery.inspect("t-rec")
        self.assertTrue(any("step 2" in observation for observation in report.observations))
        self.assertFalse(any("step 1" in observation for observation in report.observations))
        self.assertEqual(report.task["objective"], "a multi-step task")
        self.assertEqual(report.next_action, "continue step 3")
        self.assertTrue(report.safe_to_resume)

    def test_no_task_and_no_intents_is_an_honest_empty_report(self) -> None:
        report = self.recovery.inspect()
        self.assertIsNone(report.task)
        self.assertEqual(report.findings, [])
        self.assertIn("No unfinished task", report.next_action)

    def test_recovery_findings_are_journalled(self) -> None:
        self.store.record_intent("write_file", {"path": str(self.workspace / "x.md"), "bytes": 1},
                                task_id="t-rec")
        self.open_task()
        self.recovery.inspect("t-rec")
        events = self.store.events(event_type="recovery_finding")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["payload"])["determination"], NOT_EXECUTED)

    def test_report_renders_without_inventing_detail(self) -> None:
        self.open_task()
        text = self.recovery.inspect("t-rec").render()
        self.assertIn("Recovery report", text)
        self.assertIn("no unresolved intents", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
