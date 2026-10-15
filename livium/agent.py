"""The operational loop.

Receive -> Understand -> Retrieve -> Plan -> Act -> Observe -> Verify -> Remember
-> Continue. One class, because nine classes for nine verbs would be scaffolding
of exactly the kind this project forbids.

What is real here, and what the loop refuses to do:

* Every assistant turn comes from an actual HTTP call to the configured provider.
  There is no branch that invents a completion when the provider is unavailable;
  the task ends as ``blocked`` and says why.
* Every tool result is the observed result of real code. The model's text is not
  an observation.
* The model cannot declare success. ``finish_task`` only *asks*; the harness then
  runs the acceptance criteria through ``livium.verification`` and sets the status
  from what those checks found.
* Stopping conditions are enforced (steps, tool calls, wall clock, freeze, repeated
  identical tool calls). An agent that cannot stop is not useful.
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid
from typing import Any, Callable, Sequence

from .capabilities import CapabilityRegistry
from .config import Config
from .context import ContextManager, TokenBudget
from .execution import Runner
from .guardian import GrowthGuardian
from .memory import Memory
from .models import (
    Conversation,
    CredentialMissing,
    ModelClient,
    ModelError,
    ProviderRejected,
    ProviderUnavailable,
    build_client,
)
from .permissions import PermissionEngine
from .recovery import Recovery
from .store import Store, now
from .tools import ToolContext, ToolRegistry, ToolResult
from .verification import Verifier, normalize_criteria, summarize


@dataclasses.dataclass
class TaskRun:
    task_id: str
    objective: str
    status: str
    steps: int
    model_calls: int          # completions attempted
    model_failures: int       # attempts that produced no completion
    tool_calls: int
    denials: int
    verification: dict[str, Any]
    criteria: list[dict[str, Any]]
    criteria_results: list[dict[str, Any]]
    observations: list[str]
    final_summary: str
    unverified: str
    guardian_consequences: list[dict[str, Any]]
    frozen: bool
    freeze_reason: str | None
    provider: dict[str, Any]
    context_reports: list[dict[str, Any]]
    reused_capabilities: list[str]
    generated_capabilities: list[str]
    error: str | None = None
    is_test_double: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def headline(self) -> str:
        return (
            f"task {self.task_id[:8]} [{self.status}] steps={self.steps} "
            f"model_calls={self.model_calls}"
            + (f" ({self.model_failures} failed)" if self.model_failures else "")
            + f" tool_calls={self.tool_calls} "
            f"verification={self.verification.get('state')}"
        )


class LiviumAgent:
    def __init__(
        self,
        config: Config,
        *,
        client: ModelClient | None = None,
        store: Store | None = None,
        script: Sequence[dict[str, Any]] | None = None,
        verify_timeout: float | None = None,
    ):
        self.config = config
        self.config.ensure_dirs()
        self.store = store or Store(config.paths.db)
        self.engine = PermissionEngine(config)
        self.memory = Memory(self.store)
        self.guardian = GrowthGuardian(config, self.engine, self.store, self.memory)
        self.capabilities = CapabilityRegistry(
            self.store, repo_root=_repo_root(), lab=config.paths.capability_lab
        )
        self.runner = Runner(
            max_seconds=config.limits.max_command_seconds,
            max_output_bytes=config.limits.max_tool_output_bytes,
        )
        self.tools = ToolRegistry(config, self.engine, self.store, self.memory, self.runner)
        self.verifier = Verifier(config, self.engine, self.store, self.memory, self.runner)
        self.context = ContextManager(config, self.store, self.memory, self.capabilities, self.guardian)
        self.client = client or build_client(config, script)
        self.recovery = Recovery(self.store, self.engine, config)
        self.loaded_capabilities: list[str] = []
        self.capability_load_failures: list[str] = []
        self._probe: dict[str, Any] | None = None
        self._verify_timeout = verify_timeout

    # -- lifecycle --------------------------------------------------------
    def start(self) -> dict[str, Any]:
        """Real startup: seed instructions into memory, probe provider, load capabilities."""

        seeded = self.memory.seed_foundational(self.config.paths.prompts)
        self.store.append_event("session_start", {
            "provider": self.config.model.provider,
            "model": self.config.model.model,
            "foundational": seeded,
        })

        integrity = self.guardian.integrity()
        if not integrity.ok:
            # Not fatal on purpose: an unbaselined install is a configuration gap,
            # whereas a *mismatch* freezes the run. Both are reported loudly.
            self.store.append_event("guardian_integrity_warning", integrity.as_dict())

        diagnostics = self.load_promoted_capabilities()
        probe = self.probe_provider()
        self.context.set_tool_names(self.tools.specs_names())
        return {
            "foundational": seeded,
            "guardian_integrity": integrity.as_dict(),
            "guardian_frozen": self.guardian.frozen,
            "provider_probe": probe,
            "capabilities_loaded": diagnostics,
            "tools": self.tools.names(),
            "context": TokenBudget(self.config, self.store).describe(),
            "isolation": self.runner.isolation.as_dict(),
            "config": self.config.describe(),
        }

    def probe_provider(self) -> dict[str, Any]:
        """Detection and validation. Records the real answer; never assumes."""

        try:
            result = self.client.validate()
            payload = {
                "ok": result.ok, "detail": result.detail, "model": self.config.model.model,
                "models_offered": len(result.models), "model_present": result.model_present,
                "is_test_double": self.client.is_test_double,
            }
        except ModelError as exc:
            payload = {
                "ok": False, "detail": str(exc), "model": self.config.model.model,
                "models_offered": 0, "model_present": False,
                "is_test_double": self.client.is_test_double,
            }
        window, source = self.client.context_window()
        payload["context_window"] = window
        payload["context_window_source"] = source
        self._probe = payload
        self.store.append_event("provider_probe", payload)
        return payload

    def load_promoted_capabilities(self) -> dict[str, Any]:
        """Import promoted capability modules and register their tools.

        Growth is real only if a promoted capability changes what the agent can
        do, so this is the load path that makes that true. A module that fails to
        import is excluded, reported, and recorded as a failure -- never skipped
        silently.
        """

        from .promoted import load_promoted

        loaded: list[str] = []
        failures: list[str] = []
        for spec, handler, capability_name in load_promoted(self.capabilities.promoted_dir):
            self.tools.register_external(spec, handler)
            loaded.append(spec["name"])
            if capability_name not in self.loaded_capabilities:
                self.loaded_capabilities.append(capability_name)
        for path, error in load_promoted.last_failures:
            failures.append(f"{path}: {error}")
            self.store.append_event(
                "capability_load_failed", {"path": str(path), "error": error}
            )
        self.capability_load_failures = failures
        return {
            "loaded_tools": loaded,
            "capabilities": self.loaded_capabilities,
            "failures": failures,
        }

    def services(self) -> dict[str, Any]:
        """Live objects handed to tools. Promoted capabilities receive these."""

        return {
            "memory": self.memory,
            "store": self.store,
            "config": self.config,
            "guardian": self.guardian,
            "capabilities": self.capabilities,
            "tools": self.tools,
        }

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "LiviumAgent":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the loop ---------------------------------------------------------
    def run_task(
        self,
        objective: str,
        acceptance: Any = None,
        *,
        task_id: str | None = None,
        resume: bool = False,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> TaskRun:
        started = time.monotonic()
        criteria = normalize_criteria(acceptance)
        task_id = task_id or uuid.uuid4().hex[:12]

        observations: list[str] = []
        context_reports: list[dict[str, Any]] = []
        criteria_results: list[dict[str, Any]] = []
        denials = 0
        tool_call_count = 0
        model_calls = 0
        model_failures = 0
        final_summary = ""
        unverified_note = ""
        status = "in_progress"
        error: str | None = None
        repeat_counts: dict[str, int] = {}
        nudged = False
        finished = False
        resume_report: dict[str, Any] | None = None

        self._open_task(task_id, objective, criteria)
        if resume:
            report = self.recovery.inspect(task_id)
            resume_report = report.as_dict()
            observations.extend(report.observations)
            observations.append(f"Recovery: {report.next_action}")
            self.store.append_event("task_resumed", resume_report, task_id=task_id)

        conversation = Conversation()
        step = 0
        try:
            while step < self.config.limits.max_steps:
                if self.guardian.frozen:
                    status = "blocked"
                    error = f"guardian froze the run: {self.guardian.frozen_reason}"
                    observations.append(error)
                    break
                if time.monotonic() - started > self.config.limits.max_seconds:
                    status = "blocked"
                    error = (
                        f"wall-clock limit of {self.config.limits.max_seconds:g}s reached; the task "
                        f"is unfinished"
                    )
                    observations.append(error)
                    break

                step += 1
                task_row = self._task_row(task_id)
                pending = [
                    {"id": row["id"], "tool": row["tool"], "args": row["args"],
                     "created_at": row["created_at"]}
                    for row in self.store.pending_intents(task_id)
                ]
                built = self.context.build(
                    task=task_row, step=step, observations=observations,
                    criteria_results=criteria_results, pending_intents=pending,
                )
                context_reports.append(built.report)
                if not conversation.messages:
                    conversation.system = built.system
                conversation.add_user(built.prompt)
                self._compact(conversation, built, observations)

                self._step_begin(task_id, step, "model_call", f"objective: {objective[:120]}")
                reply = self._call_model(conversation, built, observations)
                model_calls += 1
                if reply is None:
                    model_failures += 1
                    status = "blocked"
                    error = observations[-1] if observations else "model call failed"
                    self._step_end(task_id, step, "failed", error)
                    break

                conversation.add_assistant(reply.text, reply.tool_calls)
                if reply.text:
                    observations.append(f"model: {reply.text[:400]}")

                if not reply.tool_calls:
                    if not finished and not nudged:
                        nudged = True
                        conversation.add_user(
                            "You produced no tool call. If the task is complete, call finish_task "
                            "with an honest summary. Otherwise call a tool to make progress. Text "
                            "alone is not an observation."
                        )
                        self._step_end(task_id, step, "complete", "text-only reply; nudged once")
                        continue
                    status = "partial"
                    final_summary = reply.text or "(model returned no summary)"
                    self._step_end(task_id, step, "complete", "text-only reply; loop ends")
                    break

                step_results: list[ToolResult] = []
                for call in reply.tool_calls:
                    if tool_call_count >= self.config.limits.max_tool_calls:
                        observations.append(
                            f"tool-call limit of {self.config.limits.max_tool_calls} reached"
                        )
                        break
                    if self.guardian.frozen:
                        break
                    signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                    repeat_counts[signature] = repeat_counts.get(signature, 0) + 1
                    if repeat_counts[signature] >= 3:
                        status = "blocked"
                        error = (
                            f"the same tool call was issued {repeat_counts[signature]} times with "
                            f"identical arguments ({call.name}); repeating it is not making progress"
                        )
                        observations.append(error)
                        self.store.append_event(
                            "loop_detected", {"tool": call.name, "args": call.arguments},
                            task_id=task_id,
                        )
                        break

                    result = self.tools.invoke(
                        call.name, call.arguments,
                        ToolContext(task_id=task_id, step=step,
                                    purpose=str(call.arguments.get("purpose", "")),
                                    services=self.services()),
                    )
                    tool_call_count += 1
                    if result.denied:
                        denials += 1
                    self._post_tool_integrity(result.tool, task_id)
                    step_results.append(result)
                    conversation.add_tool_result(call, _tool_message(result))
                    observations.append(_observation_line(result))
                    if result.tool == "finish_task" and result.ok:
                        finished = True
                        final_summary = str(result.data.get("summary", ""))
                        unverified_note = str(result.data.get("unverified", ""))
                        status = {"complete": "complete", "partial": "partial",
                                  "blocked": "blocked", "failed": "failed"}.get(
                            str(result.data.get("status")), "partial")
                self._step_end(
                    task_id, step,
                    "complete" if not self.guardian.frozen else "failed",
                    "; ".join(r.tool + ("(denied)" if r.denied else "") for r in step_results)
                    or "no tool executed",
                )
                if on_step:
                    on_step({"step": step, "observations": observations[-3:],
                             "status": status if finished else "in_progress"})

                if finished or status == "blocked":
                    break
            else:
                status = "blocked"
                error = (
                    f"step limit of {self.config.limits.max_steps} reached without finish_task; "
                    f"the task is unfinished"
                )
                observations.append(error)

            if self.guardian.frozen:
                status = "blocked"
                error = f"guardian froze the run: {self.guardian.frozen_reason}"

        except Exception as exc:  # a loop bug must surface as a failed task, not a crash
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            observations.append(error)
            self.store.append_event(
                "task_exception", {"error": error, "type": type(exc).__name__}, task_id=task_id
            )

        # -- verification, always, from the real world --------------------
        if self._verify_timeout is not None:
            self.runner.max_seconds = self._verify_timeout
        results = self.verifier.verify(criteria, task_id=task_id)
        criteria_results = [r.as_dict() for r in results]
        summary = summarize(results)

        if status == "complete" and summary["state"] != "verified":
            status = "partial"
            unverified_note = (
                unverified_note
                or f"acceptance checks report '{summary['state']}'; the model's completion claim "
                   f"alone does not close the task"
            ).strip()

        consequences = [c.as_dict() for c in self.guardian.consequences(task_id)]
        self._close_task(task_id, status, final_summary, summary, step, error)
        self._remember_outcome(task_id, objective, status, summary, observations, error,
                               denials=denials, resume_report=resume_report)

        run = TaskRun(
            task_id=task_id,
            objective=objective,
            status=status,
            steps=step,
            model_calls=model_calls,
            model_failures=model_failures,
            tool_calls=tool_call_count,
            denials=denials,
            verification=summary,
            criteria=criteria,
            criteria_results=criteria_results,
            observations=observations,
            final_summary=final_summary,
            unverified=unverified_note,
            guardian_consequences=consequences,
            frozen=self.guardian.frozen,
            freeze_reason=self.guardian.frozen_reason,
            provider=self._probe or {},
            context_reports=context_reports,
            reused_capabilities=list(self.loaded_capabilities),
            generated_capabilities=self._capabilities_touched(task_id),
            error=error,
            is_test_double=self.client.is_test_double,
        )
        self.store.append_event(
            "task_run_complete",
            {"status": status, "steps": step, "model_calls": model_calls,
             "model_failures": model_failures,
             "tool_calls": tool_call_count, "denials": denials,
             "verification": summary, "error": error},
            task_id=task_id,
        )
        return run

    # -- model call -------------------------------------------------------
    def _call_model(
        self, conversation: Conversation, built, observations: list[str]
    ) -> Any:
        """One real completion, with an honest response to context rejection."""

        tools = self.tools.specs()
        prompt_estimate = built.report["tokens_estimated"]
        for attempt in (1, 2):
            try:
                reply = self.client.complete(conversation, tools)
            except ProviderRejected as exc:
                message = str(exc)
                too_long = any(
                    token in message.lower()
                    for token in ("context", "too long", "maximum", "token", "length")
                )
                if too_long and attempt == 1:
                    # Real recovery for a real failure mode: drop the oldest context
                    # turns and retry once, then report honestly if it still fails.
                    dropped = self._compact(conversation, built, observations, aggressive=True)
                    observations.append(
                        f"provider rejected the request as too long; dropped {dropped} older "
                        f"context turn(s) and retrying once"
                    )
                    self.store.append_event(
                        "context_rejected",
                        {"attempt": attempt, "dropped_turns": dropped, "error": message[:500]},
                    )
                    continue
                observations.append(f"provider rejected the request: {message[:600]}")
                return None
            except (ProviderUnavailable, CredentialMissing) as exc:
                observations.append(
                    f"model unavailable: {exc}. No completion was produced and none was "
                    f"invented; the task is blocked on this dependency."
                )
                self.store.append_event("model_unavailable", {"error": str(exc)})
                return None
            except ModelError as exc:
                observations.append(f"model error: {exc}")
                self.store.append_event("model_error", {"error": str(exc)})
                return None

            self.store.append_event(
                "model_reply",
                {
                    "model": reply.model, "provider": reply.provider,
                    "latency_ms": reply.latency_ms, "stop_reason": reply.stop_reason,
                    "usage": reply.usage, "tool_calls": [c.name for c in reply.tool_calls],
                    "text": reply.text[:2000], "is_test_double": reply.is_test_double,
                    "context_estimated_tokens": prompt_estimate,
                },
            )
            self._calibrate(prompt_estimate, reply.usage)
            return reply
        return None

    def _calibrate(self, estimated: int, usage: dict[str, Any]) -> None:
        observed = usage.get("prompt_tokens") or usage.get("input_tokens")
        if isinstance(observed, int) and observed > 0:
            budget = TokenBudget(self.config, self.store)
            budget.calibrate(estimated, observed)

    def _compact(
        self, conversation: Conversation, built, observations: list[str], *, aggressive: bool = False
    ) -> int:
        """Drop whole oldest turns when the prompt would exceed the real budget.

        Turn boundaries are preserved so an assistant message carrying tool calls is
        never separated from its results, which providers reject as malformed.
        Dropping is recorded; it is never silent.
        """

        budget = TokenBudget(self.config, self.store)
        limit = budget.available
        if aggressive:
            limit = int(limit * 0.6)
        dropped = 0
        while len(conversation.messages) > 2:
            prompt_text = "\n".join(
                str(m.get("content", "")) for m in conversation.messages
            )
            if budget.estimate(prompt_text) <= limit:
                break
            # Drop everything up to the second user (context) message.
            boundaries = [
                index for index, message in enumerate(conversation.messages)
                if message["role"] == "user" and "Working context" in str(message.get("content", ""))
            ]
            if len(boundaries) < 2:
                break
            del conversation.messages[: boundaries[-1]]
            dropped += 1
            self.store.append_event(
                "context_compacted",
                {"dropped_turns": dropped, "reason": "prompt exceeded the budget"},
            )
        if dropped:
            observations.append(
                f"context: dropped {dropped} older turn(s) to fit the prompt budget; tool results "
                f"from the current step are retained"
            )
        return dropped

    # -- persistence helpers ----------------------------------------------
    def _open_task(self, task_id: str, objective: str, criteria: list[dict[str, Any]]) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO task(id, objective, acceptance, status, model, created_at, updated_at, "
                "next_action) VALUES(?,?,?, 'active', ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET objective=excluded.objective, "
                "acceptance=excluded.acceptance, status='active', updated_at=excluded.updated_at",
                (task_id, objective, json.dumps(criteria), self.config.model_label(),
                 now(), now(), "begin the loop"),
            )
        self.store.append_event(
            "task_started",
            {"objective": objective, "criteria": criteria, "model": self.config.model_label()},
            task_id=task_id,
        )

    def _task_row(self, task_id: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM task WHERE id=?", (task_id,))
        if row is None:
            return {"id": task_id, "objective": "", "acceptance": [], "status": "active",
                    "next_action": None, "summary": None}
        data = dict(row)
        try:
            data["acceptance"] = [c.get("kind", "?") for c in json.loads(data.get("acceptance") or "[]")]
        except json.JSONDecodeError:
            data["acceptance"] = []
        return data

    def _step_begin(self, task_id: str, index: int, phase: str, detail: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO task_step(task_id, idx, phase, status, detail, started_at) "
                "VALUES(?,?,?, 'in_progress', ?, ?)",
                (task_id, index, phase, detail, now()),
            )
            conn.execute(
                "UPDATE task SET steps=?, updated_at=? WHERE id=?", (index, now(), task_id)
            )

    def _step_end(self, task_id: str, index: int, status: str, detail: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE task_step SET status=?, detail=?, ended_at=? WHERE task_id=? AND idx=?",
                (status, detail, now(), task_id, index),
            )
            conn.execute(
                "UPDATE task SET next_action=?, updated_at=? WHERE id=?",
                (detail[:400], now(), task_id),
            )

    def _close_task(
        self, task_id: str, status: str, summary_text: str, verification: dict[str, Any],
        steps: int, error: str | None,
    ) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE task SET status=?, summary=?, updated_at=?, next_action=? WHERE id=?",
                (
                    status,
                    json.dumps({"summary": summary_text, "verification": verification,
                                "error": error}),
                    now(),
                    None if status in ("complete",) else "inspect the report and decide whether to continue",
                    task_id,
                ),
            )

    def _post_tool_integrity(self, tool: str, task_id: str) -> None:
        """After any operation that could touch the filesystem, verify integrity.

        Cheap (hashing tens of kilobytes) and immediate, so a bypass attempt is
        caught in the step that performed it rather than at the end of the run.
        """

        if tool in ("run_command", "write_file"):
            self.guardian.watch(task_id=task_id, trigger=tool)

    def _capabilities_touched(self, task_id: str) -> list[str]:
        names: set[str] = set()
        for row in self.store.events(task_id=task_id, limit=200):
            if row["type"] in ("capability_proposed", "capability_scaffolded", "capability_tested",
                               "capability_promoted"):
                try:
                    name = json.loads(row["payload"]).get("name")
                except json.JSONDecodeError:
                    continue
                if name:
                    names.add(name)
        return sorted(names)

    def _remember_outcome(
        self,
        task_id: str,
        objective: str,
        status: str,
        verification: dict[str, Any],
        observations: list[str],
        error: str | None,
        *,
        denials: int,
        resume_report: dict[str, Any] | None,
    ) -> None:
        """Persist the outcome. Task continuity and experience are stored whether
        or not the model chose to call a memory tool: continuity must not depend on
        the model remembering to write it down."""

        state = verification.get("state", "unverified")
        verification_state = "tested" if verification.get("passed") else "claimed"
        if state == "verified":
            verification_state = "verified"
        elif state == "not verified":
            verification_state = "unverified"
        elif state.startswith("partially"):
            verification_state = "executed"

        detail = (
            f"status={status}; verification={state} ({verification.get('passed', 0)}/"
            f"{verification.get('total', 0)} criteria passed); error={error or 'none'}; "
            f"requested_denials={denials}"
        )
        self.memory.remember(
            "task",
            f"Task '{objective[:180]}' ended {status}",
            key=f"task:{task_id}",
            detail=detail + (f"\nlast observations: {' | '.join(observations[-3:])[:600]}"
                             if observations else ""),
            tags=["continuity", status],
            confidence=0.9 if state == "verified" else 0.6,
            verification=verification_state,
            evidence=f"task row {task_id}; verification table; journal events for task {task_id}",
            source="harness:post-task",
            task_id=task_id,
        )

        if status in ("complete",) and state == "verified":
            self.memory.remember(
                "experience",
                f"Approach succeeded for: {objective[:150]}",
                key=f"experience:success:{task_id}",
                detail=(
                    f"All {verification.get('total')} acceptance criteria passed, checked by the "
                    f"harness. Conditions: provider={self.config.model_label()}."
                ),
                tags=["success", "verified"],
                confidence=0.8,
                verification="verified",
                evidence=f"verification rows for task {task_id}",
                source="harness:post-task",
                task_id=task_id,
            )
        if status in ("blocked", "failed", "partial") or error:
            # A partial run is unfinished work, so it belongs in failure memory as a
            # hypothesis to check next time -- not only runs that stopped outright.
            self.memory.remember(
                "failure",
                f"{status}: {objective[:150]}",
                key=f"failure:{task_id}",
                detail=(
                    f"suspected cause: {error or 'not determined'}\n"
                    f"observation: {' | '.join(observations[-4:])[:600]}\n"
                    f"corrective action: not yet established — this is a hypothesis, not a "
                    f"confirmed cause. Re-check the evidence before treating it as a permanent "
                    f"restriction."
                ),
                tags=["failure", status],
                confidence=0.4,
                verification="unverified",
                evidence=f"journal events for task {task_id}",
                source="harness:post-task",
                task_id=task_id,
            )
        if denials:
            self.memory.remember(
                "failure",
                f"{denials} tool request(s) were refused by the permission engine during "
                f"'{objective[:100]}'",
                key=f"denials:{task_id}",
                detail=(
                    "Each refusal is a boundary event in the journal with the exact reason. The "
                    "permission boundary is not a suggestion; if the task genuinely requires the "
                    "operation, the allowlist must be widened by a human."
                ),
                tags=["boundary", "denials"],
                confidence=0.95,
                verification="verified",
                evidence=f"boundary_violation events for task {task_id}",
                source="harness:post-task",
                task_id=task_id,
            )
        if resume_report:
            findings = resume_report.get("findings", [])
            if findings:
                self.memory.remember(
                    "experience",
                    f"Recovered a task with {len(findings)} interrupted operation(s)",
                    key=f"recovery:{task_id}",
                    detail=json.dumps(findings)[:1200],
                    tags=["recovery"],
                    confidence=0.7,
                    verification="executed",
                    evidence=f"recovery_finding events for task {task_id}",
                    source="harness:recovery",
                    task_id=task_id,
                )

    # -- reporting --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "config": self.config.describe(),
            "stats": self.store.stats(),
            "guardian": {
                "integrity": self.guardian.integrity().as_dict(),
                "freeze": self.guardian.freeze_status(),
                "consequences": [c.as_dict() for c in self.guardian.consequences(None)],
            },
            "isolation": self.runner.isolation.as_dict(),
            "capabilities": [c.as_dict() for c in self.capabilities.all()],
            "loaded_capability_tools": sorted(self.loaded_capabilities),
            "capability_load_failures": self.capability_load_failures,
            "tools": self.tools.names(),
            "permissions": self.engine.describe(),
            "probe": self._probe,
        }


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parent.parent


def _tool_message(result: ToolResult) -> str:
    prefix = "DENIED" if result.denied else ("OK" if result.ok else "ERROR")
    body = result.output or result.error or ""
    return f"[{prefix}] {result.tool}\n{body}"


def _observation_line(result: ToolResult) -> str:
    if result.denied:
        return f"DENIED {result.tool}: {str(result.error)[:200]}"
    if not result.ok:
        return f"FAILED {result.tool}: {str(result.error or result.output)[:200]}"
    summary = result.output.replace("\n", " ")[:200]
    return f"{result.tool}: {summary}"
