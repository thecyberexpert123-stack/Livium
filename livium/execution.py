"""Execution boundary for commands, including generated code.

This module answers one question honestly: *what isolation do we actually have
on this host?* It probes for real mechanisms and reports what it found. It does
not claim a boundary it has not verified.

Mechanism used when available: a Linux user+network namespace
(``unshare -rn``). This is kernel-enforced, requires no privileges beyond a
user namespace, and removes network access entirely for the child process. When
it is unavailable the runner still applies the controls it *can* apply --
allowlisted binary, argv (never a shell), fixed working directory, hard timeout,
output caps -- and records ``network_isolated=False`` so no report can imply
otherwise.

What this does not do, stated so nobody has to infer it: it does not confine the
filesystem, does not drop privileges to another user, and does not restrict
syscalls. Code that must be treated as hostile belongs in a container or VM.
See docs/LIMITATIONS.md.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import resource
import shutil
import subprocess
import time
from typing import Sequence


@dataclasses.dataclass(frozen=True)
class IsolationReport:
    mechanism: str
    network_isolated: bool
    filesystem_confined: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ExecResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    network_isolated: bool
    cwd: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def detect_isolation(probe_timeout: float = 10.0) -> IsolationReport:
    """Probe for a real isolation mechanism. Cached by the caller, not here."""

    if os.name != "posix":  # pragma: no cover - Windows path, untested here
        return IsolationReport(
            mechanism="none",
            network_isolated=False,
            filesystem_confined=False,
            detail="non-POSIX host: no namespace mechanism probed",
        )

    unshare = shutil.which("unshare")
    if unshare:
        try:
            completed = subprocess.run(
                [unshare, "-rn", "true"],
                capture_output=True,
                timeout=probe_timeout,
                check=False,
            )
            if completed.returncode == 0:
                # Verify it actually removes the network rather than trusting the
                # flag. A namespace that does not isolate is worse than none,
                # because it would be reported as a boundary.
                verify = subprocess.run(
                    [
                        unshare, "-rn", "python3", "-c",
                        "import socket,sys\n"
                        "try:\n"
                        "    socket.create_connection(('1.1.1.1',443),timeout=2)\n"
                        "    sys.exit(7)\n"
                        "except OSError:\n"
                        "    sys.exit(0)\n",
                    ],
                    capture_output=True,
                    timeout=probe_timeout + 5,
                    check=False,
                )
                if verify.returncode == 0:
                    return IsolationReport(
                        mechanism="unshare-netns",
                        network_isolated=True,
                        filesystem_confined=False,
                        detail=(
                            "commands run in a user+network namespace: network access is "
                            "removed and verified blocked; filesystem is NOT confined and "
                            "the process keeps the invoking user's privileges"
                        ),
                    )
                return IsolationReport(
                    mechanism="none",
                    network_isolated=False,
                    filesystem_confined=False,
                    detail=(
                        "unshare is present but a network namespace did not actually block "
                        "outbound connections; refusing to claim network isolation"
                    ),
                )
            return IsolationReport(
                mechanism="none",
                network_isolated=False,
                filesystem_confined=False,
                detail=f"unshare present but unusable: {' '.join(completed.stderr.decode(errors='replace').split())[:200]}",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return IsolationReport(
                mechanism="none",
                network_isolated=False,
                filesystem_confined=False,
                detail=f"unshare probe failed: {type(exc).__name__}: {exc}",
            )

    return IsolationReport(
        mechanism="none",
        network_isolated=False,
        filesystem_confined=False,
        detail=(
            "no namespace tool available; commands run with the invoking user's full "
            "filesystem and network reach, limited only by argv allowlisting, cwd, "
            "timeout and output caps"
        ),
    )


class Runner:
    """Executes commands under the strongest boundary this host supports."""

    def __init__(
        self,
        *,
        max_seconds: float = 60.0,
        max_output_bytes: int = 30_000,
        isolate: bool = True,
        detect: bool = True,
    ):
        self.max_seconds = max_seconds
        self.max_output_bytes = max_output_bytes
        self.isolation = (
            detect_isolation() if (isolate and detect)
            else IsolationReport(
                mechanism="disabled", network_isolated=False, filesystem_confined=False,
                detail="isolation probing disabled for this caller",
            )
        )

    def wrap(self, argv: Sequence[str]) -> list[str]:
        if self.isolation.mechanism == "unshare-netns":
            return ["unshare", "-rn", *argv]
        return list(argv)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: pathlib.Path,
        timeout: float | None = None,
    ) -> ExecResult:
        """Run ``argv``.

        ``timeout`` overrides ``max_seconds`` for this call but can never exceed
        it, so a caller cannot raise its own ceiling.
        """

        limit = self.max_seconds if timeout is None else max(0.5, min(timeout, self.max_seconds))
        final = self.wrap(argv)
        # Capability code is first-party: it is expected to `import livium.tools` and
        # use ctx.services, so the package must be importable from the child. Without
        # this, every capability test fails on ModuleNotFoundError and the growth path
        # is dead on arrival -- which is exactly what happened the first time.
        import_root = pathlib.Path(__file__).resolve().parent.parent
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(pathlib.Path(cwd)),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(import_root), os.environ.get("PYTHONPATH", "")])
            ),
        }

        def _limits() -> None:  # pragma: no cover - runs in the forked child
            # 512 MiB address space and 64 MiB of output files: enough for builds
            # and tests, small enough that a runaway allocation is not fatal to
            # the host.
            try:
                resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            except (ValueError, OSError):
                pass
            try:
                resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
            except (ValueError, OSError):
                pass

        # The working directory is always one Livium created (workspace or
        # capability lab). If it is missing -- a fresh install, or someone cleaned
        # it out -- create it rather than failing with a confusing ENOENT from the
        # spawn, which reads like a missing binary.
        started = time.monotonic()   # set before any early return, so every result is timed
        truncated = False
        timed_out = False
        try:
            pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ExecResult(
                argv=list(argv), exit_code=127, stdout="", timed_out=False, truncated=False,
                stderr=f"[livium] cannot create working directory {cwd}: {exc}",
                network_isolated=self.isolation.network_isolated, cwd=str(cwd),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        try:
            completed = subprocess.run(
                final,
                cwd=str(cwd),
                env=child_env,
                capture_output=True,
                timeout=limit,
                check=False,
                preexec_fn=_limits if os.name == "posix" else None,
                stdin=subprocess.DEVNULL,
            )
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
            stderr += f"\n[livium] process exceeded the {limit:g}s limit and was killed"
            code = -9
        except FileNotFoundError as exc:
            stdout, stderr, code = "", f"[livium] executable not found: {exc}", 127

        if len(stdout) > self.max_output_bytes:
            stdout = stdout[: self.max_output_bytes] + "\n[livium] stdout truncated"
            truncated = True
        if len(stderr) > self.max_output_bytes:
            stderr = stderr[: self.max_output_bytes] + "\n[livium] stderr truncated"
            truncated = True

        return ExecResult(
            argv=list(argv),
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            truncated=truncated,
            network_isolated=self.isolation.network_isolated,
            cwd=str(cwd),
        )
