"""The Growth Guardian's enforced guarantees.

Tamper tests operate on a protected path injected through configuration, so they
never touch the repository's real governance files.
"""

from __future__ import annotations

import pathlib
import unittest

from support import LiviumTestCase, with_protected

from livium.guardian import GrowthGuardian
from livium.memory import Memory
from livium.permissions import PermissionEngine
from livium.store import Store


class GuardianFixture(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.paths.db)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.memory.seed_foundational(self.cfg.paths.prompts)
        self.guardian = GrowthGuardian(self.cfg, PermissionEngine(self.cfg), self.store,
                                      self.memory)


class BaselineTests(GuardianFixture):
    def test_unbaselined_state_is_reported_but_does_not_freeze(self) -> None:
        """No baseline is a setup step, not evidence of tampering.

        Conflating the two either blocks every fresh install or hides a real
        modification; this test pins the distinction.
        """

        report = self.guardian.integrity()
        self.assertFalse(report.ok)
        self.assertEqual(report.status, "unbaselined")
        self.assertTrue(report.missing_baseline)
        self.guardian.watch(task_id="t", trigger="run_command")
        self.assertFalse(self.guardian.frozen)

    def test_baseline_records_real_digests(self) -> None:
        result = self.guardian.record_baseline(approved_by="Test Human", force=True)
        self.assertEqual(result["baseline_files"], len(self.guardian.protected_files()))
        report = self.guardian.integrity()
        self.assertTrue(report.ok, report.detail)
        self.assertEqual(report.status, "ok")
        self.assertTrue(self.store.events(event_type="guardian_rebaselined"))

    def test_the_agent_cannot_rebaseline(self) -> None:
        for identity in ("agent", "Livium", "self", "auto", "", "Anonymous", "unknown", "-"):
            with self.subTest(approved_by=identity):
                with self.assertRaises(PermissionError):
                    self.guardian.record_baseline(approved_by=identity)


class TamperDetectionTests(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A protected file this test owns, so a real modification can be made
        # without going near the repository's actual governance files.
        self.victim = self.home.dir / "rules.py"
        self.victim.write_text("RULE = 'original'\n", encoding="utf-8")
        self.cfg = with_protected(self.config(), self.victim)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.paths.db)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.engine = PermissionEngine(self.cfg)
        self.guardian = GrowthGuardian(self.cfg, self.engine, self.store, self.memory)

    def test_modification_is_detected_and_freezes_the_run(self) -> None:
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        self.assertTrue(self.guardian.integrity().ok)

        self.victim.write_text("RULE = 'weakened'\n", encoding="utf-8")

        report = self.guardian.watch(task_id="t-tamper", trigger="run_command")
        self.assertFalse(report.ok)
        self.assertEqual(report.status, "tampered")
        self.assertEqual(len(report.mismatches), 1)
        self.assertIn("recorded", report.mismatches[0])
        self.assertTrue(self.guardian.frozen)
        self.assertIn("differ from the recorded baseline", self.guardian.frozen_reason)
        self.assertTrue(self.store.events(event_type="guardian_freeze"))
        violations = self.store.events(event_type="boundary_violation")
        self.assertEqual(json_category(violations[0]), "protected")

    def test_the_agent_cannot_write_a_protected_path(self) -> None:
        decision = self.engine.check_write(self.victim)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.category, "protected")

    def test_promotion_is_blocked_while_integrity_fails(self) -> None:
        gate = self.guardian.promotion_gate("anything")
        self.assertFalse(gate["ok"])
        self.assertIn("no recorded baseline", gate["detail"])

    def test_promotion_gate_blocks_after_a_boundary_violation(self) -> None:
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        self.guardian.watch(task_id="t-x", trigger="write_file")  # clean
        self.store.append_event(
            "boundary_violation",
            {"tool": "write_file", "category": "protected", "reason": "capability 'sneaky' tried "
             "to overwrite a protected file"},
            task_id="t-x",
        )
        gate = self.guardian.promotion_gate("sneaky")
        self.assertFalse(gate["ok"])

    def test_frozen_state_cannot_be_cleared_by_the_agent(self) -> None:
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        self.victim.write_text("RULE = 'weakened'\n", encoding="utf-8")
        self.guardian.watch(task_id="t", trigger="run_command")
        self.assertTrue(self.guardian.frozen)
        # Restoring the file clears the *condition*; the guardian still requires a
        # deliberate re-baseline, which only accepts a human approver.
        self.victim.write_text("RULE = 'original'\n", encoding="utf-8")
        self.assertTrue(self.guardian.integrity().ok)
        self.assertTrue(self.guardian.frozen)
        with self.assertRaises(PermissionError):
            self.guardian.record_baseline(approved_by="agent")


class ConsequenceTests(GuardianFixture):
    def test_consequences_are_derived_from_the_journal(self) -> None:
        self.store.append_event(
            "boundary_violation", {"tool": "run_command", "category": "command", "reason": "rm"},
            task_id="t-cons",
        )
        consequences = self.guardian.consequences("t-cons")
        self.assertTrue(consequences)
        self.assertTrue(consequences[0].enforced)
        self.assertIn("must not be retried", consequences[0].response)

    def test_clean_task_has_no_enforced_consequences(self) -> None:
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        consequences = self.guardian.consequences("t-clean")
        self.assertFalse([c for c in consequences if c.enforced])

    def test_duplicate_memory_is_reported_as_a_consolidation_prompt(self) -> None:
        self.memory.remember("experience", "the retry needed a longer timeout", key="a")
        self.memory.remember("experience", "The retry needed a longer timeout!", key="b")
        observations = [c.observation for c in self.guardian.consequences(None)]
        self.assertTrue(any("duplicate" in o for o in observations))


class InstructionProposalTests(GuardianFixture):
    def test_a_proposal_is_recorded_and_the_file_is_untouched(self) -> None:
        target = "GROWTH.md"
        before = (self.cfg.paths.prompts / target).read_text(encoding="utf-8")
        result = self.guardian.propose_instruction_change(
            target_file=target,
            observed_failure="the agent reused a duplicate helper twice",
            suspected_cause="reuse check was skipped",
            proposed_change="Always search for reuse before building anything.",
            evidence="events 1,2 and mem:9",
            occurrence_count=3,
            previous_version_digest="abc123",
        )
        after = (self.cfg.paths.prompts / target).read_text(encoding="utf-8")
        self.assertEqual(before, after, "the guardian must not edit its own instructions")
        self.assertIn("not modified", result["note"])
        record = self.memory.get(result["memory_id"])
        assert record is not None
        self.assertIn("proposed change", record.detail)
        self.assertIn("previous version digest", record.detail)
        self.assertTrue(self.store.events(event_type="instruction_change_proposed"))

    def test_a_single_occurrence_is_not_ready_for_review(self) -> None:
        result = self.guardian.propose_instruction_change(
            target_file="GROWTH.md", observed_failure="one-off slip",
            suspected_cause="unknown", proposed_change="add a sweeping new rule",
            evidence="one event", occurrence_count=1, previous_version_digest="d",
        )
        self.assertFalse(result["ready_for_review"])

    def test_overlap_with_existing_rules_is_flagged(self) -> None:
        result = self.guardian.propose_instruction_change(
            target_file="FOUNDATIONAL.md",
            observed_failure="the agent claimed a command succeeded without running it",
            suspected_cause="skipped verification",
            proposed_change="Never claim that a command succeeded without observing its result.",
            evidence="two events",
            occurrence_count=2,
            previous_version_digest="d",
        )
        self.assertTrue(result["contradictions"])
        self.assertFalse(result["ready_for_review"])
        # The scan must compare against the actual rule text, not the pointer record.
        self.assertIn("FOUNDATIONAL.md", " ".join(result["contradictions"]))
        finding = " ".join(result["contradictions"]).lower()
        self.assertIn("never", finding)
        self.assertIn("claim", finding)


def json_category(event) -> str:
    import json

    return json.loads(event["payload"])["category"]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
