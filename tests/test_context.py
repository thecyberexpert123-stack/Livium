"""Context management: priority, budgeting, omission reporting, recovery."""

from __future__ import annotations

import json
import unittest

from support import LiviumTestCase

from livium.capabilities import CapabilityRegistry
from livium.context import ContextManager, TokenBudget
from livium.guardian import GrowthGuardian
from livium.memory import Memory
from livium.permissions import PermissionEngine
from livium.store import Store


class ContextFixture(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.paths.db)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.memory.seed_foundational(self.cfg.paths.prompts)
        self.engine = PermissionEngine(self.cfg)
        self.guardian = GrowthGuardian(self.cfg, self.engine, self.store, self.memory)
        self.capabilities = CapabilityRegistry(self.store, lab=self.cfg.paths.capability_lab)
        self.manager = ContextManager(self.cfg, self.store, self.memory, self.capabilities,
                                     self.guardian)
        self.manager.set_tool_names(("read_file", "write_file"))

    def task(self, objective="summarise the README into notes.md", criteria=("file_exists notes.md",)):
        self.store.execute(
            "INSERT INTO task(id, objective, acceptance, status, created_at, updated_at) "
            "VALUES('t-ctx',?,?,'active','now','now')",
            (objective, json.dumps(list(criteria))),
        )
        return {"id": "t-ctx", "objective": objective, "acceptance": list(criteria),
                "status": "active", "next_action": "write the file", "summary": None}

    def build(self, **kwargs):
        kwargs.setdefault("observations", [])
        kwargs.setdefault("step", 1)
        return self.manager.build(task=kwargs.pop("task", None) or self.task(), **kwargs)


class RelevanceTests(ContextFixture):
    def test_relevant_memory_is_included_and_irrelevant_memory_is_not(self) -> None:
        """Demonstration C's core assertion, at unit level."""

        self.memory.remember("decision", "notes.md must be written in the workspace root",
                            key="notes-location", verification="verified", confidence=0.9)
        for index in range(6):
            self.memory.remember(
                "experience",
                f"the aquarium pump filter needs replacing every {index} weeks in Kolkata",
                key=f"aquarium-{index}", confidence=0.9,
            )
        built = self.build()
        self.assertIn("notes.md must be written", built.prompt)
        self.assertNotIn("aquarium pump", built.prompt)
        recorded = self.store.events(event_type="memory_retrieved")
        self.assertTrue(recorded)

    def test_objective_and_criteria_are_always_present(self) -> None:
        built = self.build()
        self.assertIn("summarise the README", built.prompt)
        self.assertIn("file_exists notes.md", built.prompt)

    def test_protected_instructions_appear_in_the_system_block(self) -> None:
        built = self.build()
        self.assertIn("Never claim work you did not do", built.system)
        self.assertIn("before you build anything new", built.system.lower())
        self.assertIn("no shell", built.system.lower())

    def test_pending_intents_appear_as_unresolved_work(self) -> None:
        intent_id = self.store.record_intent("write_file", {"path": "/tmp/x", "bytes": 3},
                                            task_id="t-ctx")
        built = self.build(pending_intents=[{"id": intent_id, "tool": "write_file",
                                            "args": '{"path": "/tmp/x"}', "created_at": "now"}])
        self.assertIn("write-ahead intent log", built.prompt)
        self.assertIn(str(intent_id), built.prompt)

    def test_recent_real_operations_are_surfaced_as_evidence(self) -> None:
        self.store.append_event("tool_exec", {"argv": ["wc", "-l", "notes.md"], "exit_code": 0},
                                task_id="t-ctx")
        built = self.build()
        self.assertIn("tool_exec", built.prompt)
        self.assertIn("exit_code", built.prompt)


class BudgetTests(ContextFixture):
    def test_budget_respects_the_declared_window(self) -> None:
        config = self.config(LIVIUM_CONTEXT_WINDOW="8000", LIVIUM_MAX_OUTPUT_TOKENS="500",
                             LIVIUM_SAFETY_MARGIN_PCT="25")
        budget = TokenBudget(config, self.store)
        self.assertEqual(budget.window, 8000)
        self.assertEqual(budget.reserve, 500 + 2000)
        self.assertEqual(budget.available, 5500)
        self.assertIn("operator-declared", budget.window_source)

    def test_mandatory_sections_survive_a_tiny_budget(self) -> None:
        config = self.config(LIVIUM_CONTEXT_WINDOW="3000", LIVIUM_MAX_OUTPUT_TOKENS="200",
                             LIVIUM_SAFETY_MARGIN_PCT="10")
        manager = ContextManager(config, self.store, self.memory, self.capabilities, self.guardian)
        manager.set_tool_names(("read_file",))
        for index in range(40):
            self.memory.remember("experience", f"background noise item {index} " * 8,
                                key=f"noise-{index}")
        built = manager.build(task={"id": "t-ctx", "objective": "summarise the README into notes.md",
                                    "acceptance": ["file_exists notes.md"], "status": "active"},
                              step=2, observations=["write_file: wrote 10 bytes"])
        self.assertIn("summarise the README", built.prompt)
        self.assertIn("file_exists notes.md", built.prompt)
        self.assertIn("write_file: wrote 10 bytes", built.prompt)
        # And it must have stayed inside its own budget, counting the system block.
        self.assertLessEqual(built.report["tokens_estimated"], 3000)
        self.assertTrue(built.report["within_window"])
        self.assertGreater(built.report["system_tokens"], 0)

    def test_omissions_are_recorded_and_shown_not_silently_dropped(self) -> None:
        """Records that *are* relevant but do not fit must be visibly withheld.

        The records have to genuinely match the objective. An earlier version of
        this test used records that matched nothing, so retrieval correctly found
        nothing and the test measured the wrong thing -- it was passing judgement on
        a section that had no reason to exist.
        """

        config = self.config(LIVIUM_CONTEXT_WINDOW="4000", LIVIUM_MAX_OUTPUT_TOKENS="200")
        manager = ContextManager(config, self.store, self.memory, self.capabilities, self.guardian)
        manager.set_tool_names(("read_file",))
        for index in range(30):
            self.memory.remember(
                "experience", f"goal background record {index} " + "filler text " * 12,
                key=f"bg-{index}",
            )
        built = manager.build(task={"id": "t-ctx", "objective": "goal", "acceptance": [],
                                    "status": "active"}, step=1, observations=[])
        self.assertTrue(
            any("goal background record" in line for line in built.prompt.splitlines()),
            "retrieval should have found the matching records",
        )
        self.assertIn("omitted", built.prompt.lower())
        self.assertTrue(built.report["omitted"])
        self.assertTrue(any("omitted from this section" in line for line in built.prompt.splitlines()))

    def test_items_are_dropped_whole_never_cut_mid_record(self) -> None:
        config = self.config(LIVIUM_CONTEXT_WINDOW="3500", LIVIUM_MAX_OUTPUT_TOKENS="200")
        manager = ContextManager(config, self.store, self.memory, self.capabilities, self.guardian)
        manager.set_tool_names(("read_file",))
        marker = "SENTINEL-COMPLETE-RECORD"
        for index in range(12):
            self.memory.remember(
                "experience",
                f"{marker} record {index} " + "filler " * 30,
                key=f"whole-{index}",
            )
        built = manager.build(task={"id": "t-ctx", "objective": "goal", "acceptance": [],
                                    "status": "active"}, step=1, observations=[])
        for line in built.prompt.splitlines():
            if "filler" in line:
                self.assertIn(marker, line, "a record was rendered without its own beginning")
                self.assertGreater(len(line), 200)

    def test_context_report_row_is_written_for_every_construction(self) -> None:
        self.build()
        rows = self.store.query("SELECT * FROM context_report WHERE task_id='t-ctx'")
        self.assertEqual(len(rows), 1)
        budget = json.loads(rows[0]["budget"])
        self.assertIn("window_source", budget)
        self.assertGreater(rows[0]["estimated"], 0)

    def test_refuses_to_run_when_the_rules_cannot_fit(self) -> None:
        config = self.config(LIVIUM_CONTEXT_WINDOW="600", LIVIUM_MAX_OUTPUT_TOKENS="100")
        manager = ContextManager(config, self.store, self.memory, self.capabilities, self.guardian)
        with self.assertRaises(RuntimeError) as caught:
            manager.build(task={"id": "t", "objective": "x", "acceptance": [], "status": "active"},
                          step=1, observations=[])
        self.assertIn("rules", str(caught.exception))

    def test_calibration_uses_real_usage_only(self) -> None:
        budget = TokenBudget(self.cfg, self.store)
        before = budget.calibration
        budget.calibrate(1000, 0)          # no observation -> unchanged
        self.assertEqual(budget.calibration, before)
        budget.calibrate(0, 500)           # nothing estimated -> unchanged
        self.assertEqual(budget.calibration, before)
        budget.calibrate(1000, 2500)       # implausible ratio -> refused
        self.assertEqual(budget.calibration, before)
        budget.calibrate(1000, 1300)       # plausible -> blended
        self.assertNotEqual(budget.calibration, before)
        stored = self.store.get_meta("token_calibration")
        self.assertIn("factor", stored)
        reloaded = TokenBudget(self.cfg, self.store)
        self.assertEqual(reloaded.calibration, budget.calibration)

    def test_malformed_calibration_metadata_falls_back_without_blocking(self) -> None:
        self.store.set_meta("token_calibration", {"last_observed": 120})
        budget = TokenBudget(self.cfg, self.store)
        self.assertEqual(budget.calibration, 1.0)
        self.assertTrue(budget.calibration_warning)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
