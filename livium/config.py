"""Environment-driven configuration.

Everything the agent can reach is declared here so that the permission engine
(`livium.permissions`) has one authoritative source of truth. Nothing in this
module talks to the network or the filesystem beyond resolving paths.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shlex
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Providers Livium knows how to speak to. "openai_compatible" deliberately
# covers OpenAI, OpenRouter, Groq, Together, vLLM, LM Studio and Ollama's
# OpenAI-compatible endpoint -- they share one wire format, so one client is
# real support for all of them rather than five half-built integrations.
PROVIDERS = ("openai_compatible", "anthropic", "ollama", "scripted")

DEFAULT_BINARIES = (
    "python3",
    "python",
    "git",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "diff",
    "sort",
    "uniq",
    "sha256sum",
    "sed",
    "tr",
    "date",
    "echo",
    "true",
    "false",
)


class ConfigError(RuntimeError):
    """Raised for unusable configuration. Never silently defaulted around."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - guarded by tests
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _resolve_under_home(raw: str, home: pathlib.Path) -> pathlib.Path:
    """Resolve a configured path: absolute as given, relative to the data home."""

    path = pathlib.Path(raw).expanduser()
    return (path if path.is_absolute() else home / path).resolve()


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if raw is None:
        return default
    return tuple(part for part in (p.strip() for p in raw.split(",")) if part)


@dataclasses.dataclass(frozen=True)
class ModelConfig:
    """Which model to talk to, and how."""

    provider: str = "openai_compatible"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 120.0
    max_retries: int = 2
    # Context window in tokens. None means "discover it, else use the
    # conservative fallback". We never assume all providers are identical.
    context_window: int | None = None
    fallback_context_window: int = 8192
    max_output_tokens: int = 1024
    temperature: float = 0.0
    # Fraction of the window held back for reasoning + the model's reply.
    safety_margin: float = 0.25
    allow_test_double: bool = False


@dataclasses.dataclass(frozen=True)
class LimitConfig:
    """Stopping conditions for the operational loop (Part III, Stage 9)."""

    max_steps: int = 12
    max_tool_calls: int = 40
    #: How many times the router may retry the *same* tool after a failure that could
    #: plausibly differ on a second attempt. Retries of an identical call are never
    #: allowed -- see livium.router.
    max_retries_per_tool: int = 2
    max_seconds: float = 900.0
    max_command_seconds: float = 60.0
    max_tool_output_bytes: int = 30000
    max_file_write_bytes: int = 400_000
    #: Deferred discovery: what one follow-up cycle may spend after a task is done.
    #: Every one of these is checked before an investigation starts, and stopping
    #: leaves the queue resumable rather than half-done (see livium.discovery).
    followup_enabled: bool = True
    followup_max_items: int = 2
    followup_max_model_calls: int = 6
    followup_max_steps: int = 6
    followup_max_seconds: float = 120.0
    #: A discovery found *while investigating another* enters the queue at depth+1.
    #: Past this depth it is recorded but not investigated, which is what stops the
    #: chain from becoming unbounded recursive work.
    followup_max_depth: int = 2
    #: Token ceiling for a cycle. Reported as an estimate limit unless the provider
    #: reported usage, in which case the real counts are used and the report says so.
    followup_max_tokens: int = 20000


@dataclasses.dataclass(frozen=True)
class PathConfig:
    home: pathlib.Path
    db: pathlib.Path
    workspace: pathlib.Path
    evidence: pathlib.Path
    prompts: pathlib.Path
    # Where newly developed capabilities are written before promotion. The
    # agent can write here freely; promotion into `livium/promoted` is a
    # separate, human-approved step.
    capability_lab: pathlib.Path
    #: Where promotion copies a capability so the loader can import it. Configurable
    #: because an installed package may be read-only, and because a demonstration or
    #: test needs to promote without writing into the source tree.
    promoted: pathlib.Path


@dataclasses.dataclass(frozen=True)
class PermissionConfig:
    read_roots: tuple[pathlib.Path, ...]
    write_roots: tuple[pathlib.Path, ...]
    allowed_binaries: tuple[str, ...]
    denied_binaries: tuple[str, ...]
    network_allowlist: tuple[str, ...]
    protected_paths: tuple[pathlib.Path, ...]
    #: Paths the agent may write only with a human approval recorded in the database.
    approval_write_roots: tuple[pathlib.Path, ...] = ()
    #: Binaries that may run only with a human approval recorded in the database.
    approval_binaries: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Config:
    model: ModelConfig
    limits: LimitConfig
    paths: PathConfig
    perms: PermissionConfig
    log_level: str = "info"

    # -- construction -----------------------------------------------------
    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> "Config":
        if env is not None:
            # Test-friendly: build from an explicit mapping without mutating
            # the real process environment.
            original = dict(os.environ)
            try:
                os.environ.update(env)
                return cls._load()
            finally:
                os.environ.clear()
                os.environ.update(original)
        return cls._load()

    @classmethod
    def _load(cls) -> "Config":
        provider = (_env("LIVIUM_PROVIDER", "openai_compatible") or "").lower()
        if provider not in PROVIDERS:
            raise ConfigError(
                f"LIVIUM_PROVIDER must be one of {', '.join(PROVIDERS)}; got {provider!r}"
            )

        # Accept both a Livium-specific name and the conventional provider name
        # so operators are not forced to duplicate a key they already have.
        api_key = (
            _env("LIVIUM_API_KEY")
            or _env("OPENAI_API_KEY")
            or _env("ANTHROPIC_API_KEY")
            or ""
        )

        default_base = {
            "openai_compatible": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "ollama": "http://127.0.0.1:11434",
            "scripted": "",
        }[provider]
        base_url = _env("LIVIUM_BASE_URL", default_base) or default_base

        default_model = {
            "openai_compatible": "gpt-4o-mini",
            "anthropic": "claude-3-5-haiku-latest",
            "ollama": "llama3.1",
            "scripted": "scripted-double",
        }[provider]
        model_name = _env("LIVIUM_MODEL", default_model) or default_model

        window_raw = _env("LIVIUM_CONTEXT_WINDOW")
        model_cfg = ModelConfig(
            provider=provider,
            model=model_name,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout=float(_env_int("LIVIUM_MODEL_TIMEOUT", 120)),
            max_retries=_env_int("LIVIUM_MODEL_RETRIES", 2),
            context_window=int(window_raw) if window_raw else None,
            fallback_context_window=_env_int("LIVIUM_FALLBACK_CONTEXT_WINDOW", 8192),
            max_output_tokens=_env_int("LIVIUM_MAX_OUTPUT_TOKENS", 1024),
            temperature=float(_env_int("LIVIUM_TEMPERATURE_X100", 0)) / 100.0,
            safety_margin=float(_env_int("LIVIUM_SAFETY_MARGIN_PCT", 25)) / 100.0,
            allow_test_double=_env_bool("LIVIUM_ALLOW_TEST_DOUBLE", False),
        )

        limits = LimitConfig(
            max_steps=_env_int("LIVIUM_MAX_STEPS", 12),
            max_retries_per_tool=_env_int("LIVIUM_MAX_RETRIES_PER_TOOL", 2),
            followup_enabled=_env_bool("LIVIUM_FOLLOWUP_ENABLED", True),
            followup_max_items=_env_int("LIVIUM_FOLLOWUP_MAX_ITEMS", 2),
            followup_max_model_calls=_env_int("LIVIUM_FOLLOWUP_MAX_MODEL_CALLS", 6),
            followup_max_steps=_env_int("LIVIUM_FOLLOWUP_MAX_STEPS", 6),
            followup_max_seconds=float(_env("LIVIUM_FOLLOWUP_MAX_SECONDS", "120") or 120),
            followup_max_depth=_env_int("LIVIUM_FOLLOWUP_MAX_DEPTH", 2),
            followup_max_tokens=_env_int("LIVIUM_FOLLOWUP_MAX_TOKENS", 20000),
            max_tool_calls=_env_int("LIVIUM_MAX_TOOL_CALLS", 40),
            max_seconds=float(_env_int("LIVIUM_MAX_SECONDS", 900)),
            max_command_seconds=float(_env_int("LIVIUM_MAX_COMMAND_SECONDS", 60)),
            max_tool_output_bytes=_env_int("LIVIUM_MAX_TOOL_OUTPUT_BYTES", 30000),
            max_file_write_bytes=_env_int("LIVIUM_MAX_FILE_WRITE_BYTES", 400_000),
        )

        home = pathlib.Path(_env("LIVIUM_HOME", str(REPO_ROOT / ".livium")) or "").expanduser()
        workspace = pathlib.Path(
            _env("LIVIUM_WORKSPACE", str(home / "workspace")) or ""
        ).expanduser()
        paths = PathConfig(
            home=home.resolve(),
            db=pathlib.Path(
                _env("LIVIUM_DB", str(home / "livium.db")) or ""
            ).expanduser().resolve(),
            workspace=workspace.resolve(),
            evidence=pathlib.Path(
                _env("LIVIUM_EVIDENCE", str(home / "evidence")) or ""
            ).expanduser().resolve(),
            prompts=(REPO_ROOT / "livium" / "prompts").resolve(),
            capability_lab=(home / "capability-lab").resolve(),
            promoted=_resolve_under_home(
                _env("LIVIUM_PROMOTED_DIR", str(home / "promoted")) or "",
                home),
        )

        # Protected paths: the governance and safety surface. The permission
        # engine refuses to let the *agent* write these; see permissions.py for
        # what that guarantee does and does not cover.
        # The enforcement surface: the modules that decide what the agent may do,
        # plus the instructions it operates under. The agent's tools cannot write
        # them, and the guardian hashes them, so an edit is detected rather than
        # trusted. See docs/LIMITATIONS.md section 3 for what this does not cover.
        # The write refusal covers the components whose unauthorised change could
        # weaken governance or enforcement. The policy that explains why each one is
        # here -- what may change, who authorises it, how the change is verified --
        # lives in livium/integrity.py, next to the mechanism that uses it. Ordinary
        # feature code is reviewed through git like any other work, and is not
        # write-refused: protecting everything equally protects nothing in particular.
        protected = (
            paths.prompts,
            (REPO_ROOT / "AGENTS.md").resolve(),
            (REPO_ROOT / "livium" / "permissions.py").resolve(),
            (REPO_ROOT / "livium" / "guardian.py").resolve(),
            (REPO_ROOT / "livium" / "integrity.py").resolve(),
            (REPO_ROOT / "livium" / "approvals.py").resolve(),
            (REPO_ROOT / "livium" / "config.py").resolve(),
            (REPO_ROOT / "livium" / "execution.py").resolve(),
            (REPO_ROOT / "livium" / "tools.py").resolve(),
            (REPO_ROOT / "livium" / "verification.py").resolve(),
        )

        extra_write = tuple(
            pathlib.Path(p).expanduser().resolve()
            for p in _env_list("LIVIUM_EXTRA_WRITE_ROOTS", ())
        )
        #: Files this deployment refuses to let the agent write, on top of the
        #: enforcement surface above. A component protected only this way is still
        #: watched and still refused; what it lacks is a written rationale, and
        #: livium.integrity says so in the record instead of borrowing one.
        extra_protected = tuple(
            pathlib.Path(p).expanduser().resolve()
            for p in _env_list("LIVIUM_EXTRA_PROTECTED_PATHS", ())
        )

        perms = PermissionConfig(
            read_roots=(paths.workspace, paths.capability_lab, REPO_ROOT),
            write_roots=(paths.workspace, paths.capability_lab) + extra_write,
            allowed_binaries=_env_list("LIVIUM_ALLOWED_BINARIES", DEFAULT_BINARIES),
            denied_binaries=_env_list(
                "LIVIUM_DENIED_BINARIES",
                (
                    "rm", "rmdir", "mv", "dd", "mkfs", "shutdown", "reboot",
                    "sudo", "su", "chown", "chmod", "chgrp", "curl", "wget",
                    "ssh", "scp", "nc", "ncat", "netcat", "telnet", "bash",
                    "sh", "zsh", "eval", "exec", "pip", "pip3", "npm", "apt",
                    "apt-get", "systemctl", "kill", "pkill", "crontab", "at",
                    # Cross-platform: the same gap exists on Windows, where these are
                    # the interpreters that turn text into syntax.
                    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh",
                    "command.com", "wsl", "reg", "regedit", "schtasks", "sc",
                    "diskpart", "format", "del", "rd", "takeown", "icacls",
                ),
            ),
            network_allowlist=_env_list("LIVIUM_NETWORK_ALLOWLIST", ()),
            protected_paths=protected + extra_protected,
            # Consequential-but-legitimate operations. Empty by default: nothing is
            # approvable until an operator says what is. An approval is a row a human
            # writes; the model cannot create one (see livium.approvals).
            approval_write_roots=tuple(
                pathlib.Path(p).expanduser().resolve()
                for p in _env_list("LIVIUM_APPROVAL_WRITE_ROOTS", ())
            ),
            approval_binaries=_env_list("LIVIUM_APPROVAL_BINARIES", ()),
        )

        return cls(
            model=model_cfg,
            limits=limits,
            paths=paths,
            perms=perms,
            log_level=(_env("LIVIUM_LOG_LEVEL", "info") or "info").lower(),
        )

    # -- helpers ----------------------------------------------------------
    def model_label(self) -> str:
        return f"{self.model.provider}:{self.model.model}"

    def ensure_dirs(self) -> None:
        for path in (
            self.paths.home,
            self.paths.workspace,
            self.paths.evidence,
            self.paths.capability_lab,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def credential_present(self) -> bool:
        if self.model.provider in ("ollama", "scripted"):
            return True
        return bool(self.model.api_key)

    def describe(self) -> dict[str, Any]:
        """Machine-readable summary for reports and the viewer UI."""

        return {
            "model": {
                "provider": self.model.provider,
                "model": self.model.model,
                "base_url": self.model.base_url,
                "api_key_present": bool(self.model.api_key),
                "context_window": self.model.context_window,
                "fallback_context_window": self.model.fallback_context_window,
                "max_output_tokens": self.model.max_output_tokens,
                "safety_margin": self.model.safety_margin,
            },
            "limits": dataclasses.asdict(self.limits),
            "paths": {
                "home": str(self.paths.home),
                "db": str(self.paths.db),
                "workspace": str(self.paths.workspace),
                "evidence": str(self.paths.evidence),
                "capability_lab": str(self.paths.capability_lab),
                "promoted": str(self.paths.promoted),
            },
            "permissions": {
                "read_roots": [str(p) for p in self.perms.read_roots],
                "write_roots": [str(p) for p in self.perms.write_roots],
                "allowed_binaries": list(self.perms.allowed_binaries),
                "denied_binaries": list(self.perms.denied_binaries),
                "network_allowlist": list(self.perms.network_allowlist),
                "protected_paths": [str(p) for p in self.perms.protected_paths],
            },
        }

    @staticmethod
    def quote_command(argv: list[str]) -> str:
        return " ".join(shlex.quote(a) for a in argv)
