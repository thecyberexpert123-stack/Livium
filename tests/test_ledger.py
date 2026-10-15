"""The learning-efficiency ledger (mission section 9).

The ledger's whole value is that its numbers are *measured*. These tests are mostly
about the difference between a measured zero, an absent measurement, and an
estimate -- three things that must never be conflated.
"""

from __future__ import annotations

import json
import unittest

from support import LiviumTestCase

from livium.ledger import DEFAULT_CATEGORY, Ledger, classify, evidence_for_experience


class RecordTests(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.store()
        self.ledger = Ledger(self.store)

    def record(self, **overrides):
        kwargs = dict(task_id="t-1", category="file-production", objective="do a thing",
                      tools_exposed=5, tools_available=12)
        kwargs.update(overrides)
        return self.ledger.record(**kwargs)

    def test_a_row_is_stored_and_read_back_with_its_counts(self) -> None:
        row_id = self.record(model_calls=3, tools_executed=4, retries=1, failures=2, denials=1)
        rows = self.ledger.rows(limit=5)
        self.assertEqual(rows[0].task_id, "t-1")
        self.assertEqual(row_id, 1)
        self.assertEqual(rows[0].model_calls, 3)
        self.assertEqual(rows[0].failures, 2)
        self.assertEqual(rows[0].approvals_requested, 0)
        self.assertEqual(rows[0].created_at, self.store.one(
            "SELECT created_at FROM ledger WHERE id=1")["created_at"])

    def test_missing_usage_stays_null_and_is_never_recorded_as_zero(self) -> None:
        self.record()
        row = self.ledger.rows(limit=1)[0]
        self.assertIsNone(row.prompt_tokens_actual)
        self.assertIsNone(row.completion_tokens_actual)
        self.assertIsNone(row.total_tokens_actual)
        self.assertEqual(row.usage_reported_calls, 0)

    def test_actual_and_estimated_tokens_are_kept_apart(self) -> None:
        self.record(prompt_tokens_actual=1200, completion_tokens_actual=300,
                    usage_reported_calls=2, tokens_estimated=5000)
        row = self.ledger.rows(limit=1)[0]
        self.assertEqual(row.total_tokens_actual, 1500)
        self.assertEqual(row.tokens_estimated, 5000)
        self.assertNotEqual(row.total_tokens_actual, row.tokens_estimated)

    def test_only_prompt_lengths_are_stored_never_prompt_contents(self) -> None:
        secret = "LIVIUM_API_KEY=sk-do-not-store-me"
        self.record(objective=f"find {secret}")
        raw = self.store.one("SELECT * FROM ledger WHERE id=1")
        blob = json.dumps({k: raw[k] for k in raw.keys()}, default=str)
        self.assertNotIn(secret, blob, "the ledger must not be a second copy of the prompt")

    def test_the_summary_separates_covered_actuals_from_estimates(self) -> None:
        self.record(task_id="t-1", prompt_tokens_actual=100, completion_tokens_actual=50,
                    usage_reported_calls=1, tokens_estimated=900)
        self.record(task_id="t-2", tokens_estimated=1000)
        summary = self.ledger.summary()
        bucket = summary["categories"]["file-production"]
        self.assertEqual(bucket["tasks"], 2)
        self.assertEqual(bucket["actual_tokens"], 150)
        self.assertEqual(bucket["actual_tokens_covered"], 1,
                         "one of the two tasks has a real measurement, not two")
        self.assertEqual(bucket["estimated_tokens"], 1900)

    def test_an_empty_ledger_says_so_rather_than_reporting_zeros(self) -> None:
        summary = self.ledger.summary()
        self.assertEqual(summary["tasks"], 0)
        self.assertIn("no task has been recorded", summary["note"])

    def test_route_decisions_and_experience_evidence_are_stored_for_audit(self) -> None:
        self.record(
            route_decisions=[{"step": 1, "decision": "execute"}, {"step": 2, "decision": "verify"}],
            experience_influenced=True,
            experience_evidence={"reuse": [{"memory_id": 3, "tool": "memory_recent"}],
                                 "avoided_repetition": []})
        raw = self.store.one("SELECT route_decisions, experience_evidence FROM ledger WHERE id=1")
        self.assertEqual(json.loads(raw["route_decisions"])[1]["decision"], "verify")
        self.assertEqual(json.loads(raw["experience_evidence"])["reuse"][0]["memory_id"], 3)


class ClassificationTests(LiviumTestCase):
    def test_the_category_comes_from_the_tools_that_ran(self) -> None:
        self.assertEqual(classify(["write_file"]), "file-production")
        self.assertEqual(classify(["run_command"]), "command-execution")
        self.assertEqual(classify(["memory_search"]), "recall")
        self.assertEqual(classify([]), DEFAULT_CATEGORY)


class ExperienceEvidenceTests(LiviumTestCase):
    """`experience_influenced` must be a claim with evidence, not a feeling."""

    def test_reuse_requires_the_injected_record_to_name_the_tool_that_ran(self) -> None:
        influenced, evidence = evidence_for_experience(
            injected=[{"id": 1, "kind": "capability", "key": "memory_recent",
                       "content": "use memory_recent to see recent records", "tags": []}],
            tools_executed=["memory_recent"], failed_tools=[], route_decisions=[])
        self.assertTrue(influenced)
        self.assertEqual(evidence["reuse"][0]["memory_id"], 1)
        self.assertEqual(evidence["reuse"][0]["tools"], ["memory_recent"])

    def test_a_record_that_was_injected_but_not_used_does_not_count(self) -> None:
        influenced, evidence = evidence_for_experience(
            injected=[{"id": 1, "kind": "capability", "key": "memory_recent",
                       "content": "use memory_recent", "tags": []}],
            tools_executed=["write_file"], failed_tools=[], route_decisions=[])
        self.assertFalse(influenced)
        self.assertEqual(evidence["reuse"], [])

    def test_nothing_injected_means_no_influence_claim(self) -> None:
        influenced, evidence = evidence_for_experience(
            injected=[], tools_executed=["write_file"], failed_tools=[], route_decisions=[])
        self.assertFalse(influenced)
        self.assertEqual(evidence, {"reuse": [], "avoided_repetition": []})

    def test_a_failure_record_that_changed_the_route_counts_as_avoided_repetition(self) -> None:
        influenced, evidence = evidence_for_experience(
            injected=[{"id": 7, "kind": "failure",
                       "content": "run_command failed: the binary was not on the allowlist",
                       "key": None, "tags": []}],
            tools_executed=[], failed_tools=["run_command"],
            route_decisions=[{"decision": "change_approach",
                              "evidence": {"tool": "run_command"}}])
        self.assertTrue(influenced)
        self.assertEqual(evidence["avoided_repetition"][0]["memory_id"], 7)
        self.assertEqual(evidence["avoided_repetition"][0]["tool"], "run_command")

    def test_the_same_failure_without_a_recorded_route_is_not_claimed_as_learning(self) -> None:
        influenced, _ = evidence_for_experience(
            injected=[{"id": 7, "kind": "failure", "content": "run_command failed",
                       "key": None, "tags": []}],
            tools_executed=[], failed_tools=["run_command"], route_decisions=[])
        self.assertFalse(influenced)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
