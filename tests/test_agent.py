"""The operational loop.

Model replies here come from the scripted test double (see tests/support.py), so
these tests validate *orchestration*: loop control, tool dispatch, verification,
persistence, honest failure. They say nothing about model quality and nothing about
a real provider integration -- `test_models.py` covers the wire format, and the
real provider cannot be reached from this environment.

One test deliberately uses a transport that raises, to prove the agent reports an
unavailable model instead of inventing a completion.
"""

from __future__ import annotations

import json
import unittest

from support import LiviumTestCase

from livium.agent import LiviumAgent
from livium.models import (
    ModelClient, ModelReply, ProbeResult, ProviderRejected, ProviderUnavailable, ToolCall,
)


class BrokenTransport(ModelClient):
    """A transport that always fails. Test double for the network layer only."""

    provider = "broken"

    def complete(self, conversation, tools, *, max_output_tokens=None):
        raise ProviderUnavailable("simulated total egress failure for the test")

    def probe(self):
        from livium.models import ProbeResult

        return ProbeResult(ok=False, detail="simulated total egress failure for the test")


class NestedUsageReply(ModelClient):
    """A model transport fixture that returns a real-shaped reply with bad telemetry."""

    provider = "usage-fixture"

    def __init__(self, config):
        super().__init__(config)
        self.is_test_double = True

    def complete(self, conversation, tools, *, max_output_tokens=None):
        return ModelReply(
            text="",
            tool_calls=[ToolCall(
                id="finish-1", name="finish_task",
                arguments={"summary": "The task reached its finish action.",
                           "status": "complete"},
            )],
            stop_reason="tool_use",
            usage={"prompt_tokens": {"cached_tokens": 4},
                   "completion_tokens": {"reasoning_tokens": 7}},
            latency_ms=1,
            provider=self.provider,
            model="usage-fixture",
            is_test_double=True,
        )

    def probe(self):
        return ProbeResult(ok=True, detail="labelled usage fixture")


class RateLimitedOnce(ModelClient):
    """A deterministic provider fixture for the bounded Retry-After path."""

    provider = "rate-limit-fixture"

    def __init__(self, config):
        super().__init__(config)
        self.is_test_double = True
        self.calls = 0

    def complete(self, conversation, tools, *, max_output_tokens=None):
        self.calls += 1
        if self.calls == 1:
            raise ProviderRejected(
                "HTTP 429 rate limited", status_code=429, retry_after=0,
            )
        return ModelReply(
            text="",
            tool_calls=[ToolCall(
                id="finish-2", name="finish_task",
                arguments={"summary": "The bounded retry succeeded.", "status": "complete"},
            )],
            stop_reason="tool_use", usage={}, latency_ms=1,
            provider=self.provider, model="rate-limit-fixture", is_test_double=True,
        )

    def probe(self):
        return ProbeResult(ok=True, detail="labelled rate-limit fixture")


class AgentFixture(LiviumTestCase):
    def make_agent(self, script=None, **env):
        # The env overrides must reach the agent's own config; an earlier version of
        # this helper built a config and then discarded it, so tests that set limits
        # were silently running with the defaults.
        agent = self.agent(script=script, **env)
        agent.start()
        agent.guardian.record_baseline(approved_by="Test Human", force=True)
        return agent


class TaskExecutionTests(AgentFixture):
    def test_nested_provider_usage_does_not_discard_a_model_reply(self) -> None:
        config = self.config()
        config.ensure_dirs()
        agent = LiviumAgent(config, client=NestedUsageReply(config))
        self.addCleanup(agent.close)
        agent.start()
        agent.guardian.record_baseline(approved_by="Test Human", force=True)

        run = agent.run_task("finish a task even when provider usage metadata is malformed")

        self.assertIsNone(run.error)
        self.assertTrue(run.final_summary)
        self.assertEqual(run.model_calls, 1)
        self.assertEqual(run.model_failures, 0)
        self.assertEqual(run.status, "partial")  # no acceptance criteria means unverified
        self.assertTrue(run.is_test_double)

    def test_provider_rate_limit_retries_after_the_reported_wait(self) -> None:
        from unittest.mock import patch

        config = self.config(LIVIUM_MODEL_RETRIES="1")
        config.ensure_dirs()
        client = RateLimitedOnce(config)
        agent = LiviumAgent(config, client=client)
        self.addCleanup(agent.close)
        agent.start()
        agent.guardian.record_baseline(approved_by="Test Human", force=True)

        with patch("livium.agent.time.sleep") as sleep:
            run = agent.run_task("finish after a transient provider rate limit")

        self.assertIsNone(run.error)
        self.assertEqual(client.calls, 2)
        self.assertEqual(run.model_calls, 2)
        self.assertEqual(run.model_failures, 1)
        sleep.assert_called_once_with(0.1)
        events = agent.store.events(event_type="model_rate_limit_retry", task_id=run.task_id)
        self.assertEqual(len(events), 1)
        attempts = agent.store.events(event_type="model_request_attempt", task_id=run.task_id)
        self.assertEqual(len(attempts), 2)

    def test_a_real_task_runs_tools_and_is_verified_by_the_harness(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "report.md", "content": "total 7 items\n", "purpose": "the artefact"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "wrote report.md", "status": "complete"}}]},
        ])
        run = agent.run_task(
            "write report.md containing a total line",
            [{"kind": "file_exists", "path": "report.md"},
             {"kind": "file_contains", "path": "report.md", "text": "total 7 items"}],
        )
        self.assertEqual(run.status, "complete")
        self.assertEqual(run.verification["state"], "verified")
        self.assertEqual(run.denials, 0)
        self.assertGreaterEqual(run.tool_calls, 2)
        self.assertTrue(run.is_test_double)
        # The file on disk is the real evidence, not the model's claim.
        written = (agent.config.paths.workspace / "report.md").read_text(encoding="utf-8")
        self.assertEqual(written, "total 7 items\n")

    def test_model_claim_alone_does_not_close_the_task(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "I definitely wrote the file", "status": "complete"}}]},
        ])
        run = agent.run_task("pretend to write a file",
                            [{"kind": "file_exists", "path": "never-created.txt"}])
        self.assertEqual(run.status, "partial")
        self.assertEqual(run.verification["state"], "not verified")
        self.assertIn("acceptance checks report", run.unverified)

    def test_no_criteria_means_unverified_not_success(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "did something", "status": "complete"}}]},
        ])
        run = agent.run_task("do something without a definition of done")
        self.assertIn("unverified", run.verification["state"])
        self.assertEqual(run.status, "partial")

    def test_an_unavailable_provider_blocks_rather_than_inventing_a_completion(self) -> None:
        config = self.config()
        config.ensure_dirs()
        agent = LiviumAgent(config, client=BrokenTransport(config))
        self.addCleanup(agent.close)
        agent.start()
        run = agent.run_task("a task that cannot be attempted")
        self.assertEqual(run.status, "blocked")
        self.assertIn("model unavailable", run.error)
        # One attempt was made, it failed, and the failure is reported as such --
        # rather than being counted as a completion or hidden.
        self.assertEqual(run.model_calls, 1)
        self.assertEqual(run.model_failures, 1)
        self.assertFalse(run.final_summary)
        self.assertEqual(run.tool_calls, 0)
        events = agent.store.events(event_type="model_unavailable")
        self.assertEqual(len(events), 1)

    def test_probe_reports_the_failure_honestly_at_startup(self) -> None:
        config = self.config()
        config.ensure_dirs()
        agent = LiviumAgent(config, client=BrokenTransport(config))
        self.addCleanup(agent.close)
        startup = agent.start()
        self.assertFalse(startup["provider_probe"]["ok"])
        self.assertIn("simulated total egress failure", startup["provider_probe"]["detail"])


class DemonstrationHonestyTests(LiviumTestCase):
    def test_unreachable_real_provider_demo_never_runs_a_scripted_a1_task(self) -> None:
        """The A1 probe and task must use the same real transport, even on failure."""

        import os
        import socket
        from unittest.mock import patch

        from demos.run_demonstrations import Demonstrations

        # A bound, non-listening loopback port fails without any external network.
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{unused.getsockname()[1]}/v1"
            with patch.dict(os.environ, {
                "LIVIUM_PROVIDER": "openai_compatible", "LIVIUM_BASE_URL": url,
                "LIVIUM_MODEL": "local-unreachable", "LIVIUM_API_KEY": "",
                "LIVIUM_MODEL_TIMEOUT": "1", "LIVIUM_MODEL_RETRIES": "0",
                # Even if the ambient environment enables the double, A1 must not.
                "LIVIUM_ALLOW_TEST_DOUBLE": "1",
            }):
                demo = Demonstrations(self.home.dir, approve_as=None)
                demo.demonstration_a()

        a1, a2 = demo.results
        self.assertEqual(a1["outcome"], "blocked by environment")
        self.assertFalse(a1["evidence"]["provider_probe"]["ok"])
        self.assertFalse(a1["evidence"]["is_test_double"])
        self.assertEqual(a1["evidence"]["task_status"], "blocked")
        self.assertIn("model unavailable", a1["evidence"]["error"])
        self.assertEqual(a1["evidence"]["model_calls"], 1)
        self.assertEqual(a1["evidence"]["tool_calls"], 0)
        self.assertEqual(a2["outcome"], "verified")
        self.assertTrue(a2["evidence"]["is_test_double"])


class BoundaryAndControlTests(AgentFixture):
    def test_a_denied_operation_is_recorded_and_not_retried(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "run_command", "arguments": {"argv": ["rm", "-rf", "/tmp/x"]}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "blocked by policy", "status": "blocked"}}]},
        ])
        run = agent.run_task("delete something", [{"kind": "event", "event_type": "boundary_violation"}])
        self.assertEqual(run.denials, 1)
        violations = agent.store.events(event_type="boundary_violation")
        self.assertEqual(len(violations), 1)
        payload = json.loads(violations[0]["payload"])
        self.assertEqual(payload["category"], "command")
        self.assertIn("rm", payload["reason"])
        # The criterion passes because the denial genuinely happened.
        self.assertTrue(run.verification["state"] == "verified")

    def test_repeated_identical_tool_calls_stop_the_loop(self) -> None:
        """An agent that cannot stop is not useful; three identical calls is enough."""

        same = {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]}
        agent = self.make_agent([same, same, same, same])
        run = agent.run_task("look at the workspace forever")
        self.assertEqual(run.status, "blocked")
        self.assertIn("identical arguments", run.error)
        self.assertTrue(agent.store.events(event_type="loop_detected"))

    def test_step_limit_is_enforced(self) -> None:
        script = [{"tool_calls": [{"name": "list_dir", "arguments": {"path": f"./s{i}"}}]}
                  for i in range(8)]
        agent = self.make_agent(script, LIVIUM_MAX_STEPS="3")
        run = agent.run_task("keep looking")
        self.assertEqual(run.status, "blocked")
        self.assertIn("step limit", run.error)
        self.assertLessEqual(run.steps, 3)

    def test_tool_call_limit_is_enforced(self) -> None:
        many = {"tool_calls": [{"name": "list_dir", "arguments": {"path": f"./{i}"}}
                               for i in range(6)]}
        agent = self.make_agent([many, many, many], LIVIUM_MAX_TOOL_CALLS="4")
        run = agent.run_task("many calls")
        self.assertLessEqual(run.tool_calls, 4)

    def test_text_only_reply_gets_one_nudge_then_the_loop_ends(self) -> None:
        agent = self.make_agent([{"text": "I think this is done."}, {"text": "Still just talking."}])
        run = agent.run_task("talk instead of acting")
        self.assertEqual(run.status, "partial")
        self.assertIn("Still just talking", run.final_summary)
        replies = agent.store.events(event_type="model_reply")
        self.assertEqual(len(replies), 2)

    def test_guardian_freeze_stops_the_run(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "x.txt", "content": "1"}}]},
        ])
        # Simulate a tamper detection that happens between steps.
        agent.guardian._frozen_reason = "protected file modified during the run"
        run = agent.run_task("do work while frozen")
        self.assertEqual(run.status, "blocked")
        self.assertTrue(run.frozen)
        self.assertIn("froze the run", run.error)
        self.assertTrue(run.guardian_consequences)
        self.assertTrue(any(c["enforced"] for c in run.guardian_consequences))


class PersistenceAndContinuityTests(AgentFixture):
    def test_outcome_is_remembered_without_the_model_asking(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "a"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "wrote a.txt", "status": "complete"}}]},
        ])
        run = agent.run_task("write a.txt", [{"kind": "file_exists", "path": "a.txt"}])
        continuity = agent.memory.by_key("task", f"task:{run.task_id}")
        self.assertIsNotNone(continuity)
        self.assertIn("verified", continuity.detail)
        self.assertEqual(continuity.verification, "verified")
        experiences = agent.store.query(
            "SELECT * FROM memory WHERE kind='experience' AND key LIKE 'experience:success:%'"
        )
        self.assertEqual(len(experiences), 1)

    def test_a_blocked_task_is_recorded_as_a_failure_hypothesis_not_a_fact(self) -> None:
        agent = self.make_agent([{"text": "nothing to do"}])
        run = agent.run_task("a task with no path to completion")
        failures = agent.memory.search("no path to completion", kinds=["failure"], limit=5)
        self.assertTrue(failures)
        record = failures[0]
        self.assertEqual(record.verification, "unverified")
        self.assertIn("hypothesis, not a", record.detail)
        self.assertLessEqual(record.confidence, 0.5)

    def test_task_rows_and_steps_are_persisted_for_recovery(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "b.txt", "content": "b"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "done", "status": "complete"}}]},
        ])
        run = agent.run_task("write b.txt", [{"kind": "file_exists", "path": "b.txt"}])
        task = agent.store.one("SELECT * FROM task WHERE id=?", (run.task_id,))
        self.assertEqual(task["status"], "complete")
        steps = agent.store.query("SELECT * FROM task_step WHERE task_id=?", (run.task_id,))
        self.assertEqual(len(steps), run.steps)
        self.assertTrue(all(step["ended_at"] for step in steps))

    def test_resume_reports_an_interrupted_task_and_continues(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "continued after recovery", "status": "complete"}}]},
        ])
        # An interrupted write: the intent is pending and the file really exists.
        workspace = agent.config.paths.workspace
        payload = "landed before the crash\n"
        (workspace / "partial.md").write_bytes(payload.encode("utf-8"))
        agent.store.record_intent(
            "write_file", {"path": str(workspace / "partial.md"), "bytes": len(payload.encode())},
            task_id="resume-me",
        )
        run = agent.run_task("finish the interrupted work", [], task_id="resume-me", resume=True)
        findings = agent.store.events(task_id="resume-me", event_type="recovery_finding")
        self.assertEqual(len(findings), 1)
        self.assertEqual(json.loads(findings[0]["payload"])["determination"], "confirmed_complete")
        self.assertTrue(any("Recovery:" in o for o in run.observations))
        # And the recovery experience is remembered.
        self.assertIsNotNone(agent.memory.by_key("experience", "recovery:resume-me"))

    def test_context_reports_are_recorded_for_each_step(self) -> None:
        agent = self.make_agent([
            {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "looked", "status": "complete"}}]},
        ])
        run = agent.run_task("look at the workspace", [])
        self.assertEqual(len(run.context_reports), run.steps)
        self.assertTrue(all(r["within_window"] for r in run.context_reports))
        rows = agent.store.query("SELECT * FROM context_report WHERE task_id=?", (run.task_id,))
        self.assertEqual(len(rows), run.steps)


class CapabilityContextTests(AgentFixture):
    def test_promoted_tool_becomes_callable_after_a_restart(self) -> None:
        """The growth path end to end, at unit scale: a capability promoted by a
        human is loaded on the next start and the model can call it."""

        config = self.config()
        config.ensure_dirs()
        first = LiviumAgent(config)
        self.addCleanup(first.close)
        first.start()
        registry = first.capabilities
        # Redirect promotion into the temp home. Without this the registry's default
        # promoted directory is the repository's own livium/promoted, and running the
        # test suite would install files into the source tree -- which is exactly what
        # an earlier version of this test did before it was caught.
        repo_promoted = registry.promoted_dir
        registry.promoted_dir = self.home.dir / "livium" / "promoted"
        self.addCleanup(lambda: registry.promoted_dir.mkdir(parents=True, exist_ok=True))
        registry.propose(name="peek", requirement="count the files in the workspace",
                         reuse_analysis="list_dir lists entries but the model then has to count "
                                        "them itself; no tool returns a count")
        registry.scaffold("peek")
        lab = config.paths.capability_lab / "peek"
        (lab / "peek.py").write_text(_PEEK_MODULE, encoding="utf-8")
        (lab / "test_peek.py").write_text(_PEEK_TEST, encoding="utf-8")
        result = registry.run_tests("peek")
        self.assertTrue(result["passed"], result["stderr"])
        first.guardian.record_baseline(approved_by="Test Human", force=True)
        registry.promote("peek", approved_by="Test Human",
                         guardian_check=lambda: first.guardian.promotion_gate("peek"))
        first.close()

        # A genuinely new process-level agent, which loads promoted capabilities.
        promoted_dir = registry.promoted_dir
        self.assertTrue((promoted_dir / "peek" / "peek.py").exists())
        # A new agent instance: the promoted module is copied into a *temporary*
        # promoted directory by this test, so the load path is exercised without
        # installing anything into the repository.
        second = LiviumAgent(self.config(), script=[
            {"tool_calls": [{"name": "peek_count", "arguments": {}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "used the promoted capability", "status": "complete"}}]},
        ])
        self.addCleanup(second.close)
        second.capabilities.promoted_dir = promoted_dir
        second.start()
        self.assertIn("peek_count", second.tools.names())
        (second.config.paths.workspace / "one.txt").write_text("x", encoding="utf-8")
        run = second.run_task("use peek_count", [])
        self.assertTrue(any("peek_count:" in o or "1 file" in o for o in run.observations),
                        run.observations)
        self.assertTrue(second.store.events(event_type="capability_tool_used"))
        # The repository must be untouched by all of the above.
        self.assertFalse((repo_promoted / "peek.py").exists(),
                         "test promotion leaked into the repository")


_PEEK_MODULE = '''\
"""Count the entries in the workspace."""

CAPABILITY = {
    "name": "peek",
    "requirement": "count the files in the workspace",
    "description": "Reports how many files and directories the workspace contains.",
    "tools": [{
        "name": "peek_count",
        "description": "Count entries in the workspace and report the number.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }],
}


def handle(tool_name, args, ctx):
    from livium.tools import ToolResult

    config = (ctx.services or {}).get("config")
    root = config.paths.workspace
    entries = sorted(p.name for p in root.iterdir()) if root.exists() else []
    return ToolResult(tool=tool_name, ok=True,
                      output=f"{len(entries)} file(s) in the workspace: {', '.join(entries)}",
                      data={"count": len(entries), "entries": entries})
'''

_PEEK_TEST = '''\
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import peek  # noqa: E402


class FakeConfig:
    class Paths:
        workspace = pathlib.Path("/tmp/peek-workspace")
    paths = Paths()


class TestPeek(unittest.TestCase):
    def setUp(self):
        FakeConfig.Paths.workspace.mkdir(parents=True, exist_ok=True)
        for child in FakeConfig.Paths.workspace.iterdir():
            child.unlink()

    def test_counts_entries(self):
        (FakeConfig.Paths.workspace / "a.txt").write_text("a", encoding="utf-8")
        result = peek.handle("peek_count", {}, type("C", (), {"services": {"config": FakeConfig()}})())
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 1)
'''


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
