"""Model clients: real HTTP to real providers, plus explicit capability detection.

Supported backends, all implemented over the standard library:

* ``openai_compatible`` -- OpenAI, NVIDIA NIM, OpenRouter, Groq, Together,
  DeepSeek, vLLM, LM Studio, and Ollama's ``/v1`` endpoint. One wire format; one client.
* ``anthropic`` -- the Messages API.
* ``ollama`` -- the native ``/api/chat`` endpoint, for fully local use.
* ``scripted`` -- a *test double*, never a provider. Only constructible when
  ``LIVIUM_ALLOW_TEST_DOUBLE=1``; every reply it produces is tagged
  ``is_test_double=True`` so downstream code and reports cannot present it as a
  real model result.

The layer never guesses whether a provider works. :meth:`ModelClient.probe`
actually calls it, and :meth:`ModelClient.validate` refuses to run against a
model the provider does not list. If a provider is unreachable, the failure is
returned as a real error -- there is no fallback that fabricates a completion.
"""

from __future__ import annotations

import dataclasses
import email.utils
import json
import math
import os
import pathlib
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

from . import __version__
from .config import Config

class ModelError(RuntimeError):
    """A real, reportable failure from the model layer."""


class NvidiaRequestPacer:
    """Persistently space NVIDIA hosted API requests across Livium processes.

    The operator-provided hosted endpoint limit is 40 requests per minute. A 1.6 s
    minimum start interval leaves headroom below that ceiling. Reservations share a
    small SQLite file under LIVIUM_HOME, so separate Livium processes using that home
    do not each assume they own the full quota.
    """

    INTERVAL_SECONDS = 1.6
    DATABASE_NAME = "nvidia-request-pacing.sqlite3"

    def __init__(self, home: pathlib.Path):
        self.path = home / self.DATABASE_NAME

    def wait_for_slot(self) -> float:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ModelError(
                f"could not create NVIDIA request-pacing storage {self.path.parent}: {exc}"
            ) from exc
        now = time.time()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30.0)
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS request_gate "
                "(gate TEXT PRIMARY KEY, next_at REAL NOT NULL)"
            )
            row = connection.execute(
                "SELECT next_at FROM request_gate WHERE gate='nvidia-hosted'"
            ).fetchone()
            slot = max(now, float(row[0]) if row else now)
            connection.execute(
                "INSERT INTO request_gate(gate, next_at) VALUES('nvidia-hosted', ?) "
                "ON CONFLICT(gate) DO UPDATE SET next_at=excluded.next_at",
                (slot + self.INTERVAL_SECONDS,),
            )
            connection.commit()
        except sqlite3.Error as exc:
            # Fail closed: sending without a reservation could exceed the configured
            # provider limit when another Livium process is also making requests.
            raise ModelError(
                f"could not reserve an NVIDIA API request slot in {self.path}: {exc}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

        delay = max(0.0, slot - now)
        if delay:
            time.sleep(delay)
        return delay


class ProviderUnavailable(ModelError):
    """The provider could not be reached at all (network, DNS, TLS, refused)."""


class CredentialMissing(ModelError):
    """The provider needs a credential that is not configured."""


class ProviderRejected(ModelError):
    """The provider answered, but refused the request (status >= 400)."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclasses.dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclasses.dataclass
class ModelReply:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: dict[str, Any]
    latency_ms: int
    provider: str
    model: str
    is_test_double: bool = False
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ],
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
            "model": self.model,
            "is_test_double": self.is_test_double,
        }


@dataclasses.dataclass
class ProbeResult:
    ok: bool
    detail: str
    models: list[str] = dataclasses.field(default_factory=list)
    model_present: bool | None = None


def estimate_tokens(text: str) -> int:
    """Conservative token estimate.

    English prose in modern BPE tokenisers lands near 4 characters per token;
    code and JSON are denser. We divide by 3.5 and round up so the estimate errs
    high, and budgeting preserves headroom accordingly. When a provider reports
    real usage we record it and use it to correct the estimate (see
    ``LiviumAgent._record_usage_calibration``).
    """

    if not text:
        return 0
    return max(1, int(len(text) / 3.5) + 1)


def detect_provider(base_url: str) -> str:
    """Infer the provider from a base URL. Used only when the operator has not
    set LIVIUM_PROVIDER explicitly; the result is always echoed in reports."""

    host = base_url.lower()
    if "api.groq.com" in host:
        return "groq"
    if "anthropic" in host:
        return "anthropic"
    if ":11434" in host and "/v1" not in host:
        return "ollama"
    return "openai_compatible"


class Conversation:
    """Canonical message list, rendered per provider.

    One internal representation, two real renderings. Keeping this small means
    adding a provider does not require a second conversation model.
    """

    def __init__(self, system: str = "", messages: list[dict[str, Any]] | None = None):
        self.system = system
        self.messages: list[dict[str, Any]] = messages or []

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, text: str, tool_calls: Sequence[ToolCall] = ()) -> None:
        self.messages.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in tool_calls
                ],
            }
        )

    def add_tool_result(self, call: ToolCall, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}
        )

    def for_openai(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.system:
            out.append({"role": "system", "content": self.system})
        for msg in self.messages:
            role = msg["role"]
            if role == "assistant" and msg.get("tool_calls"):
                out.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content") or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c["arguments"], default=str),
                                },
                            }
                            for c in msg["tool_calls"]
                        ],
                    }
                )
            elif role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg["tool_call_id"],
                        "content": msg.get("content", ""),
                    }
                )
            else:
                out.append({"role": role, "content": msg.get("content", "")})
        return out

    def for_anthropic(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in self.messages:
            role = msg["role"]
            if role == "assistant" and msg.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for call in msg["tool_calls"]:
                    blocks.append(
                        {"type": "tool_use", "id": call["id"], "name": call["name"],
                         "input": call["arguments"]}
                    )
                out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg["tool_call_id"],
                                "content": msg.get("content", ""),
                            }
                        ],
                    }
                )
            else:
                out.append({"role": role, "content": msg.get("content", "")})
        return out

    def for_ollama(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in self.messages:
            role = msg["role"]
            if role == "assistant" and msg.get("tool_calls"):
                out.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content", ""),
                        "tool_calls": [
                            {"function": {"name": c["name"], "arguments": c["arguments"]}}
                            for c in msg["tool_calls"]
                        ],
                    }
                )
            elif role == "tool":
                out.append({"role": "tool", "content": msg.get("content", "")})
            else:
                out.append({"role": role, "content": msg.get("content", "")})
        return out


class ModelClient:
    """Base class. Subclasses implement ``complete`` and ``probe``."""

    provider = "base"
    name = "base"

    def __init__(self, config: Config):
        self.config = config
        self.model = config.model.model
        self.base_url = config.model.base_url
        self.timeout = config.model.timeout
        self.is_test_double = False

    # -- interface --------------------------------------------------------
    def complete(
        self,
        conversation: Conversation,
        tools: Sequence[dict[str, Any]],
        *,
        max_output_tokens: int | None = None,
    ) -> ModelReply:
        raise NotImplementedError

    def probe(self) -> ProbeResult:
        raise NotImplementedError

    # -- shared logic -----------------------------------------------------
    def validate(self) -> ProbeResult:
        """Real detection + validation. Raises rather than assuming."""

        result = self.probe()
        cfg = self.config.model
        if result.models and cfg.model:
            base = cfg.model.split(":", 1)[0]
            if cfg.model not in result.models and base not in result.models:
                similar = [m for m in result.models if base.lower() in m.lower()][:5]
                offered = (similar or result.models)[:8]
                raise ModelError(
                    f"model {cfg.model!r} is not offered by {self.name} at {self.base_url}. "
                    f"{len(result.models)} model(s) available, including: {', '.join(offered)}. "
                    f"Set LIVIUM_MODEL to one of them (optionally set "
                    f"LIVIUM_CONTEXT_WINDOW for its window if it is not in Livium's table)."
                )
        return result

    def context_window(self) -> tuple[int, str]:
        """Return (tokens, how_we_know). Never pretends to know exactly."""

        cfg = self.config.model
        if cfg.context_window:
            return cfg.context_window, "LIVIUM_CONTEXT_WINDOW (operator-declared)"
        known = self._known_context_window(cfg.model)
        if known:
            return known, (
                "built-in table for a known model (the provider's /models endpoint does "
                "not report context length)"
            )
        return cfg.fallback_context_window, (
            "conservative fallback: provider did not report a window and the model "
            "is not in the built-in table"
        )

    @staticmethod
    def _known_context_window(model: str) -> int | None:
        """A small factual table for models where providers do not expose the
        window over the API. Deliberately short: a guess labelled as knowledge is
        worse than an honest fallback with headroom."""

        m = model.lower()
        table = (
            ("claude-3-5", 200_000), ("claude-3-7", 200_000), ("claude-sonnet-4", 200_000),
            ("claude-opus-4", 200_000), ("claude-haiku-4", 200_000),
            ("gpt-4o", 128_000), ("gpt-4.1", 128_000), ("gpt-4-turbo", 128_000),
            ("gpt-3.5", 16_385), ("o1", 200_000), ("o3", 200_000), ("o4-mini", 200_000),
            ("llama3", 8_192), ("llama-3.1", 128_000), ("qwen2.5", 32_768),
            ("qwen3", 40_960), ("mistral", 32_768), ("gemma3", 128_000),
            ("deepseek", 64_000), ("phi4", 16_384),
        )
        for prefix, window in table:
            if m.startswith(prefix) or prefix in m:
                return window
        return None

    # -- HTTP -------------------------------------------------------------
    def _request(
        self,
        url: str,
        body: dict[str, Any] | None,
        headers: dict[str, str],
        *,
        method: str = "POST",
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        # Identify this as a server-side Livium client rather than urllib's generic
        # default. Some API front doors apply signature rules to anonymous/default
        # HTTP clients; do not impersonate a browser or another provider SDK.
        request_headers = {
            **headers,
            "Accept": "application/json",
            "User-Agent": f"Livium/{__version__}",
        }
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            if (urllib.parse.urlsplit(url).hostname or "").lower() == "integrate.api.nvidia.com":
                NvidiaRequestPacer(self.config.paths.home).wait_for_slot()
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            retry_after = _retry_after_seconds(
                exc.headers.get("Retry-After") if exc.headers else None, detail
            ) if exc.code == 429 else None
            if exc.code == 403 and "1010" in detail:
                raise ProviderRejected(
                    f"{self.name} was denied by Cloudflare with error 1010 at {url}. "
                    "This is an access/signature block before model inference, not a "
                    "model-ID or API-key rejection. Livium sent its identified "
                    "server-side User-Agent; retry from a different trusted network "
                    "or ask Groq support to review the block.",
                    status_code=exc.code,
                ) from exc
            raise ProviderRejected(
                f"{self.name} returned HTTP {exc.code} for {url}: {detail}",
                status_code=exc.code, retry_after=retry_after,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailable(
                f"{self.name} is unreachable at {self.base_url}: {exc.reason}. "
                f"Check network egress and LIVIUM_BASE_URL."
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise ProviderUnavailable(
                f"{self.name} network error at {self.base_url}: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ModelError(
                f"{self.name} returned non-JSON ({len(payload)} bytes): {payload[:300]!r}"
            ) from exc


class OpenAICompatibleClient(ModelClient):
    provider = "openai_compatible"

    def __init__(self, config: Config):
        super().__init__(config)
        self.name = f"openai-compatible@{config.model.base_url}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.model.api_key:
            headers["Authorization"] = f"Bearer {self.config.model.api_key}"
        return headers

    def complete(self, conversation, tools, *, max_output_tokens=None) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": conversation.for_openai(),
            "temperature": self.config.model.temperature,
            "max_tokens": max_output_tokens or self.config.model.max_output_tokens,
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}}
                for t in tools
            ]
            body["tool_choice"] = "auto"
        if (urllib.parse.urlsplit(self.base_url).hostname or "").lower() == "integrate.api.nvidia.com":
            try:
                reasoning_budget = int(os.environ.get("LIVIUM_NVIDIA_REASONING_BUDGET", "4096"))
            except ValueError as exc:
                raise ModelError("LIVIUM_NVIDIA_REASONING_BUDGET must be an integer") from exc
            if not 128 <= reasoning_budget <= 32768:
                raise ModelError("LIVIUM_NVIDIA_REASONING_BUDGET must be between 128 and 32768")
            # NVIDIA's Nemotron model card recommends non-empty assistant content for
            # coding-agent integrations. Its tool-calling examples also explicitly
            # enable the reasoning template used to produce tool calls. Bound the
            # default reasoning budget so the hosted model does not spend its full
            # default budget thinking before returning an agent action.
            body["chat_template_kwargs"] = {
                "enable_thinking": True,
                "force_nonempty_content": True,
                "reasoning_budget": reasoning_budget,
            }

        started = time.monotonic()
        payload = self._request(f"{self.base_url}/chat/completions", body, self._headers())
        latency = int((time.monotonic() - started) * 1000)
        return self._parse(payload, latency)

    def _parse(self, payload: dict[str, Any], latency_ms: int) -> ModelReply:
        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(
                f"{self.name} reply had no choices: {json.dumps(payload)[:500]}"
            ) from exc
        message = choice.get("message", {}) or {}
        calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function", {}) or {}
            raw_args = function.get("arguments") or "{}"
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=_parse_arguments(raw_args),
                    raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args),
                )
            )
        return ModelReply(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            stop_reason=str(choice.get("finish_reason") or ""),
            usage=normalize_token_usage(payload.get("usage")),
            latency_ms=latency_ms,
            provider=self.provider,
            model=str(payload.get("model") or self.model),
            raw=payload,
        )

    def probe(self) -> ProbeResult:
        try:
            payload = self._request(
                f"{self.base_url}/models", None, self._headers(), method="GET"
            )
        except (ProviderUnavailable, ProviderRejected) as exc:
            return ProbeResult(ok=False, detail=str(exc))
        models = [
            str(item.get("id"))
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
        present = self.model in models if models else None
        return ProbeResult(
            ok=True,
            detail=f"reached {self.base_url}; provider lists {len(models)} model(s)",
            models=models,
            model_present=present,
        )


class GroqClient(OpenAICompatibleClient):
    """Groq's OpenAI-compatible API with Groq-specific defaults and identity."""

    provider = "groq"

    def __init__(self, config: Config):
        super().__init__(config)
        self.name = f"groq@{config.model.base_url}"

    def _headers(self) -> dict[str, str]:
        if not self.config.model.api_key:
            raise CredentialMissing(
                "the Groq API requires a key: set LIVIUM_API_KEY or GROQ_API_KEY."
            )
        return super()._headers()


class AnthropicClient(ModelClient):
    provider = "anthropic"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, config: Config):
        super().__init__(config)
        self.name = "anthropic"

    def _headers(self) -> dict[str, str]:
        if not self.config.model.api_key:
            raise CredentialMissing(
                "the Anthropic API requires a key: set LIVIUM_API_KEY or ANTHROPIC_API_KEY."
            )
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.model.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
        }

    def complete(self, conversation, tools, *, max_output_tokens=None) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": conversation.for_anthropic(),
            "max_tokens": max_output_tokens or self.config.model.max_output_tokens,
            "temperature": self.config.model.temperature,
        }
        if conversation.system:
            body["system"] = conversation.system
        if tools:
            body["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in tools
            ]

        started = time.monotonic()
        payload = self._request(f"{self.base_url}/messages", body, self._headers())
        latency = int((time.monotonic() - started) * 1000)

        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in payload.get("content", []) or []:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(block.get("id") or f"toolu_{len(calls)}"),
                        name=str(block.get("name") or ""),
                        arguments=block.get("input") or {},
                        raw_arguments=json.dumps(block.get("input") or {}),
                    )
                )
        usage = payload.get("usage") or {}
        return ModelReply(
            text="\n".join(t.strip() for t in texts if t).strip(),
            tool_calls=calls,
            stop_reason=str(payload.get("stop_reason") or ""),
            usage={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
            latency_ms=latency,
            provider=self.provider,
            model=str(payload.get("model") or self.model),
            raw=payload,
        )

    def probe(self) -> ProbeResult:
        if not self.config.model.api_key:
            return ProbeResult(
                ok=False,
                detail=(
                    "no Anthropic credential configured: set LIVIUM_API_KEY or "
                    "ANTHROPIC_API_KEY in the environment"
                ),
            )
        try:
            payload = self._request(f"{self.base_url}/models", None, self._headers(), method="GET")
        except (ProviderUnavailable, ProviderRejected) as exc:
            return ProbeResult(ok=False, detail=str(exc))
        models = [
            str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict)
        ]
        return ProbeResult(
            ok=True,
            detail=f"reached {self.base_url}; provider lists {len(models)} model(s)",
            models=models,
            model_present=(self.model in models) if models else None,
        )


class OllamaClient(ModelClient):
    provider = "ollama"

    def __init__(self, config: Config):
        super().__init__(config)
        self.name = f"ollama@{config.model.base_url}"
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]

    def complete(self, conversation, tools, *, max_output_tokens=None) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": (
                [{"role": "system", "content": conversation.system}] if conversation.system else []
            ) + conversation.for_ollama(),
            "stream": False,
            "options": {
                "temperature": self.config.model.temperature,
                "num_predict": max_output_tokens or self.config.model.max_output_tokens,
            },
        }
        if tools:
            body["tools"] = [
                {"type": "function",
                 "function": {k: t[k] for k in ("name", "description", "parameters")}}
                for t in tools
            ]
        started = time.monotonic()
        payload = self._request(f"{self.base_url}/api/chat", body, {"Content-Type": "application/json"})
        latency = int((time.monotonic() - started) * 1000)
        message = payload.get("message", {}) or {}
        calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            function = raw.get("function", {}) or {}
            args = function.get("arguments")
            calls.append(
                ToolCall(
                    id=f"ollama_call_{index}",
                    name=str(function.get("name") or ""),
                    arguments=args if isinstance(args, dict) else _parse_arguments(args or "{}"),
                    raw_arguments=json.dumps(args) if not isinstance(args, str) else args,
                )
            )
        return ModelReply(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            stop_reason="tool_use" if calls else "stop",
            usage={
                "input_tokens": payload.get("prompt_eval_count"),
                "output_tokens": payload.get("eval_count"),
            },
            latency_ms=latency,
            provider=self.provider,
            model=str(payload.get("model") or self.model),
            raw=payload,
        )

    def probe(self) -> ProbeResult:
        try:
            payload = self._request(f"{self.base_url}/api/tags", None, {}, method="GET")
        except (ProviderUnavailable, ProviderRejected) as exc:
            return ProbeResult(ok=False, detail=str(exc))
        models = [str(m.get("name")) for m in payload.get("models", []) if isinstance(m, dict)]
        return ProbeResult(
            ok=True,
            detail=f"reached {self.base_url}; {len(models)} local model(s) installed",
            models=models,
            model_present=(self.model in models or f"{self.model}:latest" in models)
            if models
            else None,
        )


def _script_from_environment() -> list[dict[str, Any]]:
    """Replies for the test double, from ``LIVIUM_SCRIPT_FILE``.

    In-process tests pass their script directly. A *separate process* -- a demo, the
    Studio UI, a recovery test -- has no way to pass an object, so it names a JSON file
    instead. This is a test-only seam and it is gated exactly like the client itself:
    without ``LIVIUM_ALLOW_TEST_DOUBLE=1`` the client refuses to exist at all, and every
    reply it produces stays marked ``is_test_double``. A missing or malformed file is an
    error, not an empty script: a run that silently gets no replies would look like a
    model failure.
    """

    path = os.environ.get("LIVIUM_SCRIPT_FILE")
    if not path:
        return []
    file = pathlib.Path(path)
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModelError(f"LIVIUM_SCRIPT_FILE={path} cannot be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ModelError(f"LIVIUM_SCRIPT_FILE={path} is not JSON: {exc}") from exc
    if not isinstance(payload, list) or any(not isinstance(entry, dict) for entry in payload):
        raise ModelError(f"LIVIUM_SCRIPT_FILE={path} must contain a JSON array of reply "
                         f"objects, each with optional keys: text, tool_calls, stop_reason")
    return payload


class ScriptedClient(ModelClient):
    """A test double. Not a provider.

    It exists so the agent's *orchestration* -- loop control, tool dispatch,
    verification, recovery, persistence -- can be tested deterministically. It
    calls no network API and its replies are permanently marked
    ``is_test_double``. Every report that includes a reply from this client also
    states that no real model was involved.
    """

    provider = "scripted"

    def __init__(self, config: Config, script: Sequence[dict[str, Any]] | None = None):
        if not config.model.allow_test_double:
            raise CredentialMissing(
                "the scripted client is a test double and is disabled. Set "
                "LIVIUM_ALLOW_TEST_DOUBLE=1 only in automated tests, or configure a "
                "real provider (LIVIUM_PROVIDER=openai_compatible|groq|anthropic|ollama)."
            )
        super().__init__(config)
        self.name = "scripted-test-double"
        self.is_test_double = True
        self.script = list(script) if script else _script_from_environment()
        self.calls: list[dict[str, Any]] = []
        self._index = 0

    def queue(self, *replies: dict[str, Any]) -> None:
        self.script.extend(replies)

    def complete(self, conversation, tools, *, max_output_tokens=None) -> ModelReply:
        self.calls.append(
            {
                "messages": conversation.messages,
                "system": conversation.system,
                "tools": [t["name"] for t in tools],
            }
        )
        entry = self.script[self._index] if self._index < len(self.script) else {"text": "[script exhausted]"}
        self._index += 1
        calls = [
            ToolCall(id=c.get("id", f"sim_{i}"), name=c["name"], arguments=c.get("arguments", {}),
                     raw_arguments=json.dumps(c.get("arguments", {})))
            for i, c in enumerate(entry.get("tool_calls", []))
        ]
        return ModelReply(
            text=entry.get("text", ""),
            tool_calls=calls,
            stop_reason=entry.get("stop_reason", "tool_use" if calls else "stop"),
            usage={"input_tokens": None, "output_tokens": None},
            latency_ms=0,
            provider=self.provider,
            model=self.model,
            is_test_double=True,
            raw={"scripted": True},
        )

    def probe(self) -> ProbeResult:
        return ProbeResult(
            ok=True,
            detail=(
                "scripted test double is active. NO REAL MODEL IS BEING CALLED. This "
                "mode validates Livium's orchestration only, never model quality."
            ),
            models=[self.config.model.model],
            model_present=True,
        )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Tolerant but honest JSON argument parsing.

    Some providers emit slightly malformed or double-encoded tool arguments. We
    accept a dict, a JSON object string, or a JSON-encoded object string; anything
    else becomes ``{"_raw": ...}`` so the tool layer reports an argument error
    rather than silently running with wrong inputs.
    """

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {"_raw": raw}
    text = raw.strip() or "{}"
    for candidate in (text, text[1:-1] if text.startswith('"') and text.endswith('"') else text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                inner = json.loads(value)
                if isinstance(inner, dict):
                    return inner
            except json.JSONDecodeError:
                pass
        return {"_raw": value}
    return {"_raw": text}


def normalize_token_usage(raw: Any) -> dict[str, int]:
    """Return only the flat numeric token counts the agent consumes.

    Providers often attach nested detail objects to ``usage``.
    A provider or proxy can also return an unexpected object in one of the top-level
    count fields. Passing those values through could make accounting attempt numeric
    conversion on metadata. Preserve the complete provider response in
    ``ModelReply.raw``; keep this accounting view small and safe.
    """

    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, int] = {}
    for target, aliases in (
        ("prompt_tokens", ("prompt_tokens", "input_tokens")),
        ("completion_tokens", ("completion_tokens", "output_tokens")),
    ):
        for key in aliases:
            value = raw.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    continue
                normalized[target] = max(0, int(value))
                break
            if isinstance(value, str):
                try:
                    normalized[target] = max(0, int(value))
                    break
                except ValueError:
                    continue
    return normalized


def _retry_after_seconds(header: str | None, detail: str) -> float | None:
    """Read a bounded-retry hint from a standard header or provider error text."""

    if header:
        try:
            value = float(header.strip())
            if math.isfinite(value) and value >= 0:
                return value
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(header)
                value = parsed.timestamp() - time.time()
                if math.isfinite(value):
                    return max(0.0, value)
            except (TypeError, ValueError, OverflowError):
                pass

    try:
        payload = json.loads(detail)
        message = payload.get("error", {}).get("message", "")
    except (TypeError, ValueError, AttributeError):
        message = detail
    if not isinstance(message, str):
        return None
    match = re.search(
        r"(?:try again in|retry after)\s+([0-9]+(?:\.[0-9]+)?)\s*(?:seconds?|secs?|s)\b",
        message, re.IGNORECASE,
    )
    if not match:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) else None


def build_client(config: Config, script: Sequence[dict[str, Any]] | None = None) -> ModelClient:
    """Construct the configured client. Raises on unusable configuration."""

    provider = config.model.provider
    if provider == "openai_compatible":
        return OpenAICompatibleClient(config)
    if provider == "groq":
        return GroqClient(config)
    if provider == "anthropic":
        return AnthropicClient(config)
    if provider == "ollama":
        return OllamaClient(config)
    if provider == "scripted":
        return ScriptedClient(config, script)
    raise ModelError(f"unknown provider {provider!r}")
