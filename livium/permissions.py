"""Permission enforcement.

What this module actually is
----------------------------
An in-process policy engine. It sits between the model's tool calls and the
operating system, and it is the only route by which the model can reach the
filesystem or a subprocess. The model cannot emit a tool call that skips it.

What this module is NOT
-----------------------
It is not an OS-level security sandbox, and it is not named one. It does not
confine paths or the network for a subprocess once that subprocess is running:
`run_command` executes allowlisted binaries without a shell, but a Python script
launched that way runs with the privileges of the Livium process. Anyone who
needs a real isolation boundary for untrusted third-party code should run Livium
itself inside a container or VM. `docs/LIMITATIONS.md` states this plainly.

The guarantee it does provide, and the one the Growth Guardian depends on:
the *model*, acting through Livium's tools, cannot read or write outside its
roots, cannot execute denied binaries, cannot reach unlisted hosts, and cannot
modify the files that encode its own governance.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
from typing import Iterable, Sequence

from .config import Config

ALLOW = "allow"
DENY = "deny"
APPROVAL = "approval_required"

# Read-only git surface. Anything that rewrites history, moves refs, touches
# remotes, or rewrites the working tree is refused: those are consequential and
# a human should run them.
GIT_ALLOWED_SUBCOMMANDS = frozenset(
    {"status", "log", "diff", "show", "rev-parse", "ls-files", "describe",
     "blame", "shortlog", "cat-file", "grep", "remote-get-url", "version"}
)
#: Names that describe nobody. An approval, a promotion or a baseline recorded under
#: one of these is not attributable to a person, which is the only property that makes
#: it a decision rather than a state change the agent could have arranged for itself.
UNATTRIBUTED_NAMES = frozenset({
    "", "anonymous", "anon", "unknown", "unspecified", "none", "nobody", "n/a", "na", "-",
    "agent", "livium", "self", "auto", "model", "system", "root", "admin",
})


GIT_REFUSED_SUBCOMMANDS = frozenset(
    {"push", "pull", "fetch", "clone", "commit", "merge", "rebase", "reset",
     "clean", "checkout", "restore", "stash", "switch", "config", "remote",
     "tag", "branch", "init", "rm", "mv", "apply", "am", "cherry-pick",
     "revert", "gc", "filter-branch", "update-ref", "worktree", "submodule"}
)

# Control characters are refused in arguments: a NUL cannot be passed to execve and
# a newline breaks log and statement parsing.
_SAFE_ARG = re.compile(r"^[^\x00\n\r]*$")
# Shell metacharacters are NOT refused in arguments, and that is a deliberate,
# evidence-driven decision. Livium never invokes a shell (subprocess without
# shell=True, no interpolation anywhere), so `;`, `|`, `&`, `$` and `>` inside an
# argv element are literal characters -- they cannot chain or redirect anything.
# Refusing them blocked legitimate, useful calls (Python one-liners, SQL, format
# strings) while adding no protection, so the rule was narrowed to what actually
# matters: the program itself must be allowlisted, shell interpreters are denied
# outright, and a shell string can never stand in for an argv list.
_SHELL_INTERPRETERS = frozenset({
    "sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish",
    "cmd", "powershell", "pwsh", "command", "powershell_ise",
})


@dataclasses.dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    category: str  # path | command | network | protected

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW


class PolicyDenied(PermissionError):
    """Raised by the tool layer when the engine returns DENY or APPROVAL."""

    def __init__(self, decision: Decision):
        super().__init__(f"{decision.category}: {decision.reason}")
        self.decision = decision


class PermissionEngine:
    """Evaluates every agent-initiated filesystem, process and network request."""

    def __init__(self, config: Config):
        self.config = config
        self._read_roots = tuple(self._norm(p) for p in config.perms.read_roots)
        #: Paths a human authorised as approvable writes. Kept separately because they
        #: gain one specific privilege (a verification read) and nothing else.
        self._approval_write_roots = tuple(
            self._norm(p) for p in getattr(config.perms, "approval_write_roots", ()) or ()
        )
        self._write_roots = tuple(self._norm(p) for p in config.perms.write_roots)
        self._protected = tuple(self._norm(p) for p in config.perms.protected_paths)
        self._allowed_binaries = {b.lower() for b in config.perms.allowed_binaries}
        self._denied_binaries = {b.lower() for b in config.perms.denied_binaries}
        self._network_allowlist = {h.lower() for h in config.perms.network_allowlist}
        self._approval_binaries = {b for b in config.perms.approval_binaries}
        self._approval_roots = tuple(
            self._norm(p) for p in getattr(config.perms, "approval_write_roots", ()))
        self._protected_files: tuple[pathlib.Path, ...] = tuple(
            p for p in self._protected if p.suffix
        )

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _norm(path: str | pathlib.Path) -> pathlib.Path:
        """Resolve including symlinks, so `..` and symlink tricks cannot escape.

        Uses a non-strict resolve because the target may not exist yet (e.g. a
        file about to be written).
        """
        return pathlib.Path(path).expanduser().resolve()

    def _within(self, path: pathlib.Path, roots: Sequence[pathlib.Path]) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def is_protected(self, path: str | pathlib.Path) -> bool:
        target = self._norm(path)
        for protected in self._protected:
            if target == protected or protected in target.parents:
                return True
        return False

    # -- filesystem -------------------------------------------------------
    def check_read(self, path: str | pathlib.Path) -> Decision:
        target = self._norm(path)
        if self._within(target, self._read_roots):
            return Decision(ALLOW, f"read permitted: {target}", "path")
        return Decision(
            DENY,
            f"read refused: {target} is outside the permitted read roots "
            f"({', '.join(str(r) for r in self._read_roots)})",
            "path",
        )

    def check_verification_read(self, path: str | pathlib.Path) -> Decision:
        """Reading, for the purpose of checking an acceptance criterion.

        Identical to ``check_read`` except for one case that would otherwise be
        unsatisfiable: a path an operator has authorised as an approval write root.
        The agent may write there only with a recorded human approval, so the harness
        must be able to read what it wrote -- otherwise an approved operation can never
        be verified and every such task ends 'not verified' for a reason that has
        nothing to do with the work. The relaxation is confined to paths a human put in
        the configuration; nothing else gains read access, and the decision is returned
        with its reason rather than being silent.
        """

        decision = self.check_read(path)
        if decision.allowed:
            return decision
        target = self._norm(path)
        for root in getattr(self, "_approval_write_roots", ()) or ():
            if self._within(target, (self._norm(root),)):
                return Decision(
                    ALLOW,
                    f"read permitted for verification: {target} is an approval write root "
                    f"({root}); the write there required a recorded human approval",
                    "path",
                )
        return decision

    def check_write(self, path: str | pathlib.Path) -> Decision:
        target = self._norm(path)
        if self.is_protected(target):
            return Decision(
                DENY,
                f"write refused: {target} is a protected path governing Livium's "
                f"permissions or instructions. Propose a change instead; a human "
                f"must apply it.",
                "protected",
            )
        if self._within(target, self._write_roots):
            return Decision(ALLOW, f"write permitted: {target}", "path")
        if self._approval_roots and self._within(target, self._approval_roots):
            return Decision(
                APPROVAL,
                f"write needs approval: {target} is inside an approval root "
                f"({', '.join(str(r) for r in self._approval_roots)}). A human decides, not "
                f"the agent; see `livium approvals`.",
                "path",
            )
        return Decision(
            DENY,
            f"write refused: {target} is outside the permitted write roots "
            f"({', '.join(str(r) for r in self._write_roots)})",
            "path",
        )

    # -- process execution ------------------------------------------------
    def check_command(self, argv: Sequence[str]) -> Decision:
        if not argv:
            return Decision(DENY, "empty command", "command")
        if isinstance(argv, str):  # defensive: a shell string would defeat the engine
            return Decision(
                DENY,
                "commands must be passed as an argument list, never a shell string",
                "command",
            )

        for arg in argv:
            if not isinstance(arg, str) or not _SAFE_ARG.match(arg):
                return Decision(DENY, f"argument contains control characters: {arg!r}", "command")
        binary = pathlib.Path(argv[0]).name.lower()
        if os.name == "nt" and pathlib.Path(binary).suffix in {".exe", ".com"}:
            # Windows names native programs with extensions (e.g. python.exe),
            # while the portable allow-list intentionally stores logical names.
            binary = pathlib.Path(binary).stem
        if binary in _SHELL_INTERPRETERS:
            return Decision(
                DENY,
                f"refused: '{binary}' is a shell. Livium runs programs directly with an argv "
                f"list so that no shell interpretation is ever possible.",
                "command",
            )
        if binary in self._denied_binaries:
            return Decision(
                DENY, f"refused: '{binary}' is on the denylist", "command"
            )
        if binary not in self._allowed_binaries:
            return Decision(
                APPROVAL,
                f"refused without approval: '{binary}' is not on the allowlist",
                "command",
            )

        if binary.startswith("git") and len(argv) > 1:
            sub = next((a for a in argv[1:] if not a.startswith("-")), "")
            if sub in GIT_REFUSED_SUBCOMMANDS:
                return Decision(
                    DENY,
                    f"refused: 'git {sub}' changes repository or remote state and "
                    f"requires a human. Use recording instead of rewriting history.",
                    "command",
                )
            if sub and sub not in GIT_ALLOWED_SUBCOMMANDS:
                return Decision(
                    APPROVAL,
                    f"approval required: 'git {sub}' is outside the read-only git "
                    f"subcommands Livium may run",
                    "command",
                )

        if binary == "sed" and any(a == "-i" or a.startswith("-i") for a in argv[1:2]):
            return Decision(
                DENY,
                "refused: 'sed -i' edits files in place outside the audited "
                "write_file path",
                "command",
            )

        return Decision(ALLOW, f"permitted: {argv[0]}", "command")

    # -- network ----------------------------------------------------------
    def check_network(self, url: str) -> Decision:
        match = re.match(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?P<host>[^/:?#]+)", url)
        if not match:
            return Decision(DENY, f"refused: unparseable URL {url!r}", "network")
        scheme, host = match.group("scheme").lower(), match.group("host").lower()
        if scheme not in ("http", "https"):
            return Decision(DENY, f"refused: scheme {scheme!r} is not http/https", "network")
        if host in self._network_allowlist:
            return Decision(ALLOW, f"network permitted: {host}", "network")
        return Decision(
            DENY,
            f"refused: host {host!r} is not in LIVIUM_NETWORK_ALLOWLIST "
            f"(currently: {', '.join(sorted(self._network_allowlist)) or 'empty'})",
            "network",
        )

    # -- reporting --------------------------------------------------------
    def describe(self) -> dict:
        return {
            "read_roots": [str(p) for p in self._read_roots],
            "approval_write_roots": [str(p) for p in self._approval_write_roots],
            "write_roots": [str(p) for p in self._write_roots],
            "protected": [str(p) for p in self._protected],
            "allowed_binaries": sorted(self._allowed_binaries),
            "denied_binaries": sorted(self._denied_binaries),
            "network_allowlist": sorted(self._network_allowlist),
            "git_allowed": sorted(GIT_ALLOWED_SUBCOMMANDS),
            "git_refused": sorted(GIT_REFUSED_SUBCOMMANDS),
        }

    def protected_files(self) -> Iterable[pathlib.Path]:
        return self._protected_files
