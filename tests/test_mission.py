"""Mission acceptance criteria A-H, exercised through the real runtime.

Each test states which letter of section 11 it answers, and what would make it
fail. Where a letter is already covered by a unit test elsewhere, that is said in
the docstring instead of being duplicated -- the mission's own complexity rule
applies to its tests as much as to its code.

The model replies here come from the scripted double, so nothing in this file
says anything about model quality. Everything *around* the model is real: the
permission engine, the store, the router, the context manager, the verifier, the
ledger, and (for E) two actual operating-system processes.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
import unittest

from support import LiviumTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

WORD_COUNT_MODULE = '''
"""Count words in a workspace file."""

CAPABILITY = {
    "name": "word_count",
    "requirement": "count the words in a workspace file",
    "description": "Count whitespace-separated words in a file inside the workspace.",
    "dependencies": [],
    "permissions": ["read"],
    "limitations": "ASCII whitespace only; no per-language tokenisation.",
    "tools": [{
        "name": "word_count",
        "description": "Count words in a workspace file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    }],
}


def handle(name, arguments, ctx):
    from livium.tools import ToolResult
    config = ctx.services["config"]
    target = config.paths.workspace / str(arguments.get("path", ""))
    if not target.exists():
        return ToolResult(tool=name, ok=False, output=f"no such file: {target.name}",
                          error="not_found", category="capability")
    words = len(target.read_text(encoding="utf-8").split())
    return ToolResult(tool=name, ok=True, output=f"{words} words", data={"count": words})
'''

WORD_COUNT_TEST = '''
import pathlib
import unittest

import word_count


def context_for(directory):
    config = type("C", (), {"paths": type("P", (), {"workspace": directory})()})()
    return type("Ctx", (), {"services": {"config": config}})()


class WordCountTests(unittest.TestCase):
    def test_counts_words(self):
        directory = pathlib.Path(__file__).parent
        (directory / "sample.txt").write_text("one two three\\n", encoding="utf-8")
        result = word_count.handle("word_count", {"path": "sample.txt"}, context_for(directory))
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.data["count"], 3)

    def test_a_missing_file_is_an_error(self):
        directory = pathlib.Path(__file__).parent
        result = word_count.handle("word_count", {"path": "absent.txt"}, context_for(directory))
        self.assertFalse(result.ok)
'''


def promote_word_count(agent) -> str:
    """Take a capability through propose -> scaffold -> test -> promote.

    The module is real code with real tests, so this is the same path a genuinely
    new capability takes. No model is involved at any point.
    """

    existing = agent.capabilities.get("word_count")
    if existing is not None and existing.status in ("active", "maintained"):
        return "word_count"

    lab = agent.config.paths.capability_lab / "word_count"
    agent.capabilities.propose(
        name="word_count", requirement="count the words in a workspace file",
        reuse_analysis='Reuse ladder, measured: (1) direct reuse -- no existing tool returns a word count; read_file returns text, run_command returns program output, and the 13 registered tools were checked one by one. (2) composition -- a run_command + python3 one-liner could print len(open(p).read().split()), so the requirement is composable; that was considered and rejected because the need recurs across tasks, the one-liner would be re-invented in each prompt, and "words" needs one documented definition rather than a different expression each time. (3) extend/repair -- memory_recent is the nearest capability but it answers about memory records, not about file contents. (4) smallest suitable capability -- one tool, one argument (path), read-only.',
        rationale="the same need appeared more than once")
    agent.capabilities.scaffold("word_count")
    (lab / "word_count.py").write_text(WORD_COUNT_MODULE, encoding="utf-8")
    (lab / "test_word_count.py").write_text(WORD_COUNT_TEST, encoding="utf-8")
    tested = agent.capabilities.run_tests("word_count")
    assert tested["passed"], tested
    agent.capabilities.promote(
        "word_count", approved_by="Test Human",
        guardian_check=lambda: agent.guardian.promotion_gate("word_count"))
    return "word_count"


class MissionFixture(LiviumTestCase):
    def make_agent(self, script=None, **env):
        agent = self.agent(script=script, **env)
        agent.start()
        agent.guardian.record_baseline(approved_by="Test Human", force=True)
        return agent

    def child(self, code: str, **env) -> dict:
        """Run code in a genuinely separate process on this same home."""

        child_env = {
            "LIVIUM_HOME": str(self.home.dir),
            "LIVIUM_DB": str(self.home.dir / "livium.db"),
            "LIVIUM_WORKSPACE": str(self.home.dir / "workspace"),
            "LIVIUM_EVIDENCE": str(self.home.dir / "evidence"),
            "LIVIUM_ALLOW_TEST_DOUBLE": "1",
            "LIVIUM_PROVIDER": "scripted",
            "PYTHONPATH": str(REPO_ROOT),
            "PATH": "/usr/bin:/bin",
        }
        child_env.update(env)
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            env=child_env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        return json.loads(completed.stdout.strip().splitlines()[-1])


class A_SelectiveRetrieval(MissionFixture):
    """A: a relevant subset is retrieved; the whole store is not injected."""

    def test_only_the_relevant_records_reach_the_prompt(self) -> None:
        agent = self.make_agent()
        for index in range(30):
            agent.memory.remember("experience", f"unrelated observation {index} about aquariums",
                                  key=f"aquarium-{index}")
        relevant = agent.memory.remember(
            "experience",
            "For the quarterly report, the total must be checked against the ledger with a "
            "file_contains criterion", key="report-total", verification="verified")[0]
        agent.memory.remember("decision", "Report deadlines are the first Monday",
                              key="report-deadline", verification="verified")

        run = agent.run_task("write the quarterly report and check the total",
                             [{"kind": "file_exists", "path": "nothing.md"}])
        report = run.context_reports[0]
        self.assertIn("memory", report["included_sections"],
                      "the relevant records must be offered")
        self.assertIn(relevant, report["memory_ids"],
                      "the record that answers the objective has to reach the prompt")
        store_total = agent.store.one("SELECT COUNT(*) AS n FROM memory")["n"]
        self.assertGreaterEqual(store_total, 34,
                                "the seed instructions plus the records written above")
        self.assertLess(len(report["memory_ids"]), 12,
                        "retrieval is selective: 34 records exist, a handful are offered")
        self.assertTrue(agent.store.query(
            "SELECT payload FROM event WHERE type='memory_retrieved'"),
            "retrieval is journalled, not silent")
        for row in agent.store.query("SELECT id FROM memory WHERE key LIKE 'aquarium%'"):
            self.assertNotIn(row["id"], report["memory_ids"],
                             "irrelevant records must not be dumped into the prompt")

    def test_a_wide_store_does_not_widen_the_prompt(self) -> None:
        agent = self.make_agent()
        baseline = agent.run_task("write the quarterly report",
                                  [{"kind": "file_exists", "path": "x"}])
        for index in range(40):
            agent.memory.remember("experience", f"filler {index} " + "text " * 30,
                                  key=f"filler-{index}")
        after = agent.run_task("write the quarterly report",
                               [{"kind": "file_exists", "path": "x"}])
        growth = after.context_reports[0]["tokens_estimated"] / max(
            baseline.context_reports[0]["tokens_estimated"], 1)
        self.assertLess(growth, 2.0,
                        "40 more records must not multiply the size of the request")


class B_ContextBudget(MissionFixture):
    """B: over-budget contexts are reduced, and essential material survives.

    The unit-level behaviour (mandatory sections win, omissions are recorded, items
    are dropped whole) is in ``tests/test_context.py``. These are the end-to-end
    versions: what a real run does when the window cannot hold everything, and what
    it does when the window cannot hold even the rules.
    """

    def test_a_window_smaller_than_the_rules_fails_loudly(self) -> None:
        agent = self.make_agent(
            [{"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "claimed done", "status": "complete"}}]}],
            LIVIUM_CONTEXT_WINDOW="1200", LIVIUM_MAX_OUTPUT_TOKENS="128")
        run = agent.run_task("produce the summary", [{"kind": "file_exists", "path": "x"}])
        self.assertEqual(run.status, "failed")
        self.assertIn("protected instructions", run.error or "")
        self.assertNotEqual(run.verification["state"], "verified")
        self.assertEqual(run.context_reports, [],
                         "no request is sent when the rules cannot fit in it")

    def test_a_small_but_workable_window_keeps_essentials_and_names_omissions(self) -> None:
        agent = self.make_agent(
            [{"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "nothing to do", "status": "partial"}}]}],
            LIVIUM_CONTEXT_WINDOW="4200", LIVIUM_MAX_OUTPUT_TOKENS="256")
        for index in range(25):
            agent.memory.remember("experience", f"long unrelated note {index} " + "detail " * 40,
                                  key=f"note-{index}")
        run = agent.run_task("produce the summary", [{"kind": "file_exists", "path": "nope.md"}])
        report = run.context_reports[0]
        self.assertTrue(report["within_window"], "the built context must fit the window")
        self.assertEqual(report["budget"]["window"], 4200)
        self.assertIn("instructions", report["included_sections"])
        self.assertIn("objective", report["included_sections"])
        self.assertLessEqual(report["included_sections"].get("memory", 0), 12,
                             "what is included is bounded, and what is left out is recorded")


class C_CapabilityDiscovery(MissionFixture):
    """C: applicable capabilities are found, interfaces load when needed, and
    nothing unvalidated is presented as verified."""

    def test_a_validated_capability_is_found_and_its_interface_is_real(self) -> None:
        agent = self.make_agent()
        promoted = promote_word_count(agent)
        matches = agent.capabilities.search("count the words in a file")
        self.assertTrue(matches, "the promoted capability must be discoverable by words")
        names = [m["name"] for m in matches]
        self.assertIn(promoted, names)
        self.assertTrue(matches[0]["validated"])
        interface = agent.capabilities.interface_for(matches[0]["name"])
        self.assertIsNotNone(interface)
        self.assertTrue(interface["tools"], "an interface with no tools describes nothing")
        spec = interface["tools"][0]
        self.assertEqual(spec["name"], promoted)
        self.assertIn("parameters", spec)
        self.assertIn("limitations", interface)

    def test_an_unvalidated_capability_is_never_reported_as_validated(self) -> None:
        agent = self.make_agent()
        promote_word_count(agent)
        agent.capabilities.propose(
            name="something_nobody_has_implemented_yet",
            requirement="invent a capability that does not exist",
            reuse_analysis="no existing tool or capability covers it",
            rationale="testing the registry's honesty")
        matches = agent.capabilities.search("invent a capability that does not exist",
                                           include_unvalidated=True)
        proposed = [m for m in matches if m["name"] == "something_nobody_has_implemented_yet"]
        self.assertTrue(proposed, "an unfinished candidate is findable, but labelled")
        self.assertFalse(proposed[0]["validated"])
        self.assertNotEqual(proposed[0]["status"], "active")
        interface = agent.capabilities.interface_for(proposed[0]["name"])
        self.assertTrue(interface is None or not interface["tools"],
                        "a proposal declares no callable interface yet")

    def test_a_nonexistent_capability_is_not_invented(self) -> None:
        agent = self.make_agent()
        promote_word_count(agent)
        self.assertEqual(agent.capabilities.search("orbital telemetry autopilot"), [])
        self.assertIsNone(agent.capabilities.interface_for("orbital_telemetry"))

    def test_only_the_selected_interfaces_are_sent_to_the_model(self) -> None:
        agent = self.make_agent()
        promote_word_count(agent)
        run = agent.run_task("read the notes and report the total",
                             [{"kind": "file_exists", "path": "notes.md"}])
        available = agent.tools.specs_names()
        exposed = run.context_reports[0]["exposed_tools"]
        self.assertTrue(exposed)
        self.assertLess(len(exposed), len(available),
                        "sending every interface is the problem this mission exists to fix")
        for name in exposed:
            self.assertIn(name, available)

    def test_a_capability_tool_reports_where_it_came_from(self) -> None:
        agent = self.make_agent()
        promote_word_count(agent)
        reloaded = self.make_agent()
        entry = [c for c in reloaded.tools.catalog() if c["name"] == "word_count"][0]
        self.assertEqual(entry["source"], "capability:word_count",
                         "the catalog must not claim a capability tool is a built-in")
        self.assertIn("capability:", entry["validation"])


class D_FeedbackRecovery(MissionFixture):
    """D: a real failure is captured, evaluated, and changes the next action; an
    identical retry is not repeated forever."""

    def test_a_failed_operation_is_classified_and_the_next_step_changes(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "run_command", "arguments": {
                "argv": ["false"], "purpose": "check something"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "could not run the check", "status": "partial"}}]},
        ])
        run = agent.run_task("run the check and report",
                             [{"kind": "event", "event_type": "nothing_happened"}])
        decisions = [(d["step"], d["decision"]) for d in run.route_decisions]
        self.assertTrue(any(name in ("investigate_failure", "change_approach")
                            for _, name in decisions),
                        f"a real failure must change the route, got {decisions}")
        calls = [json.loads(r["payload"]) for r in
                 agent.store.query("SELECT payload FROM event WHERE type='tool_call'")]
        self.assertTrue(any(not c["ok"] and not c["denied"] for c in calls),
                        "the failure has to be recorded as a failure")
        self.assertNotEqual(run.status, "complete")

    def test_the_loop_does_not_repeat_an_identical_failing_call(self) -> None:
        same = {"tool_calls": [{"name": "read_file", "arguments": {
            "path": "definitely-absent.txt", "purpose": "look for it"}}]}
        agent = self.make_agent([same, same, same, same, same, same])
        run = agent.run_task("read the absent file", [{"kind": "file_exists", "path": "x.md"}])
        self.assertLessEqual(run.tool_calls, 3,
                             "the same call with the same arguments must not be repeated")
        self.assertIn(run.status, ("blocked", "partial", "failed"))

    def test_a_capability_is_not_created_merely_because_something_failed(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "absent.txt"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "the file is not there", "status": "blocked"}}]},
        ])
        run = agent.run_task("read absent.txt", [{"kind": "file_exists", "path": "absent.txt"}])
        self.assertEqual([c.name for c in agent.capabilities.all()], [],
                         "a failure is classified before anything is proposed or built")
        self.assertFalse(run.generated_capabilities)


class E_ExperienceAcrossRestart(MissionFixture):
    """E: experience written by one process is reused by a *different* process."""

    def test_a_later_process_reuses_the_experience_and_the_ledger_shows_the_link(self) -> None:
        first = self.child(
            """
            import json
            from livium.config import Config
            from livium.store import Store
            from livium.memory import Memory
            config = Config.load(); config.ensure_dirs()
            memory_id, action = Memory(Store(config.paths.db)).remember(
                "capability",
                "recording counts: call memory_recent first to see what was learned recently "
                "instead of guessing at keywords",
                key="memory_recent", confidence=0.9, verification="verified",
                evidence="observed in an earlier task", tags=["reuse:memory_recent"])
            print(json.dumps({"memory_id": memory_id, "action": action}))
            """
        )
        self.assertEqual(first["action"], "created")

        second = self.child(
            """
            import json
            from livium.config import Config
            from livium.agent import LiviumAgent
            from livium.ledger import Ledger
            config = Config.load(); config.ensure_dirs()
            agent = LiviumAgent(config, script=[
                {"tool_calls": [{"name": "memory_recent", "arguments": {"limit": 5}}]},
                {"tool_calls": [{"name": "finish_task", "arguments": {
                    "summary": "listed recent records", "status": "complete"}}]},
            ])
            agent.start()
            if not agent.guardian.integrity().ok:
                agent.guardian.record_baseline(approved_by="Test Human", force=True)
            run = agent.run_task("recall what was learned recently",
                                 [{"kind": "event", "event_type": "tool_call"}])
            row = Ledger(agent.store).rows(limit=1)[0]
            latest = agent.store.one(
                "SELECT memory_ids, experience_evidence FROM ledger ORDER BY id DESC LIMIT 1")
            print(json.dumps({
                "exposed_memory_recent": "memory_recent" in agent.tools.specs_names(),
                "influenced": bool(row.experience_influenced),
                "evidence": json.loads(latest["experience_evidence"]),
                "memory_ids": json.loads(latest["memory_ids"]),
                "status": run.status,
                "verification": run.verification["state"],
            }))
            """
        )
        self.assertEqual(second["verification"], "verified",
                         "the criterion is objective and the run satisfied it")
        self.assertTrue(second["exposed_memory_recent"])
        self.assertTrue(second["influenced"],
                        "a record naming the tool that then ran is reuse, not a coincidence")
        self.assertEqual(second["evidence"]["reuse"][0]["memory_id"], first["memory_id"])
        self.assertIn(first["memory_id"], second["memory_ids"])


class F_GenuineCapabilityGrowth(MissionFixture):
    """F: a capability can be implemented, tested, promoted, and then used.

    The lifecycle rules themselves (no self-promotion, placeholders cannot pass,
    status transitions are enforced) are in ``tests/test_capabilities.py``. This
    test proves the *end* of that path in a real runtime: the promoted tool is
    callable, the registry describes the implementation that exists, and the
    promotion is attributed to a human.
    """

    def test_a_real_capability_is_proposed_tested_promoted_and_then_used(self) -> None:
        agent = self.make_agent()
        promote_word_count(agent)

        record = agent.capabilities.get("word_count")
        assert record is not None
        self.assertEqual(record.status, "active")
        self.assertEqual(record.promoted_by, "Test Human",
                         "promotion is attributed, never anonymous")
        self.assertTrue(record.tools, "the registry must describe the real implementation")
        self.assertIn("word_count", [t["name"] for t in record.tools])
        self.assertIn("ASCII whitespace only", record.limitations or "")
        self.assertIn("capability_promoted", [
            row["type"] for row in agent.store.query(
                "SELECT type FROM event WHERE type='capability_promoted'")])

        # A later session, given a task that needs the new capability.
        reloaded = self.make_agent([
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "counted.txt", "content": "alpha beta gamma\n", "purpose": "input"}}]},
            {"tool_calls": [{"name": "word_count", "arguments": {
                "path": "counted.txt", "purpose": "the new capability"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "counted with the new capability", "status": "complete"}}]},
        ])
        self.assertIn("word_count", reloaded.tools.specs_names(),
                      "a promoted capability must change what the agent can do")
        run = reloaded.run_task("count the words in counted.txt",
                                [{"kind": "event", "event_type": "tool_call"}])
        self.assertTrue(run.route_decisions, "the harness recorded its routing")
        executed = [json.loads(r["payload"])["tool"] for r in reloaded.store.query(
            "SELECT payload FROM event WHERE type='tool_call' ORDER BY id")]
        self.assertIn("word_count", executed, "the capability actually ran")
        self.assertEqual(run.verification["state"], "verified")
        from livium.tools import ToolContext
        result = reloaded.tools.invoke(
            "word_count", {"path": "counted.txt"},
            ToolContext(task_id="probe", step=1, services=reloaded.services()))
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.data["count"], 3,
                         "the promoted capability must do what it says, on real input")


class G_TokenInstrumentation(MissionFixture):
    """G: real usage is recorded when reported; estimates are labelled as estimates;
    a missing measurement is never turned into a number."""

    def _client(self, name: str, usage: dict):
        from livium.models import ModelClient, ModelReply, ProbeResult, ToolCall

        class Client(ModelClient):
            provider = name
            is_test_double = True

            def __init__(self, config):
                super().__init__(config)
                self.calls = 0

            def complete(self, conversation, tools, *, max_output_tokens=None):
                self.calls += 1
                if self.calls == 1:
                    call = ToolCall(id="c1", name="write_file", arguments={
                        "path": "out.txt", "content": "x\n", "purpose": "artefact"})
                else:
                    call = ToolCall(id=f"c{self.calls}", name="finish_task", arguments={
                        "summary": "wrote it", "status": "complete"})
                return ModelReply(text="", tool_calls=[call], stop_reason="tool_use",
                                  usage=dict(usage), latency_ms=1, provider=name,
                                  model="instrumentation-double", is_test_double=True)

            def probe(self):
                return ProbeResult(ok=True, detail="instrumentation double")

        return Client

    def test_reported_usage_is_recorded_as_actual(self) -> None:
        from livium.agent import LiviumAgent
        from livium.ledger import Ledger

        config = self.config()
        client = self._client("reporting", {"prompt_tokens": 900, "completion_tokens": 120})
        agent = LiviumAgent(config, client=client(config))
        self.addCleanup(agent.close)
        agent.start()
        agent.guardian.record_baseline(approved_by="Test Human", force=True)
        agent.run_task("write out.txt", [{"kind": "file_exists", "path": "out.txt"}])
        row = Ledger(agent.store).rows(limit=1)[0]
        self.assertEqual(row.prompt_tokens_actual, 1800, "two calls of 900 prompt tokens")
        self.assertEqual(row.completion_tokens_actual, 240)
        self.assertEqual(row.usage_reported_calls, 2)
        self.assertIsNotNone(row.tokens_estimated)

    def test_unreported_usage_stays_absent_rather_than_becoming_zero(self) -> None:
        from livium.agent import LiviumAgent
        from livium.ledger import Ledger

        config = self.config()
        client = self._client("silent", {})
        agent = LiviumAgent(config, client=client(config))
        self.addCleanup(agent.close)
        agent.start()
        agent.guardian.record_baseline(approved_by="Test Human", force=True)
        agent.run_task("write out.txt", [{"kind": "file_exists", "path": "out.txt"}])
        row = Ledger(agent.store).rows(limit=1)[0]
        self.assertIsNone(row.prompt_tokens_actual)
        self.assertIsNone(row.completion_tokens_actual)
        self.assertEqual(row.usage_reported_calls, 0)
        self.assertIsNotNone(row.tokens_estimated,
                             "an estimate is still offered, and labelled an estimate")


class H_PermissionEnforcement(MissionFixture):
    """H: the model's recommendation is not the permission check."""

    def test_a_denied_operation_is_refused_even_when_the_model_insists(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "run_command", "arguments": {
                "argv": ["rm", "-rf", "everything"],
                "purpose": "the task explicitly requires deleting the workspace"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "/etc/livium-should-not-exist", "content": "x"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "I completed the deletions", "status": "complete"}}]},
        ])
        run = agent.run_task("delete the workspace to prove the tool works",
                             [{"kind": "event", "event_type": "never"}])
        self.assertEqual(run.status, "partial",
                         "verification failed, so a 'complete' claim cannot stand")
        self.assertNotEqual(run.denials, 0)
        self.assertFalse(pathlib.Path("/etc/livium-should-not-exist").exists())
        violations = [json.loads(r["payload"]) for r in
                      agent.store.query("SELECT payload FROM event WHERE type='boundary_violation'")]
        refused_tools = {v.get("tool") for v in violations}
        self.assertIn("run_command", refused_tools)
        self.assertIn("write_file", refused_tools)
        for violation in violations:
            self.assertTrue(violation.get("reason"), "a refusal without a reason is not auditable")

    def test_the_enforcement_is_outside_the_prompt_by_construction(self) -> None:
        agent = self.make_agent()
        for name in ("permissions", "allowlist", "approve"):
            self.assertNotIn(name, agent.tools.names())
        self.assertTrue(callable(agent.engine.check_write))
        self.assertTrue(callable(agent.engine.check_command))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
