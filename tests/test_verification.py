"""Acceptance verification must reflect the real world, including when it fails."""

from __future__ import annotations

import json
import unittest

from support import LiviumTestCase

from livium.execution import Runner
from livium.memory import Memory
from livium.permissions import PermissionEngine
from livium.store import Store
from livium.verification import PASS, ERROR, FAIL, MANUAL, Verifier, normalize_criteria, summarize


class VerifierFixture(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.paths.db)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.verifier = Verifier(self.cfg, PermissionEngine(self.cfg), self.store, self.memory,
                                 Runner(max_seconds=20))

    def write(self, name: str, text: str):
        path = self.cfg.paths.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class FileCriterionTests(VerifierFixture):
    def test_file_exists_passes_only_when_it_really_exists(self) -> None:
        [result] = self.verifier.verify([{"kind": "file_exists", "path": "made.txt"}])
        self.assertEqual(result.outcome, FAIL)
        self.write("made.txt", "content")
        [result] = self.verifier.verify([{"kind": "file_exists", "path": "made.txt"}])
        self.assertEqual(result.outcome, PASS)

    def test_file_contains_checks_actual_content(self) -> None:
        self.write("report.md", "# Report\n\ntotal 42 items\n")
        [hit] = self.verifier.verify(
            [{"kind": "file_contains", "path": "report.md", "text": "total 42 items"}])
        self.assertEqual(hit.outcome, PASS)
        self.assertIn("line 3", hit.evidence)
        [miss] = self.verifier.verify(
            [{"kind": "file_contains", "path": "report.md", "text": "total 43 items"}])
        self.assertEqual(miss.outcome, FAIL)

    def test_file_matches_uses_a_real_regex(self) -> None:
        self.write("numbers.txt", "alpha\nbeta 7\ngamma\n")
        [result] = self.verifier.verify(
            [{"kind": "file_matches", "path": "numbers.txt", "regex": r"^beta \d+$"}])
        self.assertEqual(result.outcome, PASS)
        [bad] = self.verifier.verify(
            [{"kind": "file_matches", "path": "numbers.txt", "regex": r"^delta \d+$"}])
        self.assertEqual(bad.outcome, FAIL)

    def test_file_absent(self) -> None:
        self.write("scratch.tmp", "x")
        [present] = self.verifier.verify([{"kind": "file_absent", "path": "scratch.tmp"}])
        self.assertEqual(present.outcome, FAIL)
        [absent] = self.verifier.verify([{"kind": "file_absent", "path": "other.tmp"}])
        self.assertEqual(absent.outcome, PASS)

    def test_criterion_cannot_read_outside_permitted_roots(self) -> None:
        [result] = self.verifier.verify([{"kind": "file_exists", "path": "/etc/shadow"}])
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("blocked by permissions", result.detail)


class CommandCriterionTests(VerifierFixture):
    def test_exit_code_and_stdout_are_both_checked(self) -> None:
        self.write("data.txt", "one\ntwo\nthree\n")
        [passing] = self.verifier.verify([{
            "kind": "command", "argv": ["wc", "-l", "data.txt"], "expect_exit": 0,
            "expect_stdout_contains": "3",
        }])
        self.assertEqual(passing.outcome, PASS)
        self.assertIn("exit=0", passing.evidence)

        [failing] = self.verifier.verify([{
            "kind": "command", "argv": ["wc", "-l", "data.txt"], "expect_exit": 99,
        }])
        self.assertEqual(failing.outcome, FAIL)
        self.assertIn("expected exit 99", failing.detail)

    def test_missing_stdout_match_fails(self) -> None:
        [result] = self.verifier.verify([{
            "kind": "command", "argv": ["echo", "hello"], "expect_stdout_contains": "goodbye",
        }])
        self.assertEqual(result.outcome, FAIL)

    def test_timeout_is_a_failure_not_a_pass(self) -> None:
        [result] = self.verifier.verify([{
            "kind": "command", "argv": ["python3", "-c", "import time; time.sleep(20)"],
            "expect_exit": 0, "timeout_seconds": 2,
        }])
        self.assertEqual(result.outcome, FAIL)
        self.assertIn("timed out", result.detail)

    def test_a_denied_verification_command_is_an_error_not_a_pass(self) -> None:
        [result] = self.verifier.verify([{"kind": "command", "argv": ["rm", "-rf", "/"]}])
        self.assertEqual(result.outcome, ERROR)
        self.assertIn("blocked by policy", result.detail)


class MemoryAndEventCriteriaTests(VerifierFixture):
    def test_memory_criterion_reads_real_rows(self) -> None:
        [missing] = self.verifier.verify(
            [{"kind": "memory", "mem_kind": "decision", "key": "storage"}])
        self.assertEqual(missing.outcome, FAIL)
        self.memory.remember("decision", "storage will be SQLite", key="storage",
                            verification="verified")
        [present] = self.verifier.verify(
            [{"kind": "memory", "mem_kind": "decision", "key": "storage"}])
        self.assertEqual(present.outcome, PASS)
        self.assertIn("mem:", present.detail)

    def test_event_criterion_reads_the_journal(self) -> None:
        [before] = self.verifier.verify([{"kind": "event", "event_type": "capability_promoted"}])
        self.assertEqual(before.outcome, FAIL)
        self.store.append_event("capability_promoted", {"name": "x"})
        [after] = self.verifier.verify([{"kind": "event", "event_type": "capability_promoted"}])
        self.assertEqual(after.outcome, PASS)


class HonestyTests(VerifierFixture):
    def test_manual_criteria_never_count_as_verified(self) -> None:
        results = self.verifier.verify([
            {"kind": "file_exists", "path": "nope.txt"},
            {"kind": "manual", "description": "the prose reads well"},
        ])
        summary = summarize(results)
        self.assertEqual(summary["state"], "not verified")
        self.assertEqual(summary["counts"].get(MANUAL), 1)
        self.assertEqual(len(summary["remaining"]), 2)

    def test_no_criteria_is_reported_as_unverified_not_as_success(self) -> None:
        summary = summarize([])
        self.assertEqual(summary["state"], "unverified (no acceptance criteria were defined)")
        self.assertEqual(summary["passed"], 0)

    def test_all_passing_is_verified(self) -> None:
        self.write("a.txt", "x")
        summary = summarize(self.verifier.verify([{"kind": "file_exists", "path": "a.txt"}]))
        self.assertEqual(summary["state"], "verified")

    def test_partial_state_is_distinguished_from_both_extremes(self) -> None:
        self.write("a.txt", "x")
        summary = summarize(self.verifier.verify([
            {"kind": "file_exists", "path": "a.txt"},
            {"kind": "file_exists", "path": "missing.txt"},
        ]))
        self.assertEqual(summary["state"], "partially verified")

    def test_unknown_criterion_kind_is_an_error(self) -> None:
        [result] = self.verifier.verify([{"kind": "vibes", "path": "x"}])
        self.assertEqual(result.outcome, ERROR)

    def test_malformed_criteria_are_rejected_loudly(self) -> None:
        with self.assertRaises(ValueError):
            normalize_criteria("[not json")
        with self.assertRaises(ValueError):
            normalize_criteria([{"no_kind": True}])
        with self.assertRaises(ValueError):
            normalize_criteria("{}")
        self.assertEqual(normalize_criteria(None), [])
        self.assertEqual(len(normalize_criteria('[{"kind":"manual"}]')), 1)

    def test_results_are_persisted_for_audit(self) -> None:
        self.write("a.txt", "x")
        self.verifier.verify([{"kind": "file_exists", "path": "a.txt"}], task_id="t-verify")
        rows = self.store.query("SELECT * FROM verification WHERE task_id='t-verify'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], PASS)
        self.assertEqual(json.loads(rows[0]["spec"])["path"], "a.txt")
        self.assertTrue(self.store.events(task_id="t-verify", event_type="task_verified"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
