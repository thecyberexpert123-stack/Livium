"""The deterministic task router (mission section 3).

These tests pin the *control* decisions: which of the eleven routes a piece of
observed state produces, that the ordering between them is what makes the run
safe, and that the tool selection only ever offers interfaces that exist and fit.
No model is involved here at all -- that is the point of the router.
"""

from __future__ import annotations

import unittest

from support import LiviumTestCase

from livium.router import (
    CHANGE_APPROACH, EXECUTE, INSPECT_RESULT, INVESTIGATE_FAILURE, INVESTIGATE_GAP,
    MAX_EXPOSED_TOOLS, META_TOOLS, PERSIST, REQUEST_AUTHORIZATION, RETRIEVE_CONTEXT,
    RETRY, SELECT_CAPABILITY, StepContext, TaskRouter, VERIFY, classify_failure,
    select_tools, signature_of,
)

#: Stand-in for the registry's tool names. Kept as a literal so this module stays
#: pure control logic with no configuration or filesystem; `tests/test_hygiene.py`
#: asserts it lists every tool the registry actually declares, so it cannot silently
#: fall behind and start testing a tool set that no longer exists.
AVAILABLE = ("read_file", "list_dir", "write_file", "run_command", "sha256",
             "memory_search", "memory_write", "memory_recent", "memory_update",
             "memory_forget",
             "request_capability", "tool_info", "finish_task",
             "record_discovery", "discovery_update")


def context(**overrides) -> StepContext:
    base = dict(
        task={"id": "t", "objective": "read the notes and report the total"},
        step=1, max_steps=12, available_tools=AVAILABLE,
    )
    base.update(overrides)
    return StepContext(**base)


class RuleOrderTests(LiviumTestCase):
    """Ordering is the safety property, not an implementation detail."""

    def setUp(self) -> None:
        self.router = TaskRouter(max_retries_per_tool=2)

    def test_a_frozen_guardian_outranks_everything_else(self) -> None:
        decision = self.router.route(context(frozen=True, freeze_reason="tampered", finish_requested=True))
        self.assertEqual(decision.decision, PERSIST)
        self.assertFalse(decision.requires_model)
        self.assertIn("frozen", decision.reason)

    def test_an_unconfirmed_side_effect_outranks_finishing(self) -> None:
        # A task must not be closed while a write-ahead intent is still unexplained.
        decision = self.router.route(context(pending_intents=1, finish_requested=True))
        self.assertEqual(decision.decision, INSPECT_RESULT)
        self.assertFalse(decision.requires_model)

    def test_finishing_verifies_rather_than_believing(self) -> None:
        decision = self.router.route(context(finish_requested=True))
        self.assertEqual(decision.decision, VERIFY)
        self.assertIn("harness decides", decision.reason)
        self.assertFalse(decision.requires_model)

    def test_a_denial_requiring_approval_asks_a_human_instead_of_retrying(self) -> None:
        decision = self.router.route(context(denials=[{
            "tool": "write_file", "reason": "inside an approval root",
            "approval_required": True}]))
        self.assertEqual(decision.decision, REQUEST_AUTHORIZATION)
        self.assertEqual(decision.tool_names, ("write_file",))
        self.assertFalse(decision.requires_model)

    def test_a_plain_denial_changes_approach_and_never_retries(self) -> None:
        decision = self.router.route(context(denials=[{
            "tool": "run_command", "reason": "binary not allowed", "approval_required": False}]))
        self.assertEqual(decision.decision, CHANGE_APPROACH)
        self.assertIn("cannot succeed", decision.reason)
        self.assertTrue(decision.tool_names, "a new approach still needs interfaces")

    def test_repeated_identical_calls_force_a_change_of_approach(self) -> None:
        signature = signature_of("read_file", {"path": "notes.md"})
        decision = self.router.route(context(repeats={signature: 2}))
        self.assertEqual(decision.decision, CHANGE_APPROACH)
        self.assertEqual(decision.evidence["count"], 2)

    def test_a_text_only_reply_widens_retrieval(self) -> None:
        decision = self.router.route(context(text_only_reply=True))
        self.assertEqual(decision.decision, RETRIEVE_CONTEXT)

    def test_the_default_is_to_execute_with_a_bounded_interface_set(self) -> None:
        decision = self.router.route(context())
        self.assertEqual(decision.decision, EXECUTE)
        self.assertLessEqual(len(decision.tool_names), MAX_EXPOSED_TOOLS)
        self.assertEqual(decision.evidence["available"], len(AVAILABLE))

    def test_the_last_step_says_so_rather_than_implying_the_run_continues(self) -> None:
        decision = self.router.route(context(step=12, max_steps=12))
        self.assertEqual(decision.decision, EXECUTE)
        self.assertTrue(decision.evidence.get("wrap_up"))
        self.assertIn("last step", decision.reason)


class FailureHandlingTests(LiviumTestCase):
    def setUp(self) -> None:
        self.router = TaskRouter(max_retries_per_tool=2)

    def failure(self, classification: str, tool: str = "run_command") -> dict:
        return {"tool": tool, "classification": classification, "error": "boom",
                "output": "boom happened"}

    def test_a_transient_failure_is_retried_up_to_the_configured_limit(self) -> None:
        first = self.router.route(context(failures=[self.failure("transient")]))
        second = self.router.route(context(failures=[self.failure("transient")],
                                          retries={"run_command": 1}))
        third = self.router.route(context(failures=[self.failure("transient")],
                                         retries={"run_command": 2}))
        self.assertEqual(first.decision, RETRY)
        self.assertEqual(second.decision, RETRY)
        self.assertEqual(second.evidence["attempt"], 2)
        self.assertEqual(third.decision, INVESTIGATE_FAILURE,
                         "the retry budget must be finite and then the failure is examined")

    def test_a_defect_is_not_retried_it_is_investigated(self) -> None:
        decision = self.router.route(context(failures=[self.failure("command_failed")]))
        self.assertEqual(decision.decision, INVESTIGATE_FAILURE)
        self.assertIn("run_command", decision.tool_names)

    def test_investigating_twice_is_not_allowed_to_become_the_loop(self) -> None:
        decision = self.router.route(context(
            failures=[self.failure("command_failed")],
            investigated_tools=("run_command",)))
        self.assertEqual(decision.decision, CHANGE_APPROACH)


class GapTests(LiviumTestCase):
    def setUp(self) -> None:
        self.router = TaskRouter()

    def test_a_gap_with_a_matching_capability_selects_it_rather_than_building(self) -> None:
        decision = self.router.route(context(
            gap_evidence={"tool": "no_such_tool", "classification": "unknown_tool"},
            capability_candidates=[{"name": "memory_recent", "tools": ["memory_recent"]}]))
        self.assertEqual(decision.decision, SELECT_CAPABILITY)
        self.assertEqual(decision.tool_names, ("memory_recent",))

    def test_a_gap_without_a_candidate_runs_the_reuse_ladder_first(self) -> None:
        decision = self.router.route(context(
            gap_evidence={"tool": "no_such_tool", "classification": "unknown_tool"}))
        self.assertEqual(decision.decision, INVESTIGATE_GAP)
        self.assertIn("reuse", decision.reason)
        self.assertIn("request_capability", decision.tool_names,
                      "the model still needs a way to act on the ladder's result")

    def test_no_gap_signal_means_no_gap_investigation(self) -> None:
        decision = self.router.route(context())
        self.assertEqual(decision.decision, EXECUTE)


class ToolSelectionTests(LiviumTestCase):
    def test_selection_follows_the_words_of_the_objective(self) -> None:
        chosen = select_tools("run the test suite and check the output", available=AVAILABLE)
        self.assertIn("run_command", chosen)
        self.assertIn("read_file", chosen)

    def test_meta_tools_are_always_offered_so_the_model_can_ask(self) -> None:
        chosen = select_tools("do something entirely unanticipated", available=AVAILABLE)
        for name in META_TOOLS:
            self.assertIn(name, chosen)

    def test_the_interface_set_stays_bounded(self) -> None:
        chosen = select_tools(
            "read list write run execute test build install hash checksum memory recall "
            "capability tool gap forget",
            available=AVAILABLE)
        self.assertLessEqual(len(chosen), MAX_EXPOSED_TOOLS)
        for name in META_TOOLS:
            self.assertIn(name, chosen, "the bound must not squeeze out the meta tools")

    def test_names_that_do_not_exist_are_never_offered(self) -> None:
        chosen = select_tools("read the notes", available=AVAILABLE,
                              forced=("write_file", "not_a_real_tool"))
        self.assertIn("write_file", chosen)
        self.assertNotIn("not_a_real_tool", chosen)

    def test_continuity_exposes_what_the_run_already_used(self) -> None:
        chosen = select_tools("", available=AVAILABLE, used=("sha256",))
        self.assertIn("sha256", chosen)


class FailureClassificationTests(LiviumTestCase):
    """Classification decides whether an identical retry can help. It must not guess."""

    def test_unknown_tool_is_its_own_class(self) -> None:
        from livium.tools import ToolResult
        result = ToolResult(tool="nope", ok=False, output="unknown tool 'nope'",
                            error="unknown_tool", category="dispatch")
        self.assertEqual(classify_failure("nope", result, known_tools=("read_file",)),
                         "unknown_tool")

    def test_argument_errors_are_invalid_input(self) -> None:
        from livium.tools import ToolResult
        result = ToolResult(tool="read_file", ok=False, output="path must be a string",
                            error="bad_arguments", category="dispatch")
        self.assertEqual(classify_failure("read_file", result, known_tools=("read_file",)),
                         "invalid_input")

    def test_a_non_zero_exit_is_a_command_failure(self) -> None:
        from livium.tools import ToolResult
        result = ToolResult(tool="run_command", ok=False, output="exit 2",
                            error="exit_2", category="execution", data={"exit_code": 2})
        self.assertEqual(classify_failure("run_command", result, known_tools=("run_command",)),
                         "command_failed")

    def test_only_the_retryable_classes_are_retryable(self) -> None:
        from livium.router import RETRYABLE, failure_is_retryable
        self.assertTrue(failure_is_retryable("transient"))
        self.assertTrue(failure_is_retryable("environment"))
        for classification in ("invalid_input", "missing_permissions", "command_failed",
                               "unknown_tool", "undetermined"):
            self.assertFalse(failure_is_retryable(classification),
                             f"{classification} must not be retried unchanged")
        self.assertEqual(set(RETRYABLE), {"transient", "environment"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
