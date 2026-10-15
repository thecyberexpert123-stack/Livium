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
import functools
import json
import pathlib
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
from .ledger import Ledger, classify as classify_task, evidence_for_experience
from .recovery import Recovery
from .discovery import (
    BLOCKED, COMPLETED, FollowUpBudget, FollowUpCycle, INVESTIGATING, POSTPONED,
    READY, REJECTED, DiscoveryQueue,
)
from .router import (
    CHANGE_APPROACH, EVALUATE_DISCOVERIES, EXECUTE, INSPECT_RESULT, INVESTIGATE_FAILURE,
    INVESTIGATE_GAP, PERSIST, REQUEST_AUTHORIZATION, RETRIEVE_CONTEXT, RETRY,
    SELECT_CAPABILITY, VERIFY, StepContext, TaskRouter, classify_failure, mentions_gap,
    signature_of,
)
from .store import Store, now
from .tools import TOOL_META, ToolContext, ToolRegistry, ToolResult
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
    route_decisions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    approvals_requested: int = 0
    ledger_id: int | None = None

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
            self.store, repo_root=_repo_root(), lab=config.paths.capability_lab,
            promoted=config.paths.promoted,
        )
        self.runner = Runner(
            max_seconds=config.limits.max_command_seconds,
            max_output_bytes=config.limits.max_tool_output_bytes,
        )
        #: Deferred discoveries: the queue is durable state, not a per-run list, so it
        #: survives the process that captured it (see livium.discovery).
        self.discoveries = DiscoveryQueue(
            self.store, max_depth=config.limits.followup_max_depth)
        self.tools = ToolRegistry(config, self.engine, self.store, self.memory, self.runner)
        self.verifier = Verifier(config, self.engine, self.store, self.memory, self.runner)
        self.context = ContextManager(config, self.store, self.memory, self.capabilities, self.guardian)
        self.client = client or build_client(config, script)
        self.recovery = Recovery(self.store, self.engine, config)
        self.loaded_capabilities: list[str] = []
        self.capability_load_failures: list[str] = []
        self._probe: dict[str, Any] | None = None
        self._verify_timeout = verify_timeout

    def _verify_criteria(self, criteria: list[dict[str, Any]], task_id: str):
        """Run the acceptance criteria for real, once per task, and cache the result.

        Verification is the harness checking the world, not the model reporting on
        itself, so it is run at the moment the model claims completion and the same
        result is reused when the loop ends. Re-running the checks would double the
        cost of a task to reach the same answer.
        """

        cached = self._criteria_cache.get(task_id)
        if cached is not None:
            return cached[0]
        if self._verify_timeout is not None:
            self.runner.max_seconds = self._verify_timeout
        results = self.verifier.verify(criteria, task_id=task_id)
        summary = summarize(results)
        self._criteria_cache[task_id] = (results, summary)
        return results

    def _criteria_summary(self, criteria: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
        self._verify_criteria(criteria, task_id)
        return self._criteria_cache[task_id][1]

    # -- deferred work ----------------------------------------------------
    def _budget(self) -> FollowUpBudget:
        limits = self.config.limits
        return FollowUpBudget(
            max_items=limits.followup_max_items,
            max_seconds=limits.followup_max_seconds,
            max_model_calls=limits.followup_max_model_calls,
            max_steps=limits.followup_max_steps,
            max_tokens=limits.followup_max_tokens,
        )

    def _run_followups(self, objective: str, *, task_id: str) -> dict[str, Any]:
        """Evaluate ready discoveries after the task that produced them is verified.

        The cycle has its own budget and its own record. It is allowed to investigate,
        to conclude that nothing needs doing, or to establish that the observation was
        about the primary task after all -- and it is not allowed to become the rest of
        the run: when the budget is spent, the remaining items stay queued and the stop
        is journalled with the reason, so a later cycle resumes exactly there.
        """

        cycle = FollowUpCycle(self.discoveries, budget=self._budget())
        try:
            results = cycle.run(
                functools.partial(self._followup_runner, task_id=task_id),
                clock=time.monotonic, task_id=task_id)
        except Exception as exc:  # a cycle must never fail the task it follows
            self.store.append_event(
                "discovery_cycle_failed",
                {"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"},
                task_id=task_id,
            )
            return {"evaluated": 0, "completed": 0, "rejected": 0, "postponed": 0,
                    "blocked": 0, "carried": 0, "resolved": [], "evidence": [],
                    "objective": objective,
                    "summary": f"the follow-up cycle failed ({type(exc).__name__}); the "
                               f"queue is unchanged",
                    "stopped_because": "the cycle raised, so no deferred work was evaluated"}

        resolved = [r.discovery_id for r in results if r.status in (COMPLETED, REJECTED)]
        evidence = [{"id": r.discovery_id, "status": r.status, "evidence": r.evidence,
                     "reason": r.reason} for r in results]
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
        if results:
            summary = (
                f"{len(results)} evaluated: "
                + ", ".join(f"{count} {state}" for state, count in sorted(counts.items()))
                + f"; {len(self.discoveries.eligible())} still ready"
            )
        else:
            summary = "nothing was ready to evaluate"
        return {
            "evaluated": len(results), "completed": counts.get(COMPLETED, 0),
            "rejected": counts.get(REJECTED, 0), "postponed": counts.get(POSTPONED, 0),
            "blocked": counts.get(BLOCKED, 0), "carried": len(self.discoveries.eligible()),
            "resolved": resolved, "evidence": evidence, "objective": objective,
            "summary": summary, "stopped_because": cycle.last_stop_reason,
        }

    def _followup_runner(self, discovery, budget: FollowUpBudget, *,
                         task_id: str | None = None) -> dict[str, Any]:
        """Evaluate one deferred discovery against real state.

        This is deliberately conservative. The runner does not invent work to justify the
        item: it re-reads the evidence, asks whether the observation still holds, and
        either closes it with a concrete finding or says what is still unknown. Anything
        that needs a change to a protected component is recorded as needing a human, not
        attempted.
        """

        notes: list[str] = []
        evidence: list[str] = []
        status = READY
        disposition = "unchanged"

        # 1. Does the thing the observation refers to still exist as described?
        listed = dict(discovery.source_evidence or {})
        named = [str(item) for item in (listed.get("paths") or [])][:4]
        present = 0
        for raw in named:
            path = self._resolve_evidence_path(raw)
            if not path.exists():
                # Absent is reported as absent. Paths can have been extracted from prose,
                # so a miss is not evidence that the observation was wrong.
                evidence.append(f"absent: {path}")
                continue
            present += 1
            evidence.append(f"present: {path} ({self.guardian.digest(path)[:12]})")
        if named and not present:
            disposition = "stale"
            notes.append("none of the named files exist any more")
        for command in list(listed.get("commands") or [])[:2]:
            # Through the tool registry, never through the runner directly: a command
            # that came from an observation is checked by the same permission engine,
            # journalled in the same place and followed by the same integrity check as
            # any other execution. A follow-up path that could run an unchecked command
            # would be a way around the boundary the rest of the system enforces.
            outcome = self.tools.invoke(
                "run_command", {"command": str(command)},
                ToolContext(task_id=task_id, step=0, purpose=f"follow-up disc:{discovery.id}",
                            services=self.services()),
            )
            if not outcome.ok:
                evidence.append(f"`{command}` -> refused ({outcome.error})")
                notes.append(f"`{command}` could not be run: {outcome.error}")
                continue
            exit_code = (outcome.data or {}).get("exit_code")
            evidence.append(f"`{command}` -> exit {exit_code}")
            if exit_code not in (0, None):
                disposition = "confirmed"
                notes.append(f"the reported symptom reproduces: `{command}` exits "
                             f"{exit_code}")

        # 2. What is already known about it? A discovery that memory or a validated
        #    capability already answers is closed as answered, not re-investigated.
        for record in self.memory.search(discovery.title, limit=3):
            label = record.key or record.content[:60]
            notes.append(f"memory:{record.id} {label}")
        for hit in self.capabilities.search(discovery.title, limit=2):
            notes.append(f"capability {hit['name']} ({hit['status']}) may cover this")
            if hit["validated"]:
                disposition = "answered"
                evidence.append(f"capability {hit['name']} is validated ({hit['status']})")

        # 3. Say honestly what this evaluation could not settle.
        uncertainty = (discovery.uncertainty or "the observation is not yet reproduced").strip()
        uncertainty = uncertainty.rstrip(".") or "the observation is not yet reproduced"
        if disposition in ("stale", "answered"):
            status = REJECTED
            reason = (f"not followed up: {disposition}"
                      + (f" ({'; '.join(notes)})" if notes else ""))
        elif disposition == "confirmed":
            status = COMPLETED
            reason = (f"the observation was re-checked and holds: "
                      f"{'; '.join(notes) or 'the evidence still applies'}")
        else:
            status = POSTPONED
            reason = (
                f"not confirmed by this evaluation: {uncertainty}. Checked "
                f"{len(evidence)} piece(s) of evidence without confirming or refuting it. "
                f"The record is kept as postponed with what is now known attached; it is "
                f"not retried automatically, because a queue that reopens its own items "
                f"is a queue that never drains."
            )

        if discovery.touches_protected:
            status = POSTPONED
            reason = ("needs a human: the observation concerns a protected component "
                      "(governance or enforcement), which this cycle may not change")
            notes.append("protected component in scope")

        # 4. The cheap re-check could not settle it. Investigating properly is a task --
        #    same loop, same tools, same permissions, its own acceptance criterion -- so
        #    run one, inside this cycle's budget, and let its verified outcome decide.
        if disposition == "unchanged":
            return self._investigate(discovery, budget, evidence=evidence, notes=notes,
                                     task_id=task_id)

        # 5. Anything noticed *while evaluating this* is queued, not chased.
        self.store.append_event(
            "discovery_evaluated",
            {"id": discovery.id, "status": status, "disposition": disposition,
             "notes": notes, "budget": budget.as_dict()},
            task_id=task_id,
        )
        return {
            "status": status,
            "reason": reason[:1500],
            "evidence": "; ".join(evidence)[:1500],
            "spent": budget.as_dict(),
        }

    def _investigate(self, discovery, budget: FollowUpBudget, *, evidence: list[str],
                     notes: list[str], task_id: str | None) -> dict[str, Any]:
        """Run the follow-up as a real task and report what it actually established.

        The criterion is deliberately modest: the investigation must have *touched real
        state* (at least one tool call) for its conclusion to be recorded as a completed
        follow-up. The harness can verify that. It cannot verify that a sentence about
        the world is true, so the conclusion is stored as the task's own claim,
        labelled as such, next to the evidence the harness did verify.
        """

        objective = (
            f"Evaluate a deferred observation and settle it from real state.\n"
            f"Observation: {discovery.title}\n"
            f"What was seen: {discovery.observation}\n"
            f"Why it was deferred: {discovery.why_it_matters or 'not recorded'}\n"
            f"Unknown at the time: {discovery.uncertainty or 'not recorded'}\n"
            f"Do this by inspecting the real files and outputs involved. Then finish, "
            f"stating what you established and what remains unconfirmed. Do not change "
            f"anything: an observation that turns out to need a change is reported, not "
            f"implemented, because the task that noticed it has already closed."
        )
        outcome = self.run_task(
            objective,
            [{"kind": "event", "event_type": "tool_call"}],
            evaluate_discoveries=False,
            followup_depth=discovery.depth + 1,
            followup_parent=discovery.id,
        )
        used = self._tools_used(outcome.task_id)
        prompt_tokens, completion_tokens, reported = self._tokens_for(outcome.task_id)
        spent = {
            "model_calls": outcome.model_calls, "steps": outcome.steps,
            "tokens": (prompt_tokens or 0) + (completion_tokens or 0),
            "tokens_measured": bool(reported),
        }
        detail = "; ".join(outcome.criteria_results[i]["detail"]
                           for i in range(len(outcome.criteria_results)))[:400]
        evidence_text = (f"task {outcome.task_id}: tools executed {', '.join(used) or 'none'}; "
                         f"verification {outcome.verification.get('state')}; checks: {detail}"
                         )[:1500]
        if outcome.status == "complete" and outcome.verification.get("state") == "verified":
            return {
                "status": COMPLETED,
                "reason": (
                    f"investigated as task {outcome.task_id} and the harness verified the "
                    f"investigation touched real state. The conclusion below is the task's "
                    f"own claim, not a harness finding: "
                    f"{(outcome.final_summary or '').strip()[:700]}"
                ),
                "evidence": evidence_text,
                "spent": spent,
            }
        return {
            "status": POSTPONED,
            "reason": (
                f"the investigation ({outcome.task_id}) ended {outcome.status} with "
                f"verification '{outcome.verification.get('state')}'"
                + (f": {outcome.error}" if outcome.error else "")
                + (f"; not confirmed: {outcome.unverified}" if outcome.unverified else "")
            )[:1200],
            "evidence": evidence_text,
            "spent": spent,
        }

    def _tokens_for(self, task_id: str) -> tuple[int | None, int | None, int]:
        """Measured token usage for a task, or None when the provider reported none."""

        prompt: int | None = None
        completion: int | None = None
        reported = 0
        for row in self.store.events(task_id=task_id, event_type="model_reply", limit=500):
            try:
                usage = (json.loads(row["payload"]) or {}).get("usage") or {}
            except (TypeError, ValueError):
                continue
            in_tokens = usage.get("prompt_tokens")
            if in_tokens is None:
                in_tokens = usage.get("input_tokens")
            out_tokens = usage.get("completion_tokens")
            if out_tokens is None:
                out_tokens = usage.get("output_tokens")
            if in_tokens is None and out_tokens is None:
                continue
            reported += 1
            prompt = (prompt or 0) + int(in_tokens or 0)
            completion = (completion or 0) + int(out_tokens or 0)
        return prompt, completion, reported

    def _resolve_evidence_path(self, raw: str) -> pathlib.Path:
        """A path as an observation meant it, not as a string.

        A relative path written down by the model is relative to something the model
        cannot see: usually the run's workspace, occasionally the checkout. Both are
        tried, existing first, and when neither exists the workspace-relative reading is
        returned so the absence is reported for the place it most likely meant.
        """

        candidate = pathlib.Path(raw)
        if candidate.is_absolute():
            return candidate
        workspace = self.config.paths.workspace
        options = [(workspace / candidate).resolve(),
                   (_repo_root() / candidate).resolve()]
        # "workspace/inventory.txt" means the workspace's own inventory.txt, not a
        # directory called workspace inside it.
        first = candidate.parts[0] if candidate.parts else ""
        if first == workspace.name and len(candidate.parts) > 1:
            options.insert(0, (workspace / pathlib.Path(*candidate.parts[1:])).resolve())
        if first == _repo_root().name and len(candidate.parts) > 1:
            options.append((_repo_root() / pathlib.Path(*candidate.parts[1:])).resolve())
        for option in options:
            if option.exists():
                return option
        return (workspace / candidate).resolve()

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
            # The catalog reports where a tool came from and how far the lifecycle got:
            # a promoted capability is 'active' because its tests passed and a human
            # approved it, and an unvalidated one can never be loaded at all.
            # The registry's row may predate the interface columns; bring it up to date
            # from the module that is actually loaded, so selection describes real code.
            self.capabilities.sync_metadata(capability_name)
            record = self.capabilities.get(capability_name)
            decorated = dict(spec)
            decorated.setdefault("source", f"capability:{capability_name}")
            decorated.setdefault("validation",
                                 f"capability:{record.status}" if record else "capability:unknown")
            if record is not None and record.required_permissions:
                decorated.setdefault("permissions", list(record.required_permissions))
            self.tools.register_external(decorated, handler)
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

    @property
    def approvals(self) -> Any:
        """The one approval queue this run uses (owned by the tool registry)."""

        return self.tools.approvals

    def services(self) -> dict[str, Any]:
        """Live objects handed to tools. Promoted capabilities receive these."""

        return {
            "memory": self.memory,
            "store": self.store,
            "config": self.config,
            "guardian": self.guardian,
            "capabilities": self.capabilities,
            "tools": self.tools,
            "discoveries": self.discoveries,
            "followup_depth": getattr(self, "_followup_depth", 0),
            "followup_parent": getattr(self, "_followup_parent", None),
            "integrity": self.guardian.integrity_policy,
            "runner": self.runner,
            "verifier": self.verifier,
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
        evaluate_discoveries: bool = True,
        followup_depth: int = 0,
        followup_parent: int | None = None,
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
        # -- router state: all of it observed, none of it assumed -------------
        router = TaskRouter(max_retries_per_tool=self.config.limits.max_retries_per_tool)
        route_decisions: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        retries: dict[str, int] = {}
        denials_detail: list[dict[str, Any]] = []
        investigated: list[str] = []
        intents_inspected: list[str] = []
        asked_for: list[str] = []
        attempted: list[str] = []
        exposed_last: list[str] = []
        gap_evidence: dict[str, Any] | None = None
        gap_investigation: dict[str, Any] | None = None
        approvals_requested = 0
        text_only_streak = 0
        deferred_evaluated = not evaluate_discoveries
        deferred_note: dict[str, Any] | None = None
        #: Depth of the deferred-work chain this task belongs to. A task that is itself
        #: a follow-up records what it notices one level deeper, and the queue refuses
        #: to investigate past the configured depth -- which is what stops a chain of
        #: observations from becoming an unbounded recursion.
        self._followup_depth = followup_depth
        self._followup_parent = followup_parent
        criteria_satisfied = False
        self._criteria_cache: dict[str, tuple[list[Any], dict[str, Any]]] = {}
        # Steps that actually ran to a model call, which is what the task rows, the
        # context reports and TaskRun.steps all count. A terminal decision taken at the
        # top of a step ends the run without executing that step.
        steps_executed = 0

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
            while True:
                # --- stopping conditions: enforced here, not by the model -------
                if self.guardian.frozen:
                    status = "blocked"
                    error = f"guardian froze the run: {self.guardian.frozen_reason}"
                    observations.append(error)
                    break
                if time.monotonic() - started > self.config.limits.max_seconds:
                    status = "blocked"
                    error = (f"wall-clock limit of {self.config.limits.max_seconds:g}s reached; "
                             f"the task is unfinished")
                    observations.append(error)
                    break
                if step >= self.config.limits.max_steps and not finished:
                    status = "blocked"
                    error = (f"step limit of {self.config.limits.max_steps} reached without "
                             f"finish_task; the task is unfinished")
                    observations.append(error)
                    break

                step += 1
                task_row = self._task_row(task_id)
                pending = [
                    {"id": row["id"], "tool": row["tool"], "args": row["args"],
                     "created_at": row["created_at"]}
                    for row in self.store.pending_intents(task_id)
                ]

                # --- 1. the router decides what this step is -------------------
                candidates = self.capabilities.search(objective, limit=3)
                decision = router.route(StepContext(
                    task=task_row, step=step, max_steps=self.config.limits.max_steps,
                    observations=observations, pending_intents=len(pending),
                    pending_intent_ids=tuple(str(p["id"]) for p in pending),
                    inspected_intent_ids=tuple(intents_inspected),
                    denials=denials_detail, failures=failures, retries=retries,
                    repeats=repeat_counts, attempted_tools=tuple(attempted),
                    available_tools=self.tools.specs_names(), asked_for=tuple(asked_for),
                    capability_candidates=candidates, gap_evidence=gap_evidence,
                    finish_requested=finished, text_only_reply=text_only_streak > 0,
                    frozen=self.guardian.frozen, freeze_reason=self.guardian.frozen_reason,
                    max_retries_per_tool=self.config.limits.max_retries_per_tool,
                    investigated_tools=tuple(investigated),
                    ready_discoveries=(0 if deferred_evaluated
                                       else len(self.discoveries.eligible())),
                    followup_enabled=self.config.limits.followup_enabled,
                    followup_budget=self._budget().as_dict(),
                    criteria_satisfied=finished and criteria_satisfied,
                ))
                route_decisions.append({"step": step, **decision.as_dict()})
                self.store.append_event(
                    "route_decision",
                    {"step": step, "decision": decision.decision, "reason": decision.reason,
                     "evidence": decision.evidence, "tools": list(decision.tool_names)},
                    task_id=task_id,
                )

                # --- 2. decisions the harness carries out itself -----------------
                #  A terminal decision ends the task; a prologue decision does work
                #  before the model call this step still makes. Neither is allowed to
                #  skip the model indefinitely, so no decision can spin the loop.
                if decision.decision == PERSIST:
                    status = "blocked"
                    error = decision.reason
                    observations.append(f"router: {decision.reason}")
                    break

                if decision.decision == VERIFY:
                    observations.append(f"router: {decision.reason}")
                    status = self._status_from_finish(status, final_summary)
                    unresolved = [str(i) for i in
                                  (decision.evidence.get("unresolved_intents") or [])]
                    if unresolved and status == "complete":
                        # The model says the task is done; the harness knows an operation
                        # it recorded may never have happened. That is not "complete".
                        status = "partial"
                        unverified_note = (
                            f"unconfirmed side effects remain (intent ids "
                            f"{', '.join(unresolved)}); the task is closed as partial until "
                            f"their real outcome is established"
                        )
                        observations.append(unverified_note)
                    if status == "complete" and not criteria_satisfied:
                        # The claim is checked now, against the real world, rather than
                        # only after the loop: the answer decides both whether the task
                        # closes and whether deferred work may be evaluated, and neither
                        # decision can be made from a claim.
                        check = self._criteria_summary(criteria, task_id)
                        criteria_satisfied = check["state"] == "verified"
                        observations.append(
                            f"acceptance checks: {check['state']} "
                            f"({check.get('passed', 0)}/{check.get('total', 0)} criteria "
                            f"passed)"
                        )
                        if criteria_satisfied and self.discoveries.eligible():
                            # One more turn, for the router to say what happens to the
                            # queue. It is not model work: the router answers from state.
                            continue
                        if not criteria_satisfied:
                            status = "partial"
                            unverified_note = (
                                f"acceptance checks report '{check['state']}'; the model's "
                                f"completion claim alone does not close the task"
                            )
                    break

                if decision.decision == EVALUATE_DISCOVERIES:
                    # The task is verified and closed first; only then does the deferred
                    # work get evaluated, and only from here -- never from inside the loop
                    # that is trying to finish the task.
                    observations.append(f"router: {decision.reason}")
                    deferred_evaluated = True
                    deferred_note = self._run_followups(objective, task_id=task_id)
                    observations.append(
                        f"deferred work: {deferred_note['summary']} "
                        f"({deferred_note['stopped_because']})")
                    status = self._status_from_finish(status, final_summary)
                    break

                if decision.decision == REQUEST_AUTHORIZATION:
                    status = "awaiting_approval"
                    approval_id = self._last_approval_id(task_id) or "unknown"
                    error = (
                        f"{decision.reason}. A human must run `livium approvals --approve "
                        f"{approval_id} --by <your name>`; the same operation then proceeds "
                        f"exactly once."
                    )
                    observations.append(f"router: {decision.reason}")
                    self._note_route_action(task_id, step, decision, {"approval_id": approval_id})
                    break

                if decision.decision == INSPECT_RESULT:
                    report = self.recovery.inspect(task_id)
                    observations.append(f"recovery inspection: {report.next_action}")
                    findings = []
                    for finding in report.findings:
                        findings.append(f"intent {finding.intent_id} ({finding.tool}): "
                                        f"{finding.determination} — {finding.detail}")
                        # Remember that this one has been looked at, so an intent that
                        # cannot be resolved is not inspected forever.
                        if str(finding.intent_id) not in intents_inspected:
                            intents_inspected.append(str(finding.intent_id))
                    observations.extend(f"  {line}" for line in findings)
                    self._note_route_action(task_id, step, decision,
                                            {"findings": findings,
                                             "next_action": report.next_action})

                elif decision.decision == INVESTIGATE_GAP:
                    gap_investigation = self._investigate_gap(
                        task_id, objective, decision.evidence.get("gap") or {})
                    observations.append("reuse ladder: " + gap_investigation["summary"])
                    self._note_route_action(task_id, step, decision, {
                        "summary": gap_investigation["summary"],
                        "memory_id": gap_investigation["memory_id"]})

                elif decision.decision == RETRIEVE_CONTEXT:
                    widened = self._widen_retrieval(task_id, objective, observations)
                    observations.append(f"router: {decision.reason}")
                    if widened:
                        observations.append("additionally retrieved: " + "; ".join(widened))
                    self._note_route_action(task_id, step, decision,
                                            {"records": len(widened)})

                elif decision.decision == RETRY:
                    tool_name = str(decision.evidence.get("tool"))
                    retries[tool_name] = retries.get(tool_name, 0) + 1
                    observations.append(
                        f"router: retrying {tool_name} (attempt {retries[tool_name]} of "
                        f"{self.config.limits.max_retries_per_tool}) after "
                        f"{decision.evidence.get('classification')}")

                elif decision.decision == INVESTIGATE_FAILURE:
                    name = decision.evidence.get("tool")
                    if name and name not in investigated:
                        investigated.append(str(name))
                    observations.append(f"router: {decision.reason}")

                # --- 3. one model call, with the selected interfaces only -------
                built = self.context.build(
                    task=task_row, step=step, observations=observations,
                    criteria_results=criteria_results, pending_intents=pending,
                    route=decision.as_dict(),
                    exposed_tools=decision.tool_names,
                    tool_catalog=self.tools.catalog(),
                )
                context_reports.append(built.report)
                steps_executed = step
                if not conversation.messages:
                    conversation.system = built.system
                conversation.add_user(built.prompt)
                self._compact(conversation, built, observations)

                exposed_last = list(decision.tool_names)
                self._step_begin(task_id, step, "model_call",
                                 f"{decision.decision}: {decision.reason[:120]}")
                reply = self._call_model(conversation, built, observations,
                                         tool_names=exposed_last, task_id=task_id)
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
                    text_only_streak += 1
                    if text_only_streak >= 2:
                        status = "partial"
                        final_summary = reply.text or "(model returned no summary)"
                        self._step_end(task_id, step, "complete",
                                       "no tool call after a widened retrieval; loop ends")
                        break
                    nudged = True
                    self._step_end(task_id, step, "complete",
                                   "text-only reply; the router will widen retrieval")
                    continue

                text_only_streak = 0
                abort: str | None = None
                step_results: list[ToolResult] = []
                for call in reply.tool_calls:
                    if tool_call_count >= self.config.limits.max_tool_calls:
                        observations.append(
                            f"tool-call limit of {self.config.limits.max_tool_calls} reached"
                        )
                        break
                    if self.guardian.frozen:
                        break

                    if call.name not in exposed_last and call.name not in asked_for:
                        # The model asked for something the router did not put in front of
                        # it. The tool layer still authorises it, and the interface is
                        # carried on the next step; the fact is recorded, not hidden.
                        asked_for.append(call.name)
                        self.store.append_event(
                            "tool_used_without_interface",
                            {"tool": call.name, "exposed": exposed_last}, task_id=task_id,
                        )

                    signature = signature_of(call.name, call.arguments)
                    repeat_counts[signature] = repeat_counts.get(signature, 0) + 1
                    if repeat_counts[signature] >= 3:
                        abort = (
                            f"the same tool call was issued {repeat_counts[signature]} times "
                            f"with identical arguments ({call.name}); repeating it is not "
                            f"making progress"
                        )
                        self.store.append_event(
                            "loop_detected",
                            {"tool": call.name, "arguments_digest": _digest(signature),
                             "repeats": repeat_counts[signature]},
                            task_id=task_id,
                        )
                        break

                    result = self.tools.invoke(
                        call.name, call.arguments,
                        ToolContext(task_id=task_id, step=step,
                                    purpose=str(call.arguments.get("purpose", "")),
                                    services={**self.services(),
                                              "current_tool": call.name}),
                    )
                    tool_call_count += 1
                    attempted.append(call.name)
                    if result.denied:
                        denials += 1
                        denials_detail.append({
                            "tool": call.name,
                            "reason": str(result.data.get("reason") or result.output)[:300],
                            "approval_required": bool(result.data.get("approval_required")),
                        })
                        if result.data.get("approval_required"):
                            approvals_requested += 1
                    elif not result.ok:
                        classification = classify_failure(
                            call.name, result, known_tools=self.tools.specs_names())
                        failures.append({
                            "tool": call.name, "error": result.error,
                            "output": result.output[:400], "classification": classification,
                        })
                        # The failure is evidence, so it goes in the journal where the
                        # ledger and the viewer read it -- with a digest of the
                        # arguments rather than the arguments themselves.
                        self.store.append_event(
                            "tool_failure",
                            {"tool": call.name, "classification": classification,
                             "error": str(result.error or "")[:300],
                             "output": str(result.output or "")[:300],
                             "arguments_digest": _digest(signature),
                             "attempts": retries.get(call.name, 0)},
                            task_id=task_id,
                        )
                        last_error = result.error
                        if classification == "unknown_tool" or mentions_gap(result.output or ""):
                            gap_evidence = {
                                "tool": call.name, "classification": classification,
                                "detail": result.output[:300],
                            }
                    else:
                        # A success clears that tool's failure record: the router must not
                        # keep reacting to a problem that is over.
                        failures = [f for f in failures if f.get("tool") != call.name]
                        if call.name == "request_capability":
                            gap_evidence = {
                                "tool": call.name,
                                "proposal": result.data.get("name")
                                or call.arguments.get("name"),
                                "requirement": result.data.get("requirement")
                                or call.arguments.get("requirement"),
                                "detail": "the model proposed a capability for this requirement",
                            }

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
                    "failed" if abort else ("complete" if not self.guardian.frozen else "failed"),
                    abort or ("; ".join(r.tool + ("(denied)" if r.denied else "")
                                        for r in step_results) or "no tool executed"),
                )
                if abort:
                    status = "blocked"
                    error = abort
                    observations.append(abort)
                    break
                if on_step:
                    on_step({"step": step, "observations": observations[-3:],
                             "status": status if finished else "in_progress"})

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
        results = self._verify_criteria(criteria, task_id)
        criteria_results = [r.as_dict() for r in results]
        summary = summarize(results)
        criteria_satisfied = summary["state"] == "verified"

        if status == "complete" and summary["state"] != "verified":
            status = "partial"
            unverified_note = (
                unverified_note
                or f"acceptance checks report '{summary['state']}'; the model's completion claim "
                   f"alone does not close the task"
            ).strip()

        consequences = [c.as_dict() for c in self.guardian.consequences(task_id)]
        self._close_task(task_id, status, final_summary, summary, steps_executed, error)
        self._remember_outcome(task_id, objective, status, summary, observations, error,
                               denials=denials, resume_report=resume_report)

        run = TaskRun(
            task_id=task_id,
            objective=objective,
            status=status,
            steps=steps_executed,
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
            {"status": status, "steps": steps_executed, "model_calls": model_calls,
             "model_failures": model_failures,
             "tool_calls": tool_call_count, "denials": denials,
             "verification": summary, "error": error},
            task_id=task_id,
        )
        run.route_decisions = route_decisions
        run.approvals_requested = approvals_requested
        try:
            run.ledger_id = self._record_ledger(
                run=run, objective=objective, route_decisions=route_decisions,
                context_reports=context_reports, failures=failures, retries=retries,
                denials=denials, approvals_requested=approvals_requested, started=started,
            )
        except Exception as exc:  # measurement must never take the task down
            self.store.append_event(
                "ledger_write_failed",
                {"error": f"{type(exc).__name__}: {exc}"}, task_id=task_id,
            )
        return run

    # -- router support ----------------------------------------------------
    def _note_route_action(self, task_id: str, step: int, decision,
                           data: dict[str, Any]) -> None:
        """Journal work the harness did *inside* a step, before the model call.

        These actions do not consume a step: a step is one observation/decision/act
        cycle ending in a model call or a terminal transition, which keeps the stored
        step count comparable to the context reports and the task rows.
        """

        self.store.append_event(
            "route_action",
            {"step": step, "decision": decision.decision, "reason": decision.reason,
             "detail": data},
            task_id=task_id,
        )

    @staticmethod
    def _status_from_finish(status: str, final_summary: str) -> str:
        """The task status the harness will record, given the model's finish claim."""

        # finish_task always records an explicit status; a finish request reaching the
        # router any other way (a resumed task, a hand-edited row) is not evidence of
        # completion, so the run is closed as 'partial' rather than 'complete'.
        return "partial" if status == "in_progress" else status

    def _last_approval_id(self, task_id: str) -> int | None:
        rows = self.store.query(
            "SELECT id FROM approval WHERE task_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return int(rows[0]["id"]) if rows else None

    def _widen_retrieval(self, task_id: str, objective: str,
                         observations: Sequence[str]) -> list[str]:
        """Retrieve more when the last step produced nothing usable.

        Widening is bounded and named: the objective plus the most recent failure text
        are searched, and if that yields nothing new the most recent records are listed
        (the same question ``memory_recent`` answers). Whatever is added is reported so
        the next prompt carries it, and the fact that retrieval was widened is journalled
        rather than being an invisible second attempt.
        """

        terms = [objective]
        terms.extend(obs for obs in observations[-4:] if "failed" in obs.lower()
                     or "error" in obs.lower())
        query = " ".join(terms)
        added: list[str] = []
        for record in self.memory.search(query, limit=6):
            added.append(f"{record.kind}/{record.key or record.id}: {record.content[:160]}")
        if not added:
            for record in self.memory.recent(limit=5):
                added.append(f"{record.kind}/{record.key or record.id}: {record.content[:160]}")
        self.store.append_event(
            "retrieval_widened", {"query_chars": len(query), "records": len(added)},
            task_id=task_id,
        )
        return added

    def _investigate_gap(self, task_id: str, objective: str,
                         gap: dict[str, Any]) -> dict[str, Any]:
        """Run the reuse ladder for a suspected capability gap, as a harness action.

        The specification's ordering -- direct reuse, composition, extension, only then
        new code -- is executed here before the model is allowed to treat the gap as real.
        The ladder cannot prove that composition works; it names the candidates it found
        and records them, so the next action is informed and the decision is auditable.
        No capability is created by this method: proposing stays a model action and
        promotion stays a human one.
        """

        # Running the ladder twice for the same objective must not produce two memories:
        # the first answer stands until something about the situation changes.
        key = f"gap:{abs(hash(objective)) % 10**8}"
        existing = self.memory.by_key("decision", key)
        if existing is not None:
            return {"summary": existing.content, "rungs": {}, "memory_id": existing.id,
                    "reused": True}

        direct = self.capabilities.search(objective, limit=5)
        unvalidated = self.capabilities.search(objective, limit=5, include_unvalidated=True)
        tool_candidates = [name for name in
                           (str(c.get("name")) for c in direct)
                           if name in self.tools.specs_names()]
        reusable_tools = sorted(set(tool_candidates) | {
            name for name in self.tools.specs_names()
            if name.split("_")[0] in objective.lower()
        })
        rungs = {
            "direct_reuse": [c["name"] for c in direct],
            "validated_tools": reusable_tools,
            "composition_candidates": sorted(self.tools.specs_names()),
            "near_miss_capabilities": [
                {"name": c["name"], "status": c["status"], "matched": c["matched_terms"]}
                for c in unvalidated if not c["validated"]
            ],
            "gap_signal": gap,
        }
        if direct:
            summary = (
                f"{len(direct)} validated capability(ies) already match "
                f"({', '.join(c['name'] for c in direct)}); reuse before building"
            )
        elif rungs["near_miss_capabilities"]:
            summary = (
                "no validated capability matches; "
                f"{len(rungs['near_miss_capabilities'])} unfinished candidate(s) exist "
                "and could be extended rather than replaced"
            )
        else:
            summary = (
                "no registered capability matches, so the gap is real; the smallest "
                "adequate implementation is the next step, with tests, and a human "
                "promotes it"
            )

        record, _ = self.memory.remember(
            "decision",
            f"Capability gap ladder for '{objective[:90]}': {summary}",
            key=key,
            detail=json.dumps(rungs, default=str)[:1200],
            tags=["gap", "reuse-ladder"],
            confidence=0.7,
            verification="executed",
            evidence=f"reuse ladder executed for task {task_id}",
            source="harness:router",
            task_id=task_id,
        )
        self.store.append_event(
            "gap_investigated",
            {"task_id": task_id, "summary": summary, "rungs": rungs,
             "memory_id": getattr(record, "id", None)},
            task_id=task_id,
        )
        return {"summary": summary, "rungs": rungs,
                "memory_id": getattr(record, "id", None)}

    # -- ledger ------------------------------------------------------------
    def _record_ledger(self, *, run: "TaskRun", objective: str, route_decisions: list[dict[str, Any]],
                       context_reports: list[dict[str, Any]], failures: list[dict[str, Any]],
                       retries: dict[str, int], denials: int, approvals_requested: int,
                       started: float) -> int:
        """Store one measured row for this task. Actual and estimated stay separate."""

        replies = self.store.events(task_id=run.task_id, event_type="model_reply", limit=500)
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        reported = 0
        for row in replies:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            usage = payload.get("usage") or {}
            in_tokens = usage.get("prompt_tokens")
            if in_tokens is None:
                in_tokens = usage.get("input_tokens")
            out_tokens = usage.get("completion_tokens")
            if out_tokens is None:
                out_tokens = usage.get("output_tokens")
            if in_tokens is None and out_tokens is None:
                continue
            reported += 1
            prompt_tokens = (prompt_tokens or 0) + int(in_tokens or 0)
            completion_tokens = (completion_tokens or 0) + int(out_tokens or 0)

        estimated = sum(int(r.get("tokens_estimated") or 0) for r in context_reports) or None
        memory_ids: list[int] = []
        for report in context_reports:
            for record_id in report.get("memory_ids") or []:
                if record_id not in memory_ids:
                    memory_ids.append(int(record_id))

        injected = [
            {"id": row["id"], "kind": row["kind"], "key": row["key"],
             "content": row["content"], "tags": json.loads(row["tags"] or "[]")}
            for row in self.store.query(
                f"SELECT id, kind, key, content, tags FROM memory "
                f"WHERE id IN ({','.join('?' * len(memory_ids))})",
                tuple(memory_ids),
            )
        ] if memory_ids else []

        # What was reused is read from the journal of real tool calls, not from what
        # the model said it used.
        capability_tools = {
            entry["name"] for entry in self.tools.catalog()
            if str(entry.get("source", "")).startswith("capability:")
        }
        used_tools = self._tools_used(run.task_id)
        executed_capability_tools = sorted(capability_tools & set(used_tools))
        proposed = bool(self.store.query(
            "SELECT 1 FROM event WHERE task_id=? AND type='capability_proposed' LIMIT 1",
            (run.task_id,),
        ))
        influenced, experience_evidence = evidence_for_experience(
            injected=injected,
            tools_executed=used_tools,
            failed_tools=[str(f.get("tool")) for f in failures],
            route_decisions=route_decisions,
        )
        selected = sorted({
            str(name) for decision in route_decisions
            for name in (decision.get("evidence", {}).get("capability_candidates") or [])
        })
        captured, investigated = self._discovery_counts(run.task_id)
        reused = bool(executed_capability_tools)
        return Ledger(self.store).record(
            task_id=run.task_id,
            category=classify_task(used_tools, run.criteria),
            objective=objective,
            tools_exposed=len(context_reports[-1].get("exposed_tools") or [])
            if context_reports else 0,
            tools_available=len(self.tools.specs_names()),
            capabilities_selected=selected,
            memory_ids=memory_ids,
            capability_reused=reused,
            new_development=proposed,
            experience_influenced=influenced,
            experience_evidence=experience_evidence,
            model_calls=run.model_calls, model_failures=run.model_failures,
            prompt_tokens_actual=prompt_tokens, completion_tokens_actual=completion_tokens,
            usage_reported_calls=reported, tokens_estimated=estimated,
            tools_executed=run.tool_calls,
            retries=sum(retries.values()), denials=denials,
            approvals_requested=approvals_requested,
            failures=len(failures),
            route_decisions=[{"step": d["step"], "decision": d["decision"]} for d in route_decisions],
            verification_state=run.verification.get("state"), status=run.status,
            duration_ms=int((time.monotonic() - started) * 1000),
            discoveries_captured=captured, followups_investigated=investigated,
        )

    def _discovery_counts(self, task_id: str) -> tuple[int, int]:
        """How many observations this task captured, and how many were investigated.

        Read from the discovery records themselves. The model's own account of what it
        noticed is not a measurement, and the ledger exists to be measured.
        """

        rows = self.store.query("SELECT history FROM discovery WHERE source_task=?",
                                (task_id,))
        investigated = 0
        for row in rows:
            try:
                history = json.loads(row["history"] or "[]")
            except (TypeError, ValueError):
                continue
            if any(str(entry.get("event", "")).endswith("-> investigating")
                   for entry in history if isinstance(entry, dict)):
                investigated += 1
        return len(rows), investigated

    def _tools_used(self, task_id: str) -> list[str]:
        """Tool names this task actually invoked, from the journal (not from memory)."""

        rows = self.store.query(
            "SELECT payload FROM event WHERE task_id=? AND type='tool_call' ORDER BY id",
            (task_id,),
        )
        names: list[str] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            name = payload.get("tool")
            if name and name not in names:
                names.append(str(name))
        return names

    # -- model call -------------------------------------------------------
    def _call_model(
        self, conversation: Conversation, built, observations: list[str],
        tool_names: Sequence[str] | None = None, task_id: str = "",
    ) -> Any:
        """One real completion, with an honest response to context rejection."""

        tools = self.tools.specs_for(tool_names) if tool_names else self.tools.specs()
        if tool_names and not tools:  # every selected name was unknown: never send none
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
                self.store.append_event("model_unavailable", {"error": str(exc)},
                                        task_id=task_id or None)
                return None
            except ModelError as exc:
                observations.append(f"model error: {exc}")
                self.store.append_event("model_error", {"error": str(exc)},
                                        task_id=task_id or None)
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
                # The task id belongs on this row: the learning ledger reads usage from
                # here, and an unattributed token count cannot be attributed afterwards.
                task_id=task_id or None,
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

        # Which tools need this is read from the registry's own declared side effects
        # rather than a list kept here, so a tool that starts writing to disk is
        # covered the moment it says so. `run_command` counts regardless: it can run an
        # interpreter, which can write anything.
        meta = TOOL_META.get(tool) or {}
        if tool == "run_command" or meta.get("side_effects"):
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


def _digest(value: str) -> str:
    """A stable short digest, for journals that must not store what the model wrote."""

    import hashlib

    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


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
