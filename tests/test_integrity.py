"""The integrity response: policy, detection, review, verification, recovery.

These tests are about the properties that make an integrity record worth having. A
detected change must become a row that cannot be edited or deleted; a recovery must be
verified against the bytes on disk rather than reported as done; and the enforcement
probes must fail on a weakened control -- because a check that always passes is not a
check. The last test is the important one: it hands the verifier a deliberately weakened
copy of the module that holds the policy and asserts that the probes catch it.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import unittest


def digest_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

from support import REPO_ROOT, LiviumTestCase, with_protected

from livium.config import Config
from livium.integrity import (
    ENFORCEMENT, GOVERNANCE, INSTRUCTIONS, OPERATIONAL, Integrity, components,
    from_configuration, policy_for,
)
from livium.memory import Memory
from livium.permissions import PermissionEngine
from livium.store import Store


class IntegrityFixture(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.victim = self.home.dir / "rules.py"
        self.victim.write_text("RULE = 'original'\n\n\ndef check(value):\n    return value == 'ok'\n",
                               encoding="utf-8")
        self.cfg = with_protected(self.config(), self.victim)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.paths.db)
        self.addCleanup(self.store.close)
        self.integrity = Integrity(self.cfg, self.store, PermissionEngine(self.cfg))

    def tamper(self) -> None:
        self.victim.write_text("RULE = 'weakened'\n", encoding="utf-8")


class PolicyTests(IntegrityFixture):
    def test_every_component_carries_a_reason_and_an_authoriser(self) -> None:
        entries = self.integrity.components()
        self.assertTrue(entries)
        for component in entries:
            with self.subTest(path=str(component.path)):
                self.assertIn(component.tier,
                              (GOVERNANCE, ENFORCEMENT, INSTRUCTIONS, OPERATIONAL))
                self.assertTrue(component.why.strip(), "a protected file with no stated why")
                self.assertTrue(component.authorizer.strip())
                self.assertTrue(component.verification.strip())

    def test_the_governance_modules_are_protected_and_classified(self) -> None:
        by_path = {str(c.path): c for c in components(REPO_ROOT)}
        for name in ("guardian.py", "integrity.py", "permissions.py", "approvals.py"):
            with self.subTest(module=name):
                component = by_path[str(REPO_ROOT / "livium" / name)]
                self.assertEqual(component.tier, GOVERNANCE)
                self.assertTrue(component.needs_independent_check)
        self.assertEqual(policy_for(REPO_ROOT / "livium" / "router.py"), None,
                         "ordinary feature code has no integrity policy")

    def test_a_file_protected_only_by_configuration_says_so(self) -> None:
        """A deployment's own file is watched even though the policy does not name it."""

        paths = [str(c.path) for c in self.integrity.components()]
        self.assertIn(str(self.victim), paths)
        entry = next(c for c in self.integrity.components() if c.path == self.victim)
        self.assertEqual(entry.tier, OPERATIONAL)
        self.assertIn("policy does not name it", entry.why)
        self.assertIsNotNone(self.integrity.policy_for(self.victim),
                             "a method that decides about a change must see the same set "
                             "the guardian watches")

    def test_the_effective_policy_covers_everything_the_engine_refuses(self) -> None:
        engine = PermissionEngine(self.cfg)
        watched = {c.path.resolve() for c in self.integrity.components()}
        for path in engine.protected_files():
            with self.subTest(path=str(path)):
                self.assertIn(path.resolve(), watched)


class DetectionTests(IntegrityFixture):
    def test_inspect_reports_ok_after_a_baseline_and_changed_after_a_write(self) -> None:
        self.integrity.protect(approved_by="Test Human", force=True)
        self.assertEqual(self.integrity.inspect(record=False)["status"], "ok")
        self.tamper()
        view = self.integrity.inspect(record=True)
        self.assertEqual(view["status"], "tampered")
        self.assertIn(str(self.victim), view["changed"])

    def test_a_detection_becomes_a_record_with_both_digests(self) -> None:
        self.integrity.protect(approved_by="Test Human", force=True)
        expected = self.integrity.baseline()[str(self.victim)]
        self.tamper()
        self.integrity.inspect(detection="test", record=True)
        rows = self.integrity.events(limit=10)
        detected = [row for row in rows if row["event"] == "change_detected"]
        self.assertTrue(detected, "a detected change must be an authoritative record")
        record = detected[0]
        self.assertEqual(record["paths"], [str(self.victim)])
        self.assertEqual(record["observed"]["digests"][str(self.victim)],
                         digest_of(self.victim))
        self.assertEqual(record["expected"]["digests"][str(self.victim)], expected)
        self.assertTrue(record["policy"].get("action"))

    def test_the_record_cannot_be_edited_or_deleted(self) -> None:
        """The row a tamper attempt would most like to rewrite is the row about it."""

        self.integrity.record_event("policy_violation", tier=GOVERNANCE, paths=[self.victim],
                                    observed={}, expected={}, detection="test",
                                    policy={"note": "test"}, decision="recorded",
                                    evidence={}, outcome="recorded")
        row = self.store.one("SELECT id FROM integrity_event ORDER BY id DESC LIMIT 1")
        for statement in (f"UPDATE integrity_event SET event='change_detected' WHERE id={row['id']}",
                          f"DELETE FROM integrity_event WHERE id={row['id']}"):
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store.execute(statement)

    def test_an_unbaselined_component_is_reported_as_such_not_as_clean(self) -> None:
        view = self.integrity.inspect(record=True)
        self.assertEqual(view["status"], "unbaselined")
        self.assertFalse(view["ok"])
        self.assertIn(str(self.victim), view["unbaselined"])


class ReviewTests(IntegrityFixture):
    def test_review_names_what_the_change_did_to_the_structure(self) -> None:
        self.integrity.protect(approved_by="Test Human", force=True)
        self.tamper()  # the new content drops the check() function entirely
        review = self.integrity.review_change(self.victim, proposed_by="test")
        self.assertEqual(review["path"], str(self.victim))
        self.assertTrue(review["findings"], "removing a function must be visible in the review")
        self.assertIn("check", json.dumps(review["findings"]))
        self.assertEqual(review["decision"], "requires_explicit_acknowledgement")
        self.assertIn("it does not authorise it", review["note"])

    def test_review_of_ordinary_code_says_no_policy_applies(self) -> None:
        plain = self.home.dir / "notes.txt"
        plain.write_text("x", encoding="utf-8")
        review = self.integrity.review_change(plain)
        self.assertEqual(review["decision"], "unrestricted")
        self.assertEqual(review["tier"], OPERATIONAL)

    def test_reviewing_a_candidate_writes_nothing(self) -> None:
        self.integrity.protect(approved_by="Test Human", force=True)
        digest_before = digest_of(self.victim)
        review = self.integrity.review_candidate(
            self.victim, "RULE = 'candidate'\n", proposed_by="test")
        self.assertEqual(digest_of(self.victim), digest_before)
        self.assertTrue(review["structure_reviewed"])
        self.assertTrue(review["findings"], "dropping check() is a structural change")
        self.assertIn("verify", review["policy_not_answered_here"])

    def test_a_weakened_permission_policy_is_flagged_as_broadening(self) -> None:
        """Widening what the agent may do is the change most worth catching."""

        from livium.integrity import policy_broadening

        before = {"allowed_binaries": ["ls", "cat"], "denied_binaries": ["rm"],
                  "network_allowlist": [], "write_roots": ["/work"]}
        after = {"allowed_binaries": ["ls", "cat", "rm"], "denied_binaries": [],
                 "network_allowlist": ["example.com"], "write_roots": ["/work", "/etc"]}
        widening = policy_broadening(before, after)
        self.assertTrue(widening, "a more permissive policy must be reported")
        self.assertFalse(policy_broadening(before, dict(before)),
                         "an unchanged policy is not broader")
        self.assertFalse(
            policy_broadening(after, before),
            "narrowing a policy is not broadening, whatever else it may be")


class ApprovalTests(IntegrityFixture):
    def setUp(self) -> None:
        super().setUp()
        self.integrity.protect(approved_by="Test Human", force=True)
        self.tamper()

    def test_an_unnamed_approver_is_refused(self) -> None:
        for identity in ("", "agent", "Anonymous", "-"):
            with self.subTest(approved_by=identity):
                with self.assertRaises(PermissionError):
                    self.integrity.approve_change(self.victim, approved_by=identity,
                                                  reason="please", run_tests=False)

    def test_approval_requires_the_weakening_to_be_acknowledged(self) -> None:
        digest_before = self.integrity.baseline()[str(self.victim)]
        approval = self.integrity.approve_change(self.victim, approved_by="Real Human",
                                                 reason="tidy up", run_tests=False)
        self.assertFalse(approval["approved"])
        self.assertEqual(approval["reason"], "structural review found weakening")
        self.assertTrue(approval["unacknowledged"])
        self.assertFalse([r for r in self.integrity.events(limit=20) if r["event"] == "change_approved"],
                         "a refused approval must not be recorded as an approval")
        self.assertEqual(self.integrity.baseline()[str(self.victim)], digest_before,
                         "a refused approval must not move the baseline")

    def test_a_refusal_is_recorded_with_the_reason(self) -> None:
        self.integrity.approve_change(self.victim, approved_by="Real Human",
                                      reason="tidy up", run_tests=False)
        decisions = [row for row in self.integrity.events(limit=10)
                     if row["event"] == "change_decision"]
        self.assertTrue(decisions)
        self.assertEqual(decisions[0]["decision"], "rejected")
        self.assertEqual(decisions[0]["authorization"], "Real Human")
        self.assertIn("acknowledgement", json.dumps(decisions[0].get("evidence") or {}))

    def test_an_acknowledged_change_is_approved_verified_and_re_baselined(self) -> None:
        review = self.integrity.review_change(self.victim, proposed_by="test")
        approval = self.integrity.approve_change(
            self.victim, approved_by="Real Human", reason="the new rule is intended",
            acknowledge=tuple(review["weakening_findings"]), run_tests=False)
        self.assertTrue(approval["approved"], approval)
        self.assertEqual(self.integrity.baseline()[str(self.victim)],
                         digest_of(self.victim))
        self.assertTrue(self.integrity.snapshot_for(self.victim),
                        "an approved content must be snapshotted so it can be recovered")
        self.assertTrue([r for r in self.integrity.events(limit=20) if r["event"] == "change_approved"])

    def test_an_unprotected_path_cannot_be_approved(self) -> None:
        plain = self.home.dir / "not-a-component.txt"
        plain.write_text("x", encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.integrity.approve_change(plain, approved_by="Real Human", reason="why not",
                                          run_tests=False)

    def test_a_rejection_touches_no_file(self) -> None:
        before = self.victim.read_text(encoding="utf-8")
        result = self.integrity.reject_change(self.victim, rejected_by="Real Human",
                                             reason="not acceptable")
        self.assertTrue(result["rejected"])
        self.assertEqual(self.victim.read_text(encoding="utf-8"), before)
        self.assertTrue([r for r in self.integrity.events(limit=20) if r["event"] == "change_decision"])


class RecoveryTests(IntegrityFixture):
    def setUp(self) -> None:
        super().setUp()
        self.integrity.protect(approved_by="Test Human", force=True)
        self.original = self.victim.read_text(encoding="utf-8")
        self.tamper()

    def test_recovery_needs_a_named_human(self) -> None:
        with self.assertRaises(PermissionError):
            self.integrity.restore(self.victim, approved_by="anonymous", reason="just fix it")

    def test_unprotected_paths_are_not_recoverable(self) -> None:
        plain = self.home.dir / "user-data.txt"
        plain.write_text("important", encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.integrity.restore(plain, approved_by="Real Human", reason="oops")

    def test_recovery_restores_the_bytes_and_verifies_them(self) -> None:
        result = self.integrity.restore(self.victim, approved_by="Real Human",
                                        reason="restore the reviewed content")
        self.assertTrue(result["restored"])
        self.assertEqual(self.victim.read_text(encoding="utf-8"), self.original)
        self.assertEqual(result["digest"], digest_of(self.victim))
        self.assertEqual(self.integrity.inspect(record=False)["status"], "ok")
        events = [row["event"] for row in self.integrity.events(limit=10)]
        self.assertIn("recovery_performed", events)

    def test_a_second_recovery_says_the_file_already_matches(self) -> None:
        self.integrity.restore(self.victim, approved_by="Real Human", reason="first")
        again = self.integrity.restore(self.victim, approved_by="Real Human", reason="again")
        self.assertFalse(again["restored"])
        self.assertIn("already matches", again["reason"])


class EnforcementProbeTests(IntegrityFixture):
    """The probes run in a separate interpreter, in their own home, on a copy."""

    def test_the_probes_pass_on_the_repository_as_it_stands(self) -> None:
        result = self.integrity.verify_enforcement(run_tests=False)
        self.assertTrue(result["ok"], result["probe"]["failures"])
        names = {probe["name"] for probe in result["probe"]["probes"]}
        self.assertIn("protected_write_refused", names)
        self.assertIn("integrity_detects_change", names)
        self.assertIn("the_record_is_append_only", names)
        self.assertEqual(result["probe"]["failures"], [])
        self.assertGreaterEqual(len(names), 10)

    def test_the_probes_run_outside_the_home_they_are_checking(self) -> None:
        """A probe that depended on this home's baseline would pass by arrangement."""

        result = self.integrity.verify_enforcement(run_tests=False)
        baseline = next(p for p in result["probe"]["probes"]
                        if p["name"] == "baseline_recorded")
        self.assertTrue(baseline["passed"], baseline["detail"])
        self.assertNotIn(str(self.home.dir), json.dumps(result["probe"]))

    def test_a_weakened_control_is_caught_and_recorded(self) -> None:
        target = REPO_ROOT / "livium" / "integrity.py"
        source = target.read_text(encoding="utf-8")
        anchor = "        known = components(self.repo_root)"
        self.assertIn(anchor, source, "the anchor moved; this test would prove nothing")
        # The candidate stops watching the policy's components: detection stops working
        # while the file still imports and runs.
        doctored = source.replace(anchor, "        return []  # weakened by the test")

        result = self.integrity.verify_enforcement(
            candidate_files={target: doctored}, run_tests=False)
        self.assertFalse(result["ok"], "a weakened control passed the enforcement probes")
        self.assertTrue(result["probe"]["failures"])
        self.assertIn("integrity_detects_change",
                      json.dumps(result["probe"]["failures"]))
        recorded = [row["event"] for row in self.integrity.events(limit=10)]
        self.assertIn("enforcement_verification_failed", recorded)

    def test_a_passing_verification_is_recorded_as_such(self) -> None:
        self.integrity.verify_enforcement(
            candidate_files={REPO_ROOT / "livium" / "integrity.py":
                             (REPO_ROOT / "livium" / "integrity.py").read_text(encoding="utf-8")},
            run_tests=False)
        recorded = [row["event"] for row in self.integrity.events(limit=10)]
        self.assertIn("enforcement_verified", recorded)


class SummariesTests(IntegrityFixture):
    def test_the_summary_separates_what_is_known_from_what_is_not(self) -> None:
        self.integrity.protect(approved_by="Test Human", force=True)
        self.tamper()
        self.integrity.inspect(record=True)
        summary = self.integrity.enforcement_summary()
        self.assertGreaterEqual(summary["detections"], 1)
        self.assertTrue(summary["unresolved_detections"], summary)
        self.assertIn("not a prevention", summary["note"])
        self.assertTrue(self.integrity.incidents(limit=5))

    def test_a_baseline_records_the_human_who_authorised_it(self) -> None:
        result = self.integrity.protect(approved_by="Test Human", force=True)
        self.assertEqual(result["approved_by"], "Test Human")
        recorded = [row for row in self.integrity.events(limit=10)
                    if row["event"] == "baseline_recorded"]
        self.assertTrue(recorded)
        self.assertEqual(recorded[0]["authorization"], "Test Human")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
