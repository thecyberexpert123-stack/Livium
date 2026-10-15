"""Execution tests: real processes, real isolation, real kills.

These tests run actual subprocesses on this host. Nothing here is mocked, because
the properties under test -- that a namespace really removes the network, that a
runaway process is really killed, that output really is capped -- cannot be
established any other way.

Where the host cannot provide a mechanism (for example, no user namespaces), the
test asserts the honest report rather than skipping: Livium is allowed to say
"isolation unavailable", it is not allowed to claim isolation it did not verify.
"""

from __future__ import annotations

import pathlib
import os
import time
import unittest

from support import LiviumTestCase

from livium.execution import Runner, detect_isolation
from livium.permissions import PermissionEngine


class IsolationTests(LiviumTestCase):
    def test_isolation_report_matches_what_the_host_can_do(self) -> None:
        """The reported mechanism is the one actually used to wrap commands."""

        report = detect_isolation()
        runner = Runner(max_seconds=10.0)
        self.assertEqual(runner.isolation.mechanism, report.mechanism)
        self.assertEqual(runner.isolation.network_isolated, report.network_isolated)
        # Filesystem confinement is never claimed: a writable workspace does not
        # confine a program that can read /etc.
        self.assertFalse(runner.isolation.filesystem_confined)

    def test_network_really_is_unreachable_inside_the_runner(self) -> None:
        """If isolation is claimed, prove it by trying to escape from inside."""

        runner = Runner(max_seconds=30.0)
        if not runner.isolation.network_isolated:
            self.skipTest(f"host provides no network isolation: {runner.isolation.detail}")

        workspace = self.config().paths.workspace
        probe = (
            "import socket, sys\n"
            "s = socket.socket(); s.settimeout(3)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 80))\n"
            "except OSError as exc:\n"
            "    print('blocked', type(exc).__name__); sys.exit(0)\n"
            "print('connected'); sys.exit(3)\n"
        )
        result = runner.run(["python3", "-c", probe], cwd=workspace)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("blocked", result.stdout)
        self.assertTrue(result.network_isolated)

    def test_wrap_is_the_mechanism_it_reports(self) -> None:
        runner = Runner(max_seconds=5.0)
        wrapped = runner.wrap(["python3", "-c", "print(1)"])
        if runner.isolation.mechanism == "unshare-netns":
            self.assertEqual(wrapped[:2], ["unshare", "-rn"])
        else:
            self.assertEqual(wrapped, ["python3", "-c", "print(1)"])


class RunnerContractTests(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.cfg.ensure_dirs()
        self.workspace = self.cfg.paths.workspace
        self.runner = Runner(max_seconds=10.0)

    def test_a_real_exit_code_is_reported(self) -> None:
        result = self.runner.run(["python3", "-c", "raise SystemExit(7)"], cwd=self.workspace)
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.ok)

    def test_stdout_and_stderr_are_kept_apart(self) -> None:
        result = self.runner.run(
            ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            cwd=self.workspace,
        )
        self.assertIn("out", result.stdout)
        self.assertNotIn("err", result.stdout)
        self.assertIn("err", result.stderr)

    def test_a_runaway_process_is_killed_at_the_limit(self) -> None:
        started = time.monotonic()
        result = self.runner.run(
            ["python3", "-c", "import time; time.sleep(60)"], cwd=self.workspace, timeout=1.0)
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, -9)
        self.assertLess(elapsed, 15.0, "the process was not killed promptly")
        self.assertIn("exceeded the 1s limit", result.stderr)

    def test_a_caller_cannot_raise_the_ceiling(self) -> None:
        """A per-call timeout may tighten the limit, never extend it."""

        bounded = Runner(max_seconds=1.0)
        started = time.monotonic()
        result = bounded.run(
            ["python3", "-c", "import time; time.sleep(30)"], cwd=self.workspace, timeout=9999)
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 15.0)
        self.assertIn("1s limit", result.stderr)

    def test_output_is_capped_and_says_it_was_capped(self) -> None:
        capped = Runner(max_seconds=10.0, max_output_bytes=500)
        result = capped.run(
            ["python3", "-c", "print('x' * 50000)"], cwd=self.workspace)
        self.assertTrue(result.truncated)
        self.assertIn("[livium] stdout truncated", result.stdout)
        self.assertLess(len(result.stdout), 600)

    def test_a_missing_working_directory_is_created_not_guessed_at(self) -> None:
        fresh = self.cfg.paths.home / "brand-new-dir"
        result = self.runner.run(["python3", "-c", "print('ok')"], cwd=fresh)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertTrue(fresh.is_dir())

    def test_an_unusable_working_directory_is_reported_explicitly(self) -> None:
        """A path that cannot be a directory produces our message, not a raw ENOENT."""

        blocker = self.cfg.paths.home / "not-a-dir"
        blocker.write_text("i am a file", encoding="utf-8")
        result = self.runner.run(["python3", "-c", "print('never')"], cwd=blocker / "child")
        self.assertEqual(result.exit_code, 127)
        self.assertIn("cannot create working directory", result.stderr)
        self.assertIn(str(blocker), result.stderr)

    def test_an_unknown_binary_is_reported_by_name(self) -> None:
        result = self.runner.run(["definitely-not-a-real-binary-xyz"], cwd=self.workspace)
        self.assertEqual(result.exit_code, 127)
        # Under `unshare` the message comes from the wrapper, so assert the guarantee
        # (the binary is named) rather than another program's wording.
        self.assertIn("definitely-not-a-real-binary-xyz", result.stderr)

    def test_the_child_never_inherits_the_parent_environment(self) -> None:
        """A credential in the agent's environment must not reach a child process.

        This is the property that matters: `run_command` takes an argv and nothing
        else, so there is no channel through which the model could inject or read
        environment variables.
        """

        import json
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {
            "LIVIUM_API_KEY": "must-not-leak",
            "OPENAI_API_KEY": "must-not-leak-either",
            "SECRET_TOKEN": "nope",
        }):
            result = self.runner.run(
                ["python3", "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
                cwd=self.workspace,
            )
        child = json.loads(result.stdout)
        for name in ("LIVIUM_API_KEY", "OPENAI_API_KEY", "SECRET_TOKEN"):
            self.assertNotIn(name, child)
        self.assertEqual(child.get("HOME"), str(self.workspace))
        # Capability code is first-party and must be importable in the child.
        self.assertIn(str(pathlib.Path(__file__).resolve().parent.parent), child["PYTHONPATH"])
        # And there is no parameter through which a caller (or the model) could add one.
        with self.assertRaises(TypeError):
            self.runner.run(["echo", "x"], cwd=self.workspace, env={"LIVIUM_API_KEY": "x"})

    def test_capability_code_can_import_livium_in_the_child(self) -> None:
        result = self.runner.run(
            ["python3", "-c", "import livium.tools as t; print(t.__name__)"], cwd=self.workspace)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("livium.tools", result.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX RLIMIT_AS is unavailable on Windows")
    def test_address_space_is_capped(self) -> None:
        """A runaway allocation fails inside the child instead of harming the host."""

        result = self.runner.run(
            ["python3", "-c", "\n".join([
                "try:",
                "    block = bytearray(4 * 1024 * 1024 * 1024)",
                "except MemoryError:",
                "    print('refused'); raise SystemExit(0)",
                "print('allocated')",
            ])],
            cwd=self.workspace,
        )
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("refused", result.stdout)


class CommandPermissionTests(LiviumTestCase):
    """The allow-list is enforced by the engine, and it cannot be widened by arguments."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.config()
        self.engine = PermissionEngine(self.cfg)

    def test_a_denied_binary_is_refused_with_a_reason(self) -> None:
        decision = self.engine.check_command(["rm", "-rf", "/"])
        self.assertFalse(decision.allowed)
        self.assertIn("rm", decision.reason)

    def test_a_binary_outside_the_allowlist_is_refused(self) -> None:
        decision = self.engine.check_command(["definitely-not-allowed"])
        self.assertFalse(decision.allowed)

    def test_shells_are_refused_so_metacharacters_stay_literal(self) -> None:
        for shell in ("sh", "bash", "zsh", "dash"):
            with self.subTest(shell=shell):
                self.assertFalse(self.engine.check_command([shell, "-c", "echo hi"]).allowed)

    def test_control_characters_are_refused(self) -> None:
        self.assertFalse(self.engine.check_command(["python3", "-c", "print(1)\nrm -rf /"]).allowed)
        self.assertFalse(self.engine.check_command(["printf", "a\rb"]).allowed)

    def test_an_allowlisted_binary_is_permitted(self) -> None:
        decision = self.engine.check_command(["python3", "-c", "print('hello')"])
        self.assertTrue(decision.allowed, decision.reason)

    def test_metacharacters_reach_the_program_as_text(self) -> None:
        """`;` is not shell syntax here: no shell is ever spawned."""

        cfg = self.config()
        cfg.ensure_dirs()
        runner = Runner(max_seconds=10.0)
        result = runner.run(["echo", "hi && echo bye"], cwd=cfg.paths.workspace)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hi && echo bye", result.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
