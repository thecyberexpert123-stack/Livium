"""Tool tests assert observable effects: bytes on disk, exit codes, database rows."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import unittest
from unittest import mock

from support import LiviumTestCase

from livium.config import Config
from livium.execution import Runner, detect_isolation
from livium.memory import Memory
from livium.permissions import PermissionEngine
from livium.store import Store
from livium.tools import ToolContext, ToolRegistry


class ToolFixture(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()          # `config` stays callable for variants
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.paths.db)
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store)
        self.engine = PermissionEngine(self.cfg)
        self.runner = Runner(
            max_seconds=self.cfg.limits.max_command_seconds,
            max_output_bytes=self.cfg.limits.max_tool_output_bytes,
        )
        self.tools = ToolRegistry(self.cfg, self.engine, self.store, self.memory, self.runner)
        self.ctx = ToolContext(task_id="t-tools", step=1)

    def invoke(self, tool_name, **arguments):
        return self.tools.invoke(tool_name, arguments, self.ctx)

    def run_ok(self, **arguments):
        result = self.invoke("run_command", **arguments)
        self.assertIn("exit_code", result.data, result.output)
        return result


class FileToolTests(ToolFixture):
    def test_write_then_read_round_trip_with_hash_confirmation(self) -> None:
        result = self.invoke("write_file", path="notes.md", content="hello livium\n")
        self.assertTrue(result.ok, result.output)
        target = self.cfg.paths.workspace / "notes.md"
        self.assertEqual(target.read_text(encoding="utf-8"), "hello livium\n")
        self.assertTrue(result.data["verified_by_readback"])
        self.assertEqual(result.data["sha256"],
                         hashlib.sha256(target.read_bytes()).hexdigest())

        read = self.invoke("read_file", path="notes.md")
        self.assertTrue(read.ok)
        self.assertIn("hello livium", read.output)

    def test_write_is_recorded_as_a_resolved_intent(self) -> None:
        self.invoke("write_file", path="a.txt", content="x")
        intents = self.store.query("SELECT * FROM intent WHERE tool='write_file'")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], "done")
        self.assertIn("verified_by_readback", intents[0]["result"])

    def test_missing_workspace_is_created_rather_than_failing(self) -> None:
        """A cleaned-out workspace must not surface as a confusing ENOENT."""

        import shutil

        cfg = self.config()
        shutil.rmtree(cfg.paths.workspace, ignore_errors=True)
        tools = ToolRegistry(cfg, PermissionEngine(cfg), self.store, self.memory,
                            Runner(max_seconds=10))
        result = tools.invoke("run_command", {"argv": ["true"]},
                              ToolContext(task_id="t", step=1))
        self.assertTrue(result.ok, result.output)

    def test_write_to_protected_path_is_denied_and_recorded(self) -> None:
        prompts = self.cfg.paths.prompts
        before = (prompts / "GROWTH.md").read_text(encoding="utf-8")
        result = self.invoke("write_file", path=str(prompts / "GROWTH.md"), content="weakened")
        self.assertFalse(result.ok)
        self.assertTrue(result.denied)
        self.assertIn("DENIED", result.output)
        self.assertEqual((prompts / "GROWTH.md").read_text(encoding="utf-8"), before)
        violations = self.store.events(event_type="boundary_violation")
        self.assertEqual(len(violations), 1)
        self.assertEqual(json.loads(violations[0]["payload"])["category"], "protected")

    def test_write_outside_roots_is_denied(self) -> None:
        result = self.invoke("write_file", path="/tmp/livium-escape-attempt.txt", content="nope")
        self.assertTrue(result.denied)
        self.assertFalse(pathlib.Path("/tmp/livium-escape-attempt.txt").exists())

    def test_oversized_write_is_refused(self) -> None:
        config = self.config(LIVIUM_MAX_FILE_WRITE_BYTES="100")
        config.ensure_dirs()
        tools = ToolRegistry(config, self.engine, self.store, self.memory, self.runner)
        result = tools.invoke("write_file", {"path": "big.txt", "content": "x" * 500}, self.ctx)
        self.assertFalse(result.ok)
        self.assertIn("write limit", result.output)

    def test_write_is_atomic_no_temp_file_left_behind(self) -> None:
        self.invoke("write_file", path="atomic.txt", content="content")
        leftovers = [p.name for p in self.cfg.paths.workspace.iterdir()
                     if p.name.startswith(".livium-tmp-")]
        self.assertEqual(leftovers, [])

    def test_reading_a_missing_file_reports_not_found(self) -> None:
        result = self.invoke("read_file", path="nope.txt")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_found")

    def test_list_dir_shows_real_entries(self) -> None:
        self.invoke("write_file", path="one.txt", content="1")
        self.invoke("write_file", path="two.txt", content="22")
        result = self.invoke("list_dir", path=".")
        self.assertTrue(result.ok)
        names = {entry["name"] for entry in result.data["entries"]}
        self.assertEqual(names, {"one.txt", "two.txt"})
        sizes = {entry["name"]: entry["bytes"] for entry in result.data["entries"]}
        self.assertEqual(sizes["two.txt"], 2)

    def test_sha256_tool_matches_a_real_digest(self) -> None:
        self.invoke("write_file", path="h.txt", content="digest me")
        result = self.invoke("sha256", path="h.txt")
        expected = hashlib.sha256(b"digest me").hexdigest()
        self.assertIn(expected, result.output)

    def test_reading_the_repository_is_allowed_but_writing_it_is_not(self) -> None:
        read = self.invoke("read_file", path=str(self.cfg.paths.prompts / "FOUNDATIONAL.md"))
        self.assertTrue(read.ok)
        write = self.invoke("write_file", path=str(self.cfg.paths.prompts / "FOUNDATIONAL.md"),
                            content="x")
        self.assertTrue(write.denied)


class CommandToolTests(ToolFixture):
    def test_real_process_runs_and_exit_code_is_observed(self) -> None:
        result = self.invoke("run_command", argv=["python3", "-c", "print('real output')"])
        self.assertTrue(result.ok)
        self.assertIn("real output", result.output)
        self.assertEqual(result.data["exit_code"], 0)

    def test_nonzero_exit_is_reported_as_failure_not_success(self) -> None:
        result = self.run_ok(argv=["python3", "-c", "raise SystemExit(7)"])
        self.assertFalse(result.ok)
        self.assertEqual(result.data["exit_code"], 7)

    def test_denied_binary_is_refused_and_never_executed(self) -> None:
        marker = self.cfg.paths.workspace / "should-not-exist.txt"
        result = self.invoke("run_command",
                             argv=["rm", "-f", str(marker)])
        self.assertTrue(result.denied)
        self.assertIn("denylist", result.output)

    def test_shell_string_is_refused(self) -> None:
        result = self.invoke("run_command", argv="echo hi > out.txt")
        self.assertFalse(result.ok)
        self.assertIn("array of strings", result.output)

    def test_metacharacters_in_arguments_are_literal_not_shell_syntax(self) -> None:
        """The real guarantee is "no shell", not "no semicolons".

        Livium spawns programs directly with an argv list, so `;` and `&&` cannot
        chain anything. This test asserts the guarantee by observing the output:
        echo receives the metacharacters as literal arguments.
        """

        result = self.invoke("run_command", argv=["echo", "hi", "&&", "echo", "bye"])
        self.assertTrue(result.ok, result.output)
        self.assertIn("hi && echo bye", result.output)

    def test_shell_interpreters_are_refused(self) -> None:
        for shell in ("sh", "bash", "zsh"):
            with self.subTest(shell=shell):
                result = self.invoke("run_command", argv=[shell, "-c", "echo escaped"])
                self.assertTrue(result.denied)
                self.assertIn("shell", result.output)

    def test_timeout_kills_the_process_and_says_so(self) -> None:
        result = self.run_ok(argv=["python3", "-c", "import time; time.sleep(30)"],
                             timeout_seconds=2)
        self.assertFalse(result.ok)
        self.assertTrue(result.data["timed_out"])
        self.assertIn("killed", result.output)

    def test_argument_error_is_reported_rather_than_silently_ignored(self) -> None:
        result = self.tools.invoke("run_command", {"_raw": "garbage"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bad_arguments")

    def test_unknown_tool_is_reported_with_the_available_list(self) -> None:
        result = self.invoke("teleport", destination="mars")
        self.assertFalse(result.ok)
        self.assertIn("unknown tool", result.output)
        self.assertIn("read_file", result.output)

    def test_execution_records_whether_the_network_was_isolated(self) -> None:
        result = self.invoke("run_command", argv=["true"])
        self.assertIn("network_isolated", result.data)
        self.assertEqual(result.data["network_isolated"], self.runner.isolation.network_isolated)


class IsolationTests(LiviumTestCase):
    def test_isolation_detection_reports_what_it_verified(self) -> None:
        report = detect_isolation()
        self.assertIn(report.mechanism, ("unshare-netns", "none"))
        if report.network_isolated:
            self.assertEqual(report.mechanism, "unshare-netns")
            # And it must be honest about the part it does not provide.
            self.assertFalse(report.filesystem_confined)
            self.assertIn("NOT confined", report.detail)

    def test_output_is_capped(self) -> None:
        runner = Runner(max_seconds=30, max_output_bytes=200)
        workspace = self.config().paths.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        result = runner.run(["python3", "-c", "print('x' * 5000)"], cwd=workspace)
        self.assertTrue(result.truncated)
        self.assertLess(len(result.stdout), 500)

    def test_missing_binary_is_reported_as_such(self) -> None:
        runner = Runner(max_seconds=10)
        workspace = self.config().paths.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        result = runner.run(["definitely-not-a-real-binary-xyz"], cwd=workspace)
        self.assertNotEqual(result.exit_code, 0)
        # The message must name the thing that was missing, whichever layer detects it.
        self.assertIn("definitely-not-a-real-binary-xyz", result.stderr)


class MemoryToolTests(ToolFixture):
    def test_memory_write_then_search(self) -> None:
        write = self.invoke("memory_write", kind="experience",
                           content="reading the file before editing avoided a wrong assumption",
                           confidence=0.7, verification="tested", evidence="read_file output")
        self.assertTrue(write.ok, write.output)
        search = self.invoke("memory_search", query="reading the file before editing")
        self.assertTrue(search.ok)
        self.assertIn("wrong assumption", search.output)
        self.assertEqual(search.data["records"][0]["verification"], "tested")

    def test_memory_write_rejects_the_foundational_kind(self) -> None:
        result = self.invoke("memory_write", kind="foundational",
                            content="new permanent rule invented by the agent")
        self.assertTrue(result.denied)
        self.assertIn("protected", result.output)

    def test_memory_write_rejects_unknown_verification_state(self) -> None:
        result = self.invoke("memory_write", kind="experience", content="x",
                            verification="totally-verified")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bad_arguments")

    def test_memory_update_and_forget_by_id(self) -> None:
        write = self.invoke("memory_write", kind="decision", content="we will use SQLite",
                           key="db", confidence=0.5)
        memory_id = write.data["id"]
        update = self.invoke("memory_update", id=memory_id, content="we will use SQLite FTS5",
                            verification="verified", confidence=0.9)
        self.assertTrue(update.ok)
        record = self.memory.get(memory_id)
        assert record is not None
        self.assertEqual(record.verification, "verified")
        forget = self.invoke("memory_forget", id=memory_id, reason="superseded by a later decision")
        self.assertTrue(forget.ok)
        self.assertIsNone(self.memory.by_key("decision", "db"))

    def test_empty_search_is_an_error_not_a_silent_empty_result(self) -> None:
        result = self.invoke("memory_search", query="")
        self.assertFalse(result.ok)


class TavilySearchToolTests(ToolFixture):
    def make_allowed_tool(self):
        config = self.config(LIVIUM_NETWORK_ALLOWLIST="api.tavily.com")
        engine = PermissionEngine(config)
        return ToolRegistry(config, engine, self.store, self.memory, self.runner)

    def test_web_search_returns_bounded_snippets_and_source_urls(self) -> None:
        registry = self.make_allowed_tool()
        response_data = {
            "request_id": "req-123",
            "results": [{
                "title": "A useful source", "url": "https://example.org/article",
                "content": "A short evidence snippet.", "score": 0.91,
                "published_date": "2026-10-15",
            }],
        }
        response = io.BytesIO(json.dumps(response_data).encode())
        response.status = 200
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "unit-test-secret"}), \
             mock.patch("livium.tools.urllib.request.build_opener", return_value=opener):
            result = registry.invoke("web_search", {
                "query": "current facts", "max_results": 99, "topic": "news",
            }, self.ctx)

        self.assertTrue(result.ok, result.output)
        self.assertIn("https://example.org/article", result.output)
        self.assertIn("A short evidence snippet.", result.output)
        self.assertNotIn("unit-test-secret", result.output)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_header("Authorization"), "Bearer unit-test-secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["max_results"], 10)
        self.assertEqual(payload["topic"], "news")
        self.assertEqual(result.data["results"][0]["url"], "https://example.org/article")

    def test_web_search_requires_an_explicit_host_allowlist(self) -> None:
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "unit-test-secret"}):
            result = self.invoke("web_search", query="hello")
        self.assertFalse(result.ok)
        self.assertTrue(result.denied)
        self.assertEqual(result.category, "network")

    def test_web_search_requires_key_without_contacting_provider(self) -> None:
        registry = self.make_allowed_tool()
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": ""}), \
             mock.patch("livium.tools.urllib.request.build_opener") as opener:
            result = registry.invoke("web_search", {"query": "hello"}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_api_key")
        opener.assert_not_called()


class CapabilityProposalToolTests(ToolFixture):
    def test_proposal_records_a_proposal_and_implements_nothing(self) -> None:
        result = self.invoke(
            "request_capability", name="word_count",
            requirement="count words in a file without invoking a shell",
            reuse_analysis="read_file can fetch text but wc needs a path argument; no existing "
                           "tool counts words in content the agent already holds",
        )
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.data["status"], "proposed")
        self.assertIn("Nothing was implemented", result.output)
        # The claim "nothing was implemented" must be true:
        self.assertFalse((self.cfg.paths.capability_lab / "word_count").exists())
        from livium.capabilities import CapabilityRegistry

        registry = CapabilityRegistry(self.store, lab=self.cfg.paths.capability_lab)
        self.assertEqual(registry.get("word_count").status, "proposed")

    def test_proposal_requires_a_reuse_analysis(self) -> None:
        result = self.invoke("request_capability", name="thing", requirement="do a thing")
        self.assertFalse(result.ok)
        self.assertIn("reuse_analysis", result.output)

    def test_proposal_with_a_bad_name_is_refused(self) -> None:
        result = self.invoke("request_capability", name="Bad Name!", requirement="x",
                            reuse_analysis="checked")
        self.assertFalse(result.ok)


class FinishTaskToolTests(ToolFixture):
    def test_finish_task_only_asks_the_harness(self) -> None:
        result = self.invoke("finish_task", summary="all done I promise", status="complete")
        self.assertTrue(result.ok)
        self.assertIn("harness now checks", result.output)
        event = self.store.events(event_type="task_finished")[0]
        self.assertEqual(json.loads(event["payload"])["status"], "complete")

    def test_invalid_status_refused(self) -> None:
        result = self.invoke("finish_task", summary="x", status="triumphant")
        self.assertFalse(result.ok)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
