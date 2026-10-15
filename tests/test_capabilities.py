"""Capability lifecycle: the stages must mean something.

Promotion is exercised against a temporary promoted directory, so a test run never
installs anything into the repository. The loader is the same code the agent uses
at startup.
"""

from __future__ import annotations

import pathlib
import unittest

from support import LiviumTestCase

from livium.capabilities import (
    CapabilityError,
    CapabilityRegistry,
    file_manifest,
    validate_capability_module,
)
from livium.guardian import GrowthGuardian
from livium.memory import Memory
from livium.permissions import PermissionEngine
from livium.promoted import load_promoted
from livium.store import Store

GOOD_MODULE = '''
"""A real capability: count the non-blank lines of a workspace file."""

from __future__ import annotations

CAPABILITY = {
    "name": "__NAME__",
    "requirement": "count non-blank lines without invoking a shell",
    "description": "Counts the substantive lines of a text file in the workspace.",
    "tools": [
        {
            "name": "__NAME___count",
            "description": "Count non-blank lines in a workspace file and report the number.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ],
}


def handle(tool_name, args, ctx):
    from livium.tools import ToolResult

    if tool_name != "__NAME___count":
        return ToolResult(tool=tool_name, ok=False, output="unknown tool", error="unknown_tool")
    path = str(args.get("path", ""))
    if not path:
        return ToolResult(tool=tool_name, ok=False, output="path is required",
                          error="bad_arguments")
    config = (ctx.services or {}).get("config")
    root = config.paths.workspace if config else None
    if root is None:
        return ToolResult(tool=tool_name, ok=False, output="no workspace available",
                          error="no_workspace")
    target = root / path
    if not target.is_file():
        return ToolResult(tool=tool_name, ok=False, output=f"no such file: {target}",
                          error="not_found")
    lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ToolResult(tool=tool_name, ok=True,
                      output=f"{len(lines)} non-blank line(s) in {path}",
                      data={"count": len(lines)})
'''

GOOD_TEST = '''
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import __NAME__  # noqa: E402


class FakeConfig:
    class Paths:
        workspace = pathlib.Path(__WORKSPACE__)
    paths = Paths()


class FakeContext:
    services = {"config": FakeConfig()}


class TestCount(unittest.TestCase):
    def setUp(self):
        pathlib.Path(__WORKSPACE__).mkdir(parents=True, exist_ok=True)

    def test_counts_only_substantive_lines(self):
        target = pathlib.Path(__WORKSPACE__) / "sample.txt"
        target.write_text("one\\n\\n   \\ntwo\\n", encoding="utf-8")
        result = __NAME__.handle("__NAME___count", {"path": "sample.txt"}, FakeContext())
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.data["count"], 2)

    def test_missing_path_is_an_error(self):
        result = __NAME__.handle("__NAME___count", {}, FakeContext())
        self.assertFalse(result.ok)
'''


class CapabilityFixture(LiviumTestCase):
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
        # repo_root points at the temp home, so livium/promoted is temporary.
        self.registry = CapabilityRegistry(
            self.store, repo_root=self.home.dir, lab=self.cfg.paths.capability_lab,
            promoted=self.cfg.paths.promoted,
        )

    def implement(self, name: str, module: str, test: str) -> None:
        directory = self.cfg.paths.capability_lab / name
        directory.mkdir(parents=True, exist_ok=True)
        workspace = str(self.cfg.paths.workspace)
        rendered_module = module.replace("__NAME__", name)
        rendered_test = (test.replace("__NAME__", name)
                         .replace("__WORKSPACE__", repr(workspace)))
        assert "__NAME__" not in rendered_module
        assert "__WORKSPACE__" not in rendered_test
        (directory / f"{name}.py").write_text(rendered_module, encoding="utf-8")
        (directory / f"test_{name}.py").write_text(rendered_test, encoding="utf-8")


class ProposalTests(CapabilityFixture):
    def test_proposal_creates_a_row_and_no_code(self) -> None:
        capability = self.registry.propose(
            name="line_count", requirement="count non-blank lines",
            reuse_analysis="wc needs a path and cannot count only non-blank lines",
        )
        self.assertEqual(capability.status, "proposed")
        self.assertIsNone(capability.implementation)
        self.assertFalse((self.cfg.paths.capability_lab / "line_count").exists())

    def test_scaffold_refuses_a_symlink_that_would_overwrite_outside_the_lab(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        lab = self.cfg.paths.capability_lab / "line_count"
        lab.mkdir(parents=True)
        outside = self.home.dir / "outside.py"
        outside.write_text("keep this\n")
        try:
            (lab / "line_count.py").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"this Windows account cannot create symlinks: {exc}")
        with self.assertRaisesRegex(CapabilityError, "not a regular lab file"):
            self.registry.scaffold("line_count")
        self.assertEqual(outside.read_text(), "keep this\n")
        self.assertEqual(self.registry.get("line_count").status, "proposed")

    def test_names_are_validated(self) -> None:
        for bad in ("Bad Name", "x", "9starts_with_digit", "a" * 60):
            with self.subTest(name=bad):
                with self.assertRaises(CapabilityError):
                    self.registry.propose(name=bad, requirement="r", reuse_analysis="a")

    def test_duplicate_proposal_for_a_validated_capability_is_refused(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE, GOOD_TEST)
        self.registry.run_tests("line_count")
        self.assertEqual(self.registry.get("line_count").status, "validated")
        with self.assertRaises(CapabilityError):
            self.registry.propose(name="line_count", requirement="r2", reuse_analysis="a")

    def test_a_scaffold_cannot_validate_itself(self) -> None:
        """Regression guard for a real defect.

        The generated placeholder tests passed and marked the skeleton 'validated',
        which would have invited a human to promote an unimplemented module. The
        harness now refuses to validate a module that still contains scaffold
        markers, whatever the tests report.
        """

        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        result = self.registry.run_tests("line_count")
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "experimental")
        self.assertIn("scaffold placeholders", result["evidence"])
        self.assertTrue(self.store.events(event_type="capability_test_refused"))
        # Deleting the placeholder test is not a way around it.
        (self.cfg.paths.capability_lab / "line_count" / "test_line_count.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        result = self.registry.run_tests("line_count")
        self.assertFalse(result["passed"])
        self.assertEqual(self.registry.get("line_count").status, "experimental")

    def test_status_is_not_trusted_until_tests_really_pass(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        status = self.registry.get("line_count").status
        self.assertEqual(status, "experimental")
        self.assertNotIn(status, ("validated", "active"))


class TestExecutionTests(CapabilityFixture):
    def test_failing_tests_leave_the_capability_experimental(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE,
                       GOOD_TEST + "\n\nclass TestBroken(unittest.TestCase):\n"
                                   "    def test_always_fails(self):\n"
                                   "        self.assertEqual(1, 2)\n")
        result = self.registry.run_tests("line_count")
        self.assertFalse(result["passed"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["status"], "experimental")
        self.assertIn("FAILED", self.registry.get("line_count").verification)

    def test_passing_tests_validate_and_record_raw_evidence(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE, GOOD_TEST)
        result = self.registry.run_tests("line_count")
        self.assertTrue(result["passed"], result["stderr"])
        capability = self.registry.get("line_count")
        self.assertEqual(capability.status, "validated")
        self.assertIn("exit_code=0", capability.evidence)
        self.assertIn("argv=", capability.evidence)
        self.assertGreater(capability.lines or 0, 10)

    def test_testing_an_unimplemented_capability_is_an_error_not_a_pass(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        with self.assertRaises(CapabilityError):
            self.registry.run_tests("line_count")

    def test_test_runner_uses_the_real_isolation_boundary(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE, GOOD_TEST)
        self.registry.run_tests("line_count")
        self.assertIn("network_isolated=", self.registry.get("line_count").evidence)


class PromotionTests(CapabilityFixture):
    def prepare_validated(self, name: str = "line_count") -> None:
        self.registry.propose(name=name, requirement="count non-blank lines",
                              reuse_analysis="no existing tool counts lines of content")
        self.registry.scaffold(name)
        self.implement(name, GOOD_MODULE, GOOD_TEST)
        self.registry.run_tests(name)
        self.guardian.record_baseline(approved_by="Test Human", force=True)

    def test_promotion_requires_tests_to_have_passed(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE, GOOD_TEST)
        # Tests exist but were never run.
        with self.assertRaises(CapabilityError) as caught:
            self.registry.promote("line_count", approved_by="Test Human",
                                  guardian_check=lambda: self.guardian.promotion_gate("line_count"))
        self.assertIn("not 'validated'", str(caught.exception))

    def test_promotion_refuses_agent_self_approval(self) -> None:
        self.prepare_validated()
        for identity in ("agent", "Livium", "self", "auto", "", "  \t  "):
            with self.subTest(approved_by=identity):
                with self.assertRaises(CapabilityError):
                    self.registry.promote("line_count", approved_by=identity,
                                          guardian_check=lambda: {"ok": True, "detail": "ok"})

    def test_promotion_is_blocked_when_guardian_integrity_fails(self) -> None:
        self.registry.propose(name="line_count", requirement="r", reuse_analysis="a")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE, GOOD_TEST)
        self.registry.run_tests("line_count")
        # No baseline recorded on purpose.
        with self.assertRaises(CapabilityError) as caught:
            self.registry.promote("line_count", approved_by="Test Human",
                                  guardian_check=lambda: self.guardian.promotion_gate("line_count"))
        self.assertIn("guardian integrity check failed", str(caught.exception))

    def test_promotion_copies_the_module_and_the_loader_picks_it_up(self) -> None:
        self.prepare_validated()
        capability = self.registry.promote(
            "line_count", approved_by="Test Human",
            guardian_check=lambda: self.guardian.promotion_gate("line_count"),
        )
        self.assertEqual(capability.status, "active")
        self.assertEqual(capability.promoted_by, "Test Human")
        promoted_file = self.registry.promoted_dir / "line_count" / "line_count.py"
        self.assertTrue(promoted_file.exists())

        tools = list(load_promoted(self.registry.promoted_dir,
                                  approved=self.registry.approved_modules()))
        self.assertEqual(len(tools), 1)
        spec, handler, capability_name = tools[0]
        self.assertEqual(spec["name"], "line_count_count")
        self.assertEqual(capability_name, "line_count")
        self.assertTrue(callable(handler))
        self.assertEqual(load_promoted.last_failures, [])
        self.assertTrue(self.store.events(event_type="capability_promoted"))

    def test_a_module_that_fails_validation_cannot_be_promoted(self) -> None:
        self.prepare_validated()
        broken = self.registry.lab / "line_count" / "line_count.py"
        broken.write_text("CAPABILITY = {}\n", encoding="utf-8")
        ok, detail = validate_capability_module(broken, expected_name="line_count")
        self.assertFalse(ok)
        self.assertIn("handle", detail)
        with self.assertRaises(CapabilityError):
            self.registry.promote("line_count", approved_by="Test Human",
                                  guardian_check=lambda: self.guardian.promotion_gate("line_count"))

    def test_named_human_deprecation_is_a_real_terminal_transition(self) -> None:
        self.prepare_validated()
        self.registry.promote("line_count", approved_by="Test Human",
                              guardian_check=lambda: self.guardian.promotion_gate("line_count"))
        self.registry.mark_used("line_count", tool="line_count_count", task_id="t-use")
        self.assertEqual(self.registry.get("line_count").status, "maintained")
        for identity in ("agent", "  \t  "):
            with self.subTest(approved_by=identity), self.assertRaisesRegex(CapabilityError, "named human"):
                self.registry.deprecate("line_count", approved_by=identity, reason="superseded")
        with self.assertRaisesRegex(CapabilityError, "needs a reason"):
            self.registry.deprecate("line_count", approved_by="Test Human", reason="  ")
        self.assertEqual(self.registry.get("line_count").status, "maintained")
        retired = self.registry.deprecate("line_count", approved_by="Test Human",
                                          reason="superseded by a clearer counter")
        self.assertEqual(retired.status, "deprecated")
        self.assertFalse((self.registry.promoted_dir / "line_count").exists())
        self.assertNotIn("line_count", self.registry.approved_modules())
        self.assertEqual(list(load_promoted(self.registry.promoted_dir,
                                             approved=self.registry.approved_modules())), [])
        event = self.store.events(event_type="capability_deprecated")[0]
        self.assertIn("superseded by a clearer counter", event["payload"])

    def test_rollback_removes_the_file_and_the_tool_disappears(self) -> None:
        self.prepare_validated()
        self.registry.promote("line_count", approved_by="Test Human",
                              guardian_check=lambda: self.guardian.promotion_gate("line_count"))
        self.assertTrue((self.registry.promoted_dir / "line_count" / "line_count.py").exists())

        rolled = self.registry.rollback("line_count", reason="regression found in review")
        self.assertEqual(rolled.status, "rollback")
        self.assertFalse((self.registry.promoted_dir / "line_count" / "line_count.py").exists())
        self.assertEqual(list(load_promoted(self.registry.promoted_dir,
                                  approved=self.registry.approved_modules())), [])
        event = self.store.events(event_type="capability_rollback")[0]
        self.assertIn("regression", event["payload"])


class LoaderRobustnessTests(CapabilityFixture):
    def test_a_broken_promoted_module_is_reported_not_ignored(self) -> None:
        promoted = self.registry.promoted_dir
        promoted.mkdir(parents=True, exist_ok=True)
        (promoted / "broken.py").write_text("def CAPABILITY(:\n", encoding="utf-8")
        # Even an explicitly allowlisted, hashed file cannot load if it is invalid.
        approved = {"broken": {"broken.py": __import__("hashlib").sha256(
            (promoted / "broken.py").read_bytes()).hexdigest()}}
        tools = list(load_promoted(promoted, approved=approved))
        self.assertEqual(tools, [])
        self.assertEqual(len(load_promoted.last_failures), 1)
        self.assertIn("SyntaxError", load_promoted.last_failures[0][1])

    def test_a_module_without_the_contract_is_rejected(self) -> None:
        promoted = self.registry.promoted_dir
        promoted.mkdir(parents=True, exist_ok=True)
        (promoted / "no_contract.py").write_text("VALUE = 1\n", encoding="utf-8")
        approved = {"no_contract": {"no_contract.py": __import__("hashlib").sha256(
            (promoted / "no_contract.py").read_bytes()).hexdigest()}}
        self.assertEqual(list(load_promoted(promoted, approved=approved)), [])
        self.assertIn("CAPABILITY", load_promoted.last_failures[0][1])

    def test_missing_directory_is_not_an_error(self) -> None:
        tools = list(load_promoted(self.home.dir / "does-not-exist"))
        self.assertEqual(tools, [])
        self.assertEqual(load_promoted.last_failures, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class AgentAuthoredCapabilityTests(CapabilityFixture):
    """The scripted double chooses calls; the tool/lab/CLI/load/use path is real.

    These tests say nothing about a model's ability to invent correct Python. They
    prove that, when a model *does* write real code and tests through the tools, the
    runtime can validate them without a human driving a hidden test subcommand.
    """

    def test_agent_writes_tests_validates_human_promotes_and_new_run_uses_it(self) -> None:
        from livium.agent import LiviumAgent

        name = "line_count"
        lab = self.cfg.paths.capability_lab / name
        module = GOOD_MODULE.replace("__NAME__", name)
        test_code = (GOOD_TEST.replace("__NAME__", name)
                     .replace("__WORKSPACE__", repr(str(self.cfg.paths.workspace))))
        script = [
            {"tool_calls": [{"name": "request_capability", "arguments": {
                "name": name, "requirement": "count non-blank lines",
                "reuse_analysis": "list_dir cannot count nonblank lines in the content of a file",
                "verification_plan": "test a two-line and a missing file"}}]},
            {"tool_calls": [{"name": "capability_scaffold", "arguments": {"name": name}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": str(lab / f"{name}.py"), "content": module,
                "purpose": "implement non-blank line counter"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": str(lab / f"test_{name}.py"), "content": test_code,
                "purpose": "test actual successful count and missing input"}}]},
            {"tool_calls": [{"name": "capability_test", "arguments": {"name": name}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "authored and validated line_count, awaiting human promotion",
                "status": "complete"}}]},
        ]
        first = LiviumAgent(self.cfg, script=script)
        self.addCleanup(first.close)
        first.start()
        first.guardian.record_baseline(approved_by="Test Human", force=True)
        result = first.run_task("build a line-count capability and test it",
                                [{"kind": "event", "event_type": "capability_tested"}])
        self.assertEqual(result.status, "complete", result.as_dict())
        self.assertEqual(result.verification["state"], "verified")
        candidate = self.registry.get(name)
        self.assertEqual(candidate.status, "validated")
        self.assertTrue(candidate.validated_manifest)
        self.assertEqual(first.capabilities.get(name).status, "validated")
        self.assertNotIn(f"{name}_count", first.tools.names(),
                         "validation must not activate the agent's own code")
        self.assertEqual(len(first.store.events(task_id=result.task_id,
                                                event_type="capability_tested")), 1)

        self.registry.promote(name, approved_by="Test Human",
                              guardian_check=lambda: first.guardian.promotion_gate(name))
        first.close()
        # The new process constructs a new tool registry and imports only the
        # approved, unchanged package. Calling its tool produces an observed count.
        second = LiviumAgent(self.cfg, script=[
            {"tool_calls": [{"name": f"{name}_count", "arguments": {"path": "sample.txt"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "counted actual content", "status": "complete"}}]},
        ])
        self.addCleanup(second.close)
        (self.cfg.paths.workspace / "sample.txt").write_text("one\n\n two \n")
        loaded = second.start()["capabilities_loaded"]
        self.assertIn(f"{name}_count", loaded["loaded_tools"])
        self.assertEqual(loaded["failures"], [])
        used = second.run_task("use the line-count capability",
                               [{"kind": "event", "event_type": "capability_tool_used"}])
        self.assertEqual(used.verification["state"], "verified")
        self.assertTrue(any("2 non-blank line" in observation for observation in used.observations))
        self.assertEqual(self.registry.get(name).status, "maintained",
                         "a successful real invocation moves Active to Maintained")
        self.registry.rollback(name, reason="operator withdrew the capability")
        third = LiviumAgent(self.cfg, script=[])
        self.addCleanup(third.close)
        self.assertNotIn(f"{name}_count", third.start()["capabilities_loaded"]["loaded_tools"])

    def test_promote_refuses_changes_to_tests_and_helpers_after_validation(self) -> None:
        self.registry.propose(name="line_count", requirement="count lines", reuse_analysis="not built in")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE, GOOD_TEST)
        result = self.registry.run_tests("line_count")
        self.assertTrue(result["passed"], result["stderr"])
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        test = self.registry.lab / "line_count" / "test_line_count.py"
        test.write_text(test.read_text() + "\n# changed since tests passed\n")
        with self.assertRaisesRegex(CapabilityError, "lab changed since its tests passed"):
            self.registry.promote("line_count", approved_by="Test Human",
                                  guardian_check=lambda: self.guardian.promotion_gate("line_count"))
        self.assertFalse((self.registry.promoted_dir / "line_count").exists())
        self.assertTrue(self.registry.run_tests("line_count")["passed"])
        source = self.registry.lab / "line_count" / "line_count.py"
        source.write_text(source.read_text() + "\n# changed implementation\n")
        with self.assertRaisesRegex(CapabilityError, "lab changed since its tests passed"):
            self.registry.promote("line_count", approved_by="Test Human",
                                  guardian_check=lambda: self.guardian.promotion_gate("line_count"))

    def test_loader_rejects_unapproved_and_tampered_code_before_importing_it(self) -> None:
        from livium.agent import LiviumAgent

        self.registry.propose(name="line_count", requirement="count lines", reuse_analysis="not built in")
        self.registry.scaffold("line_count")
        self.implement("line_count", GOOD_MODULE, GOOD_TEST)
        self.assertTrue(self.registry.run_tests("line_count")["passed"])
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        self.registry.promote("line_count", approved_by="Test Human",
                              guardian_check=lambda: self.guardian.promotion_gate("line_count"))
        installed = self.registry.promoted_dir / "line_count" / "line_count.py"
        sentinel = self.home.dir / "imported-the-tampered-file.txt"
        installed.write_text(f"import pathlib\npathlib.Path({str(sentinel)!r}).write_text('executed')\n"
                             + installed.read_text())
        agent = LiviumAgent(self.cfg, script=[])
        self.addCleanup(agent.close)
        diagnostics = agent.start()["capabilities_loaded"]
        self.assertEqual(diagnostics["loaded_tools"], [])
        self.assertIn("promoted files differ", " ".join(diagnostics["failures"]))
        self.assertFalse(sentinel.exists(), "unapproved code executed at import time")
        # A different valid module simply dropped in promoted/ is refused too.
        (self.registry.promoted_dir / "unsolicited.py").write_text(GOOD_MODULE)
        another = LiviumAgent(self.cfg, script=[])
        self.addCleanup(another.close)
        diagnostics = another.start()["capabilities_loaded"]
        self.assertIn("no active, human-promoted manifest", " ".join(diagnostics["failures"]))

class MultiFileCapabilityTests(CapabilityFixture):
    def test_relative_import_helper_and_data_file_survive_install_and_are_hashed(self) -> None:
        from livium.agent import LiviumAgent

        name = "nested_cap"
        self.registry.propose(name=name, requirement="read an installed data file",
                              reuse_analysis="no existing tool exposes this package resource")
        self.registry.scaffold(name)
        lab = self.cfg.paths.capability_lab / name
        (lab / "nested_cap.py").write_text('''
from .helper import answer
from livium.tools import ToolResult
CAPABILITY = {
    "name": "nested_cap", "requirement": "read an installed data file",
    "tools": [{"name": "nested_answer", "description": "Read the installed package resource",
               "parameters": {"type": "object", "properties": {}}}],
}
def handle(tool_name, args, ctx):
    return ToolResult(tool=tool_name, ok=True, output=answer())
''')
        (lab / "helper.py").write_text('''
import pathlib
def answer():
    return pathlib.Path(__file__).with_name("answer.txt").read_text().strip()
''')
        (lab / "answer.txt").write_text("Forty two\n")
        (lab / "test_nested_cap.py").write_text('''
import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from nested_cap import nested_cap
class RealContract(unittest.TestCase):
    def test_returns_installed_data(self):
        r = nested_cap.handle("nested_answer", {}, None)
        self.assertTrue(r.ok)
        self.assertEqual(r.output, "Forty two")
''')
        result = self.registry.run_tests(name)
        self.assertTrue(result["passed"], result["evidence"])
        self.assertIn("helper.py", result["files"])
        self.assertIn("answer.txt", result["files"])
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        self.registry.promote(name, approved_by="Test Human",
                              guardian_check=lambda: self.guardian.promotion_gate(name))
        agent = LiviumAgent(self.cfg, script=[])
        self.addCleanup(agent.close)
        loaded = agent.start()["capabilities_loaded"]
        self.assertEqual(loaded["loaded_tools"], ["nested_answer"])
        from livium.tools import ToolContext
        invoked = agent.tools.invoke("nested_answer", {},
                                      ToolContext(task_id="reuse", services=agent.services()))
        self.assertTrue(invoked.ok, invoked.output)
        self.assertEqual(invoked.output, "Forty two")
        self.assertEqual(self.registry.get(name).status, "maintained")

    def test_changed_helper_blocks_import_before_side_effects(self) -> None:
        name = "nested_cap"
        self.registry.propose(name=name, requirement="read resource", reuse_analysis="not built in")
        self.registry.scaffold(name)
        lab = self.cfg.paths.capability_lab / name
        (lab / "nested_cap.py").write_text('''
from .helper import answer
from livium.tools import ToolResult
CAPABILITY = {"name":"nested_cap","requirement":"read resource","tools":[
    {"name":"nested_answer","description":"Read a resource", "parameters":{"type":"object"}}]}
def handle(tool_name, args, ctx):
    return ToolResult(tool=tool_name, ok=True, output=answer())
''')
        (lab / "helper.py").write_text("def answer(): return 'safe'\n")
        (lab / "test_nested_cap.py").write_text('''
import pathlib,sys,unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from nested_cap import nested_cap
class T(unittest.TestCase):
    def test_actual_output(self):
        self.assertEqual(nested_cap.handle("nested_answer", {}, None).output, "safe")
''')
        self.assertTrue(self.registry.run_tests(name)["passed"])
        self.guardian.record_baseline(approved_by="Test Human", force=True)
        self.registry.promote(name, approved_by="Test Human",
                              guardian_check=lambda: self.guardian.promotion_gate(name))
        sentinel = self.home.dir / "SHOULD-NOT-EXIST"
        helper = self.cfg.paths.promoted / name / "helper.py"
        helper.write_text(f"import pathlib\npathlib.Path({str(sentinel)!r}).write_text('bad')\n"
                          + helper.read_text())
        assert not sentinel.exists()
        self.assertEqual(list(load_promoted(self.cfg.paths.promoted,
                                             approved=self.registry.approved_modules())), [])
        self.assertIn("promoted files differ", load_promoted.last_failures[0][1])
        self.assertFalse(sentinel.exists())
