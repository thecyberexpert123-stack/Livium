"""Permission-boundary tests.

These are the tests that matter most for the "real boundary" claim, so they check
observable refusals rather than the shape of a Decision object.
"""

from __future__ import annotations

import pathlib
import unittest

from support import LiviumTestCase, with_protected

from livium.permissions import ALLOW, APPROVAL, DENY, PermissionEngine


class PathPolicyTests(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.config()
        self.engine = PermissionEngine(self.config)
        self.workspace = self.config.paths.workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    def test_write_inside_workspace_allowed(self) -> None:
        decision = self.engine.check_write(self.workspace / "note.txt")
        self.assertTrue(decision.allowed, decision.reason)

    def test_write_outside_roots_denied(self) -> None:
        decision = self.engine.check_write("/etc/passwd")
        self.assertEqual(decision.outcome, DENY)
        self.assertIn("outside the permitted write roots", decision.reason)

    def test_parent_traversal_is_resolved_before_the_check(self) -> None:
        decision = self.engine.check_write(self.workspace / ".." / ".." / "escape.txt")
        self.assertEqual(decision.outcome, DENY)

    def test_symlink_escape_is_resolved(self) -> None:
        """A symlink inside the workspace pointing outside it must not become a door."""

        link = self.workspace / "sneaky"
        try:
            link.symlink_to("/etc")
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("symlinks unavailable in this environment")
        decision = self.engine.check_write(link / "passwd")
        self.assertEqual(decision.outcome, DENY, decision.reason)

    def test_repo_root_is_readable_but_not_writable(self) -> None:
        self.assertTrue(self.engine.check_read(self.config.paths.prompts / "GROWTH.md").allowed)
        decision = self.engine.check_write(self.config.paths.prompts / "GROWTH.md")
        self.assertEqual(decision.outcome, DENY)
        self.assertIn("protected", decision.reason)

    def test_protected_module_write_denied(self) -> None:
        decision = self.engine.check_write(
            pathlib.Path(__file__).resolve().parent.parent / "livium" / "guardian.py"
        )
        self.assertEqual(decision.outcome, DENY)

    def test_tamper_test_uses_injected_protected_path(self) -> None:
        """The engine reads protected paths from config, not from a hardcoded list."""

        victim = self.home.dir / "victim.py"
        victim.write_text("original", encoding="utf-8")
        config = with_protected(self.config, victim)
        engine = PermissionEngine(config)
        self.assertTrue(engine.is_protected(victim))
        self.assertEqual(engine.check_write(victim).outcome, DENY)


class CommandPolicyTests(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine = PermissionEngine(self.config())

    def test_allowlisted_binary_allowed(self) -> None:
        self.assertTrue(self.engine.check_command(["python3", "-m", "unittest"]).allowed)

    def test_denylisted_binary_refused(self) -> None:
        for argv in (["rm", "-rf", "/"], ["curl", "http://example.com"], ["sudo", "ls"],
                     ["sh", "-c", "echo hi"], ["pip", "install", "requests"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.engine.check_command(argv).outcome, DENY)

    def test_unknown_binary_requires_approval_not_silent_allow(self) -> None:
        decision = self.engine.check_command(["some-unknown-tool", "--flag"])
        self.assertEqual(decision.outcome, APPROVAL)

    def test_metacharacters_are_treated_as_literal_arguments(self) -> None:
        """Narrowed on evidence: with no shell, these are ordinary characters.

        The denial rule that used to live here cost real capability (Python
        one-liners, SQL, format strings) and bought nothing, because Livium never
        spawns a shell. What actually protects us is tested separately: the program
        must be allowlisted, shell interpreters are denied, and a shell *string* can
        never stand in for an argv list.
        """

        for argv in (["echo", "a && b"], ["echo", "semi;colon"], ["echo", "$(id)"],
                     ["echo", "pipe|char"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.engine.check_command(argv).outcome, ALLOW)

    def test_shell_interpreters_are_refused_so_metacharacters_stay_literal(self) -> None:
        for shell in ("sh", "bash", "dash", "zsh", "fish"):
            with self.subTest(shell=shell):
                decision = self.engine.check_command([shell, "-c", "echo hi"])
                self.assertEqual(decision.outcome, DENY)
                self.assertIn("shell", decision.reason)

    def test_shell_string_instead_of_argv_refused(self) -> None:
        self.assertEqual(self.engine.check_command("ls -la").outcome, DENY)

    def test_read_only_git_allowed_and_history_rewriting_refused(self) -> None:
        self.assertTrue(self.engine.check_command(["git", "status"]).allowed)
        self.assertTrue(self.engine.check_command(["git", "log", "--oneline"]).allowed)
        for argv in (["git", "push"], ["git", "reset", "--hard"], ["git", "commit", "-m", "x"],
                     ["git", "clean", "-fd"], ["git", "remote", "add", "o", "u"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.engine.check_command(argv).outcome, DENY)

    def test_git_unknown_subcommand_requires_approval(self) -> None:
        self.assertEqual(self.engine.check_command(["git", "weirdcmd"]).outcome, APPROVAL)

    def test_in_place_sed_refused(self) -> None:
        self.assertEqual(self.engine.check_command(["sed", "-i", "s/a/b/", "f.txt"]).outcome, DENY)
        self.assertTrue(self.engine.check_command(["sed", "s/a/b/", "f.txt"]).allowed)

    def test_control_characters_refused(self) -> None:
        self.assertEqual(self.engine.check_command(["echo", "a\nb"]).outcome, DENY)
        self.assertEqual(self.engine.check_command(["echo", "a\x00b"]).outcome, DENY)


class NetworkPolicyTests(LiviumTestCase):
    def test_network_is_closed_by_default(self) -> None:
        engine = PermissionEngine(self.config())
        decision = engine.check_network("https://api.openai.com/v1/chat/completions")
        self.assertEqual(decision.outcome, DENY)
        self.assertIn("LIVIUM_NETWORK_ALLOWLIST", decision.reason)

    def test_allowlist_permits_only_listed_hosts(self) -> None:
        engine = PermissionEngine(self.config(LIVIUM_NETWORK_ALLOWLIST="example.com,other.org"))
        self.assertTrue(engine.check_network("https://example.com/x").allowed)
        self.assertEqual(engine.check_network("https://evil.com/x").outcome, DENY)

    def test_non_http_schemes_refused(self) -> None:
        engine = PermissionEngine(self.config())
        self.assertEqual(engine.check_network("file:///etc/passwd").outcome, DENY)


class VerificationReadTests(LiviumTestCase):
    """The one read that a write-authorising configuration has to permit.

    Without this, an operation a human approved can be performed but never checked:
    the criterion that inspects the written file is refused for being outside the read
    roots, and the task closes 'not verified' for a reason that has nothing to do with
    the work.
    """

    def engine(self, **env: str):
        from livium.permissions import PermissionEngine

        return PermissionEngine(self.config(**env))

    def test_approval_write_root_is_readable_only_for_verification(self) -> None:
        restricted = self.home.dir / "restricted"
        restricted.mkdir(parents=True, exist_ok=True)
        target = restricted / "release-notes.md"
        engine = self.engine(LIVIUM_APPROVAL_WRITE_ROOTS=str(restricted))

        self.assertFalse(engine.check_read(target).allowed,
                         "an approval root is not a read root: the agent cannot browse it")
        decision = engine.check_verification_read(target)
        self.assertTrue(decision.allowed, decision.reason)
        self.assertIn("approval write root", decision.reason)

    def test_paths_outside_every_root_are_refused_for_verification_too(self) -> None:
        outside = pathlib.Path("/etc/shadow")
        engine = self.engine(LIVIUM_APPROVAL_WRITE_ROOTS=str(self.home.dir / "restricted"))
        decision = engine.check_verification_read(outside)
        self.assertFalse(decision.allowed)
        self.assertIn("outside the permitted read roots", decision.reason)

    def test_read_roots_still_work_for_verification(self) -> None:
        inside = self.config().paths.workspace / "notes.md"
        inside.parent.mkdir(parents=True, exist_ok=True)
        engine = self.engine()
        self.assertTrue(engine.check_verification_read(inside).allowed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
