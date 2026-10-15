"""The tools the model can actually call.

Every entry in the registry executes real code against the real filesystem or a
real subprocess. A tool that is listed but cannot do its job would be a direct
violation of the project's rules, so the registry is short and each member is
covered by tests that assert its observable effect (file contents on disk, exit
codes, rows in the database).

Flow for every call, without exception::

    model emits tool call
      -> permission engine decides (path / command / protected / network)
      -> DENY or APPROVAL: recorded as a boundary event, returned to the model as
         a denial it must not route around
      -> ALLOW: intent row written (write-ahead), execute, resolve intent with the
         observed result, append a journal event

The model's own claim about safety is never consulted.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import tempfile
from typing import Any, Callable

from .config import Config
from .execution import Runner
from .memory import Memory, ProtectedMemoryError
from .permissions import APPROVAL, DENY, PermissionEngine
from .store import Store

MAX_LISTING = 400


@dataclasses.dataclass
class ToolResult:
    tool: str
    ok: bool
    output: str
    data: dict[str, Any] = dataclasses.field(default_factory=dict)
    denied: bool = False
    error: str | None = None
    category: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool, "ok": self.ok, "output": self.output,
            "data": self.data, "denied": self.denied, "error": self.error,
            "category": self.category,
        }


@dataclasses.dataclass
class ToolContext:
    """Everything a tool needs to observe or change real state.

    ``services`` carries the live objects (memory, store, config, guardian) so a
    promoted capability can do real work against Livium's own state instead of
    being a tool that can only talk. It is passed by the harness, never chosen by
    the model.
    """

    task_id: str | None = None
    step: int = 0
    purpose: str = ""
    services: dict[str, Any] = dataclasses.field(default_factory=dict)


def _text(value: str, limit: int = 12_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n[... truncated, {len(value) - limit} more characters]"


class ToolRegistry:
    def __init__(
        self,
        config: Config,
        engine: PermissionEngine,
        store: Store,
        memory: Memory,
        runner: Runner,
    ):
        self.config = config
        self.engine = engine
        self.store = store
        self.memory = memory
        self.runner = runner
        self._handlers: dict[str, Callable[[dict[str, Any], ToolContext], ToolResult]] = {
            "read_file": self._read_file,
            "list_dir": self._list_dir,
            "sha256": self._sha256,
            "write_file": self._write_file,
            "run_command": self._run_command,
            "memory_search": self._memory_search,
            "memory_write": self._memory_write,
            "memory_update": self._memory_update,
            "memory_forget": self._memory_forget,
            "request_capability": self._request_capability,
            "finish_task": self._finish_task,
        }
        self._external_specs: dict[str, dict[str, Any]] = {}
        self._external_handlers: dict[str, Callable] = {}

    # -- schema -----------------------------------------------------------
    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file from disk and return its contents. Use this to "
                    "check real state rather than assuming it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path, absolute or relative to the workspace."},
                        "max_bytes": {"type": "integer", "description": "Optional cap; default 80000."},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": "List the entries of a directory with size and type.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "sha256",
                "description": "Compute the SHA-256 of a file. Useful for proving content did not change.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": (
                    "Write a UTF-8 text file atomically inside your workspace or capability "
                    "lab, then read it back to confirm. Protected governance files cannot be "
                    "written by this tool."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "purpose": {"type": "string", "description": "One line: why this write is needed."},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "run_command",
                "description": (
                    "Run an allowlisted program with an explicit argument list. There is no "
                    "shell: pipelines, redirection and globbing are unavailable. Working "
                    "directory is your workspace. Denied binaries and dangerous git "
                    "subcommands are refused by the harness."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Program and arguments, e.g. [\"python3\",\"-m\",\"unittest\",\"-v\"].",
                        },
                        "purpose": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "required": ["argv"],
                },
            },
            {
                "name": "memory_search",
                "description": (
                    "Search persistent memory (ranked full-text). Returns records with their "
                    "confidence, verification state and provenance. Memory is evidence, not "
                    "truth: re-check source evidence when it materially matters."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "kinds": {
                            "type": "array",
                            "items": {"type": "string",
                                      "enum": ["foundational", "capability", "experience",
                                               "decision", "failure", "task"]},
                        },
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memory_write",
                "description": (
                    "Persist something worth keeping across sessions. Record how you know it: "
                    "verification is one of unverified|claimed|executed|tested|verified|refuted, "
                    "and evidence should point at the artefact that supports it. Supplying a "
                    "key supersedes the previous active record with that key."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string",
                                 "enum": ["capability", "experience", "decision", "failure", "task"]},
                        "content": {"type": "string"},
                        "key": {"type": "string", "description": "Stable identifier for updates."},
                        "detail": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number", "description": "0.0-1.0"},
                        "verification": {"type": "string"},
                        "evidence": {"type": "string"},
                        "scope": {"type": "string"},
                    },
                    "required": ["kind", "content"],
                },
            },
            {
                "name": "memory_update",
                "description": "Correct or re-grade an existing memory record by id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "content": {"type": "string"},
                        "confidence": {"type": "number"},
                        "verification": {"type": "string"},
                        "evidence": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "memory_forget",
                "description": (
                    "Retire a memory record that is wrong, obsolete or no longer justified. "
                    "History is preserved; protected records cannot be retired."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                },
            },
            {
                "name": "request_capability",
                "description": (
                    "Record a capability *proposal*. This creates nothing and grants nothing: "
                    "it writes a Proposed record with your reuse analysis. Use it only after "
                    "working through reuse-before-build in the Growth Guardian."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "requirement": {"type": "string",
                                        "description": "The observable outcome that is currently impossible."},
                        "reuse_analysis": {"type": "string",
                                           "description": "What existing capabilities you checked, and why they are insufficient."},
                        "rationale": {"type": "string"},
                        "verification_plan": {"type": "string"},
                        "reversible": {"type": "boolean"},
                    },
                    "required": ["name", "requirement", "reuse_analysis"],
                },
            },
            {
                "name": "finish_task",
                "description": (
                    "End the task. The harness then verifies the acceptance criteria for real. "
                    "State what you actually did and what you did not verify."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "status": {"type": "string", "enum": ["complete", "partial", "blocked", "failed"]},
                        "unverified": {"type": "string",
                                       "description": "Anything you could not confirm."},
                    },
                    "required": ["summary", "status"],
                },
            },
        ] + list(self._external_specs.values())

    def specs_names(self) -> tuple[str, ...]:
        return tuple(spec["name"] for spec in self.specs())

    def register_external(self, spec: dict[str, Any], handler) -> None:
        """Add a tool supplied by a promoted capability.

        The spec is validated before registration: a tool whose schema the model
        cannot act on, or whose name collides with a built-in, is rejected rather
        than quietly shadowing something.
        """

        name = str(spec.get("name", "")).strip()
        if not name or not name.isidentifier():
            raise ValueError(f"capability tool name {name!r} is not a usable identifier")
        if name in self._handlers:
            raise ValueError(
                f"capability tool {name!r} would shadow a built-in tool; rename it"
            )
        if not str(spec.get("description", "")).strip():
            raise ValueError(f"capability tool {name!r} has no description for the model")
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(
                f"capability tool {name!r} must declare an object schema in 'parameters'"
            )
        self._external_specs[name] = {
            "name": name,
            "description": str(spec["description"]),
            "parameters": parameters,
        }
        self._external_handlers[name] = handler

    # -- dispatch ---------------------------------------------------------
    def names(self) -> list[str]:
        return list(self._handlers) + list(self._external_handlers)

    def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        ctx = ctx or ToolContext()
        handler = self._handlers.get(name)
        if handler is None and name in self._external_handlers:
            # Promoted capabilities get their own error handling: a buggy third-party
            # tool must not take the run down, and its failure must be recorded.
            if "_raw" in arguments:
                return ToolResult(
                    tool=name, ok=False,
                    output="argument error: could not parse arguments as a JSON object",
                    error="bad_arguments", category="dispatch",
                )
            try:
                result = self._external_handlers[name](name, arguments, ctx)
            except Exception as exc:
                self.store.append_event(
                    "capability_tool_error",
                    {"tool": name, "error": f"{type(exc).__name__}: {exc}"},
                    task_id=ctx.task_id,
                )
                return ToolResult(
                    tool=name, ok=False,
                    output=f"capability tool raised {type(exc).__name__}: {exc}",
                    error=f"{type(exc).__name__}: {exc}", category="capability",
                )
            if not isinstance(result, ToolResult):
                self.store.append_event(
                    "capability_tool_contract_violation",
                    {"tool": name, "returned": type(result).__name__},
                    task_id=ctx.task_id,
                )
                return ToolResult(
                    tool=name, ok=False,
                    output=(f"capability tool {name!r} returned {type(result).__name__} instead of "
                            f"a ToolResult; the contract is violated"),
                    error="contract_violation", category="capability",
                )
            self.store.append_event("capability_tool_used", {"tool": name, "ok": result.ok},
                                    task_id=ctx.task_id)
            return result
        if handler is None:
            result = ToolResult(
                tool=name, ok=False, denied=False,
                output=f"unknown tool {name!r}; available: {', '.join(self.names())}",
                error="unknown_tool", category="dispatch",
            )
            self.store.append_event("tool_unknown", {"tool": name}, task_id=ctx.task_id)
            return result
        if "_raw" in arguments:
            result = ToolResult(
                tool=name, ok=False,
                output=(
                    f"argument error: could not parse arguments as a JSON object "
                    f"(received {json.dumps(arguments['_raw'])[:200]}). Re-issue the call with "
                    f"valid JSON."
                ),
                error="bad_arguments", category="dispatch",
            )
            self.store.append_event("tool_bad_arguments", {"tool": name}, task_id=ctx.task_id)
            return result
        try:
            return handler(arguments, ctx)
        except ProtectedMemoryError as exc:
            return self._boundary(ctx, name, str(exc), "protected")
        except Exception as exc:  # a tool bug must be reported, never swallowed
            self.store.append_event(
                "tool_error",
                {"tool": name, "error": f"{type(exc).__name__}: {exc}"},
                task_id=ctx.task_id,
            )
            return ToolResult(
                tool=name, ok=False, output=f"tool failed: {type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}", category="internal",
            )

    # -- helpers ----------------------------------------------------------
    def _resolve(self, raw: str, *, base: pathlib.Path | None = None) -> pathlib.Path:
        path = pathlib.Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = (base or self.config.paths.workspace) / path
        return pathlib.Path(os.path.normpath(str(path)))

    def _boundary(self, ctx: ToolContext, tool: str, reason: str, category: str) -> ToolResult:
        """Every refusal is a recorded boundary event, not just a returned string."""

        self.store.append_event(
            "boundary_violation",
            {"tool": tool, "reason": reason, "category": category, "step": ctx.step},
            task_id=ctx.task_id,
        )
        return ToolResult(
            tool=tool, ok=False, denied=True,
            output=(
                f"DENIED by the permission engine ({category}). {reason}\n"
                f"This denial is recorded. Do not attempt the same operation by another route; "
                f"if the task genuinely requires it, stop and report the blocker."
            ),
            error=reason, category=category,
        )

    def _check(self, ctx: ToolContext, tool: str, decision, ) -> ToolResult | None:
        if decision.outcome == DENY:
            return self._boundary(ctx, tool, decision.reason, decision.category)
        if decision.outcome == APPROVAL:
            return self._boundary(
                ctx, tool,
                f"{decision.reason}. Unattended runs cannot grant approval; a human must "
                f"widen the allowlist (LIVIUM_ALLOWED_BINARIES or LIVIUM_EXTRA_WRITE_ROOTS).",
                "command" if decision.category == "command" else decision.category,
            )
        return None

    # -- filesystem tools -------------------------------------------------
    def _read_file(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self._resolve(str(args.get("path", "")))
        blocked = self._check(ctx, "read_file", self.engine.check_read(target))
        if blocked:
            return blocked
        if not target.exists():
            return ToolResult(
                tool="read_file", ok=False, output=f"no such file: {target}",
                data={"path": str(target)}, error="not_found",
            )
        if target.is_dir():
            return ToolResult(
                tool="read_file", ok=False,
                output=f"{target} is a directory; use list_dir", error="is_directory",
            )
        limit = int(args.get("max_bytes") or 80_000)
        raw = target.read_bytes()
        text = raw[:limit].decode("utf-8", errors="replace")
        note = "" if len(raw) <= limit else f"\n[... file is {len(raw)} bytes; showing first {limit}]"
        self.store.append_event(
            "tool_read", {"path": str(target), "bytes": len(raw)}, task_id=ctx.task_id
        )
        return ToolResult(
            tool="read_file", ok=True, output=_text(text) + note,
            data={"path": str(target), "bytes": len(raw),
                  "sha256": hashlib.sha256(raw).hexdigest()},
        )

    def _list_dir(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self._resolve(str(args.get("path", ".")))
        blocked = self._check(ctx, "list_dir", self.engine.check_read(target))
        if blocked:
            return blocked
        if not target.exists():
            return ToolResult(tool="list_dir", ok=False, output=f"no such directory: {target}",
                              error="not_found")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))[:MAX_LISTING]:
            try:
                stat = child.stat()
                size = stat.st_size
            except OSError:
                size = -1
            entries.append(
                {"name": child.name, "type": "dir" if child.is_dir() else "file", "bytes": size}
            )
        listing = "\n".join(
            f"{e['type']:4} {e['bytes']:>9}  {e['name']}" for e in entries
        ) or "(empty)"
        return ToolResult(tool="list_dir", ok=True,
                          output=f"{target}:\n{listing}",
                          data={"path": str(target), "entries": entries})

    def _sha256(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self._resolve(str(args.get("path", "")))
        blocked = self._check(ctx, "sha256", self.engine.check_read(target))
        if blocked:
            return blocked
        if not target.is_file():
            return ToolResult(tool="sha256", ok=False, output=f"not a file: {target}",
                              error="not_found")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return ToolResult(tool="sha256", ok=True, output=f"{digest}  {target.name}",
                          data={"path": str(target), "sha256": digest})

    def _write_file(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_path = str(args.get("path", ""))
        content = args.get("content")
        if not isinstance(content, str):
            return ToolResult(tool="write_file", ok=False,
                              output="content must be a string", error="bad_arguments")
        target = self._resolve(raw_path)
        blocked = self._check(ctx, "write_file", self.engine.check_write(target))
        if blocked:
            return blocked
        payload = content.encode("utf-8")
        if len(payload) > self.config.limits.max_file_write_bytes:
            return ToolResult(
                tool="write_file", ok=False,
                output=f"refused: {len(payload)} bytes exceeds the "
                       f"{self.config.limits.max_file_write_bytes}-byte write limit",
                error="too_large",
            )

        intent_id = self.store.record_intent(
            "write_file", {"path": str(target), "bytes": len(payload)},
            task_id=ctx.task_id, target=str(target),
            expected=f"file exists with sha256 of {len(payload)} bytes",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".livium-tmp-")
        try:
            with os.fdopen(temp_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)  # atomic: readers never see a partial file
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            self.store.resolve_intent(intent_id, "failed", {"error": str(exc)})
            return ToolResult(tool="write_file", ok=False,
                              output=f"write failed: {exc}", error=str(exc))

        observed = hashlib.sha256(target.read_bytes()).hexdigest()
        matches = observed == hashlib.sha256(payload).hexdigest()
        self.store.resolve_intent(
            intent_id, "done" if matches else "failed",
            {"sha256": observed, "verified_by_readback": matches},
        )
        self.store.append_event(
            "tool_write",
            {"path": str(target), "bytes": len(payload), "sha256": observed,
             "verified_by_readback": matches, "purpose": args.get("purpose", "")},
            task_id=ctx.task_id,
        )
        if not matches:
            return ToolResult(
                tool="write_file", ok=False,
                output=f"write reported success but read-back hash differs for {target}; "
                       f"treat this write as unconfirmed",
                error="readback_mismatch", data={"path": str(target)},
            )
        return ToolResult(
            tool="write_file", ok=True,
            output=f"wrote {len(payload)} bytes to {target} (atomic replace; read-back sha256 "
                   f"{observed[:12]}… confirmed)",
            data={"path": str(target), "bytes": len(payload), "sha256": observed,
                  "verified_by_readback": True},
        )

    def _run_command(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        argv = args.get("argv")
        if isinstance(argv, str):
            return ToolResult(
                tool="run_command", ok=False,
                output=("argv must be a JSON array of strings, not a shell string. Livium never "
                        "invokes a shell."),
                error="bad_arguments",
            )
        if not isinstance(argv, list) or not argv:
            return ToolResult(tool="run_command", ok=False,
                              output="argv must be a non-empty array of strings",
                              error="bad_arguments")
        argv = [str(a) for a in argv]
        blocked = self._check(ctx, "run_command", self.engine.check_command(argv))
        if blocked:
            return blocked

        timeout = float(args.get("timeout_seconds") or self.config.limits.max_command_seconds)
        timeout = max(1.0, min(timeout, self.config.limits.max_command_seconds))

        intent_id = self.store.record_intent(
            "run_command", {"argv": argv, "timeout": timeout}, task_id=ctx.task_id,
            expected="process exits and its exit code is observed",
        )
        result = self.runner.run(argv, cwd=self.config.paths.workspace, timeout=timeout)
        self.store.resolve_intent(
            intent_id, "done" if result.ok else "failed",
            {"exit_code": result.exit_code, "timed_out": result.timed_out,
             "duration_ms": result.duration_ms},
        )
        self.store.append_event(
            "tool_exec",
            {"argv": argv, "exit_code": result.exit_code, "timed_out": result.timed_out,
             "duration_ms": result.duration_ms, "network_isolated": result.network_isolated,
             "purpose": args.get("purpose", "")},
            task_id=ctx.task_id,
        )
        body = [f"$ {self.config.quote_command(argv)}", f"exit_code={result.exit_code}"
                f"  duration={result.duration_ms}ms"
                f"  network_isolated={result.network_isolated}"]
        if result.stdout:
            body.append("--- stdout ---\n" + _text(result.stdout, self.config.limits.max_tool_output_bytes))
        if result.stderr:
            body.append("--- stderr ---\n" + _text(result.stderr, self.config.limits.max_tool_output_bytes))
        if result.timed_out:
            body.append("--- note ---\nprocess was killed after exceeding the timeout")
        return ToolResult(
            tool="run_command", ok=result.ok, output="\n".join(body),
            data={
                "exit_code": result.exit_code, "timed_out": result.timed_out,
                "duration_ms": result.duration_ms, "stdout": result.stdout[:4000],
                "stderr": result.stderr[:4000], "network_isolated": result.network_isolated,
            },
            error=None if result.ok else f"exit_code={result.exit_code}",
        )

    # -- memory tools -----------------------------------------------------
    def _memory_search(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(tool="memory_search", ok=False, output="query is required",
                              error="bad_arguments")
        kinds = args.get("kinds") or None
        if kinds is not None and not isinstance(kinds, list):
            return ToolResult(tool="memory_search", ok=False, output="kinds must be a list",
                              error="bad_arguments")
        limit = int(args.get("limit") or 8)
        records = self.memory.search(query, kinds=kinds, limit=max(1, min(limit, 40)))
        self.store.append_event(
            "memory_search", {"query": query, "hits": [r.id for r in records]}, task_id=ctx.task_id
        )
        if not records:
            return ToolResult(
                tool="memory_search", ok=True,
                output=f"no memory records matched {query!r}. That means nothing is stored on "
                       f"this topic; it does not mean the topic is unimportant.",
                data={"records": []},
            )
        return ToolResult(
            tool="memory_search", ok=True,
            output="\n".join(r.render() for r in records),
            data={"records": [r.as_dict() for r in records]},
        )

    def _memory_write(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        kind = str(args.get("kind", "")).strip()
        content = str(args.get("content", "")).strip()
        if kind == "foundational":
            return self._boundary(
                ctx, "memory_write",
                "foundational records are protected: the agent may not write or overwrite its own "
                "enduring instructions through a tool call",
                "protected",
            )
        try:
            memory_id, action = self.memory.remember(
                kind,
                content,
                key=args.get("key") or None,
                scope=str(args.get("scope") or "global"),
                detail=args.get("detail"),
                tags=args.get("tags") or (),
                confidence=float(args.get("confidence", 0.5)),
                verification=str(args.get("verification") or "unverified"),
                evidence=args.get("evidence"),
                source=f"agent:{self.config.model_label()}",
                task_id=ctx.task_id,
            )
        except ValueError as exc:
            return ToolResult(tool="memory_write", ok=False, output=str(exc), error="bad_arguments")
        record = self.memory.get(memory_id)
        assert record is not None
        return ToolResult(
            tool="memory_write", ok=True,
            output=f"{action}: mem:{memory_id} stored. {record.render(full=False)}",
            data={"id": memory_id, "action": action},
        )

    def _memory_update(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            memory_id = int(args.get("id"))
        except (TypeError, ValueError):
            return ToolResult(tool="memory_update", ok=False, output="id must be an integer",
                              error="bad_arguments")
        try:
            record = self.memory.update(
                memory_id,
                content=args.get("content"),
                confidence=float(args["confidence"]) if args.get("confidence") is not None else None,
                verification=args.get("verification"),
                evidence=args.get("evidence"),
                detail=args.get("detail"),
                task_id=ctx.task_id,
            )
        except KeyError as exc:
            return ToolResult(tool="memory_update", ok=False, output=str(exc), error="not_found")
        except ValueError as exc:
            return ToolResult(tool="memory_update", ok=False, output=str(exc), error="bad_arguments")
        return ToolResult(tool="memory_update", ok=True,
                          output=f"updated mem:{record.id}\n{record.render()}",
                          data={"id": record.id})

    def _memory_forget(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            memory_id = int(args.get("id"))
        except (TypeError, ValueError):
            return ToolResult(tool="memory_forget", ok=False, output="id must be an integer",
                              error="bad_arguments")
        try:
            record = self.memory.forget(
                memory_id, reason=str(args.get("reason", "no reason given")), task_id=ctx.task_id
            )
        except KeyError as exc:
            return ToolResult(tool="memory_forget", ok=False, output=str(exc), error="not_found")
        return ToolResult(tool="memory_forget", ok=True,
                          output=f"retired mem:{record.id} (history retained; it will no longer "
                                 f"be retrieved)",
                          data={"id": record.id, "status": record.status})

    # -- governance tools -------------------------------------------------
    def _request_capability(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        requirement = str(args.get("requirement", "")).strip()
        reuse = str(args.get("reuse_analysis", "")).strip()
        if not name or not requirement or not reuse:
            return ToolResult(
                tool="request_capability", ok=False,
                output="name, requirement and reuse_analysis are all required: a proposal "
                       "without a reuse analysis is not reviewable",
                error="bad_arguments",
            )
        from .capabilities import CapabilityRegistry

        registry = CapabilityRegistry(self.store)
        record = registry.propose(
            name=name, requirement=requirement, reuse_analysis=reuse,
            rationale=str(args.get("rationale") or ""),
            verification_plan=str(args.get("verification_plan") or ""),
            reversible=bool(args.get("reversible", True)),
            task_id=ctx.task_id,
        )
        return ToolResult(
            tool="request_capability", ok=True,
            output=(
                f"proposal recorded: capability {name!r} is now status='proposed'. Nothing was "
                f"implemented and no permission was granted. To move it forward, write the "
                f"implementation into {self.config.paths.capability_lab} and run its tests; "
                f"promotion to 'active' requires a human to run `livium capability promote`."
            ),
            data=record.as_dict(),
        )

    def _finish_task(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        summary = str(args.get("summary", "")).strip()
        status = str(args.get("status", "complete")).strip()
        if status not in ("complete", "partial", "blocked", "failed"):
            return ToolResult(tool="finish_task", ok=False, output="invalid status",
                              error="bad_arguments")
        self.store.append_event(
            "task_finished",
            {"summary": summary, "status": status, "unverified": args.get("unverified", "")},
            task_id=ctx.task_id,
        )
        return ToolResult(
            tool="finish_task", ok=True,
            output="task marked finished by the model; the harness now checks acceptance criteria.",
            data={"summary": summary, "status": status,
                  "unverified": args.get("unverified", "")},
        )
