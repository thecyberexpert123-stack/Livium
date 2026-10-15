"""Task routing: what happens on the next step, decided from evidence.

Before this module existed, the control flow of a task was whatever tool call the
model happened to emit next. That made the model the router, the retry policy and the
stopping logic at once, which is exactly what the specification says it must not be.
The router is a pure, deterministic function of the task's real state; the model still
chooses *which tool and arguments*, but only inside the decision the router made, and
the router can override it when the evidence warrants.

The decision set (each one is an observable, journalled fact):

``retrieve_context``     widen retrieval — the last reply made no progress
``select_capability``    a registry candidate fits; expose its interface
``execute``              the ordinary path: model picks a tool from the exposed set
``inspect_result``       unresolved side effects must be examined before anything else
``investigate_failure``  a tool has failed too often to keep retrying
``retry``                a recoverable failure, with the attempt count recorded
``change_approach``      the current approach is provably not working (denied, repeat)
``investigate_gap``      run the reuse ladder before anyone proposes new code
``request_authorization``a human must approve this operation; the run pauses
``verify``               run the acceptance checks now (harness-owned)
``persist``              stop and record the state honestly

Rules are ordered and each one carries the evidence that selected it, so a reader can
disagree with the reasoning instead of guessing at it. Nothing here grants authority:
permissions, verification and promotion stay where they were, and a model cannot
route around them because the router only narrows what is offered.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Iterable, Sequence

RETRIEVE_CONTEXT = "retrieve_context"
SELECT_CAPABILITY = "select_capability"
EXECUTE = "execute"
INSPECT_RESULT = "inspect_result"
INVESTIGATE_FAILURE = "investigate_failure"
RETRY = "retry"
CHANGE_APPROACH = "change_approach"
INVESTIGATE_GAP = "investigate_gap"
REQUEST_AUTHORIZATION = "request_authorization"
VERIFY = "verify"
PERSIST = "persist"

DECISIONS = (RETRIEVE_CONTEXT, SELECT_CAPABILITY, EXECUTE, INSPECT_RESULT,
             INVESTIGATE_FAILURE, RETRY, CHANGE_APPROACH, INVESTIGATE_GAP,
             REQUEST_AUTHORIZATION, VERIFY, PERSIST)

#: Failure classes, from the tool layer's own signals rather than from prose.
PERMISSION = "permission"
INVALID_INPUT = "invalid_input"
ENVIRONMENT = "environment"
TRANSIENT = "transient"
INVALID_STATE = "invalid_state"
UNKNOWN_TOOL = "unknown_tool"
COMMAND_FAILED = "command_failed"
UNDETERMINED = "undetermined"

#: Which classes are worth another attempt with the same tool. Deliberately short:
#: a repeat that cannot plausibly differ is not a retry, it is noise.
RETRYABLE = (TRANSIENT, ENVIRONMENT)

#: Words that suggest a capability the runtime may not have.
GAP_SIGNALS = ("unknown tool", "not implemented", "no such tool", "unsupported",
               "not available", "cannot be done", "no tool")

#: Objective keywords -> tools worth exposing. Deliberately small and literal: it is
#: a starting set, not a classifier pretending to understand language. Anything the
#: run already used, or asked for by name, is added on top.
TOOL_KEYWORDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("read", "inspect", "check", "look", "cat", "content", "review"),
     ("read_file", "list_dir", "sha256")),
    (("list", "find", "locate", "which", "directory", "folder"), ("list_dir", "read_file")),
    (("write", "create", "save", "produce", "draft", "notes", "file"), ("write_file", "read_file")),
    (("run", "execute", "command", "test", "build", "install", "script", "shell"),
     ("run_command",)),
    (("remember", "recall", "memory", "learn", "lesson", "previously", "before"),
     ("memory_search", "memory_write", "memory_update", "memory_recent")),
    (("forget", "retire", "remove the record"), ("memory_forget", "memory_search")),
    (("capability", "tool", "skill", "missing", "gap"), ("request_capability", "tool_info")),
    (("hash", "digest", "checksum", "unchanged"), ("sha256",)),
)

#: Meta tools that are always available to the model. They are cheap, they never
#: perform a side effect, and without them the model could not ask for anything that
#: was not predicted here.
META_TOOLS = ("finish_task", "request_capability", "tool_info")

MAX_EXPOSED_TOOLS = 7


@dataclasses.dataclass
class StepContext:
    """Everything the router is allowed to consider. All of it is observed state."""

    task: dict[str, Any]
    step: int
    max_steps: int
    observations: Sequence[str] = ()
    last_results: Sequence[Any] = ()
    pending_intents: int = 0
    pending_intent_ids: Sequence[str] = ()
    inspected_intent_ids: Sequence[str] = ()
    denials: Sequence[dict[str, Any]] = ()
    failures: Sequence[dict[str, Any]] = ()
    retries: dict[str, int] = dataclasses.field(default_factory=dict)
    repeats: dict[str, int] = dataclasses.field(default_factory=dict)
    attempted_tools: Sequence[str] = ()
    available_tools: Sequence[str] = ()
    asked_for: Sequence[str] = ()
    capability_candidates: Sequence[dict[str, Any]] = ()
    gap_evidence: dict[str, Any] | None = None
    finish_requested: bool = False
    text_only_reply: bool = False
    frozen: bool = False
    freeze_reason: str | None = None
    max_retries_per_tool: int = 2
    investigated_tools: Sequence[str] = ()


@dataclasses.dataclass
class RouteDecision:
    decision: str
    reason: str
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    tool_names: tuple[str, ...] = ()
    #: False when the work of this decision is done by the harness rather than the
    #: model (a verification, a persistence step, an approval wait). The agent still
    #: shows the model the result where a next action is possible; the flag says who
    #: *performed* the decision, not that the model is skipped.
    requires_model: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision, "reason": self.reason,
            "evidence": self.evidence, "tool_names": list(self.tool_names),
            "requires_model": self.requires_model,
        }


# ---------------------------------------------------------------- failure classes
def classify_failure(tool: str, result: Any, *, known_tools: Iterable[str] = ()) -> str:
    """Classify a failed tool call from the tool layer's own signals.

    The point is to stop a failure from being treated as a single undifferentiated
    event: a permission refusal, a bad argument, a missing binary and a genuine
    capability gap require four different responses, and only one of them justifies
    writing new code.
    """

    error = str(getattr(result, "error", "") or "")
    output = str(getattr(result, "output", "") or "")
    data = getattr(result, "data", None) or {}
    lowered = f"{error} {output}".lower()

    if getattr(result, "denied", False):
        return PERMISSION
    if error == "unknown_tool" or "unknown tool" in lowered:
        if known_tools and tool in set(known_tools):
            return UNKNOWN_TOOL  # the name exists; the request was malformed
        return UNKNOWN_TOOL
    if error == "bad_arguments" or "must be a" in lowered or "required" in lowered:
        return INVALID_INPUT
    if "cannot create working directory" in lowered:
        return ENVIRONMENT
    if "executable not found" in lowered or "no such file or directory" in lowered:
        return ENVIRONMENT
    if isinstance(data, dict) and data.get("timed_out"):
        return TRANSIENT
    if isinstance(data, dict) and data.get("exit_code") not in (None, 0):
        return COMMAND_FAILED
    if error in ("not_found", "missing"):
        return INVALID_STATE
    if error in ("unavailable", "timeout"):
        return TRANSIENT
    if any(signal in lowered for signal in GAP_SIGNALS):
        return UNKNOWN_TOOL
    return UNDETERMINED


def failure_is_retryable(classification: str) -> bool:
    return classification in RETRYABLE


def mentions_gap(text: str) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in GAP_SIGNALS)


# ------------------------------------------------------------------ tool selection
def select_tools(objective: str, *, available: Sequence[str],
                 candidates: Sequence[str] = (), used: Sequence[str] = (),
                 asked_for: Sequence[str] = (), forced: Sequence[str] = (),
                 limit: int = MAX_EXPOSED_TOOLS) -> tuple[str, ...]:
    """Choose which tool interfaces this step is worth carrying.

    Keyword matching over the objective plus continuity (what this run already used,
    what it asked for by name, what the registry matched). The always-on meta tools
    keep the model able to request anything that was not predicted. Returns names in
    the order they were offered, deduplicated, and never more than ``limit``.
    """

    available_set = set(available)
    chosen: list[str] = []

    def add(name: str) -> None:
        if name and name in available_set and name not in chosen:
            chosen.append(name)

    for name in forced:
        add(name)
    for name in asked_for:
        add(name)
    for name in used:
        add(name)

    words = set(re.findall(r"[a-z_]+", objective.lower()))
    for keywords, tools in TOOL_KEYWORDS:
        if words & set(keywords):
            for tool in tools:
                add(tool)

    for name in candidates:
        add(name)
    for name in META_TOOLS:
        add(name)

    if len(chosen) > limit:
        # Keep the meta tools (the model must be able to finish and to ask) and the
        # most specific choices made above, dropping the tail.
        meta = [name for name in chosen if name in META_TOOLS]
        rest = [name for name in chosen if name not in META_TOOLS]
        chosen = rest[: max(0, limit - len(meta))] + meta
    return tuple(chosen)


# ------------------------------------------------------------------------ router
class TaskRouter:
    """Deterministic routing over the state of one task run."""

    def __init__(self, *, max_retries_per_tool: int = 2):
        self.max_retries_per_tool = max_retries_per_tool

    def route(self, ctx: StepContext) -> RouteDecision:
        #  The order is the safety property: a frozen guardian stops everything, an
        #  unexplained side effect is established before the task can be closed, and
        #  only then does the harness consider the model's completion claim.
        decisions = (
            self._frozen, self._unresolved_intents, self._finish, self._denied,
            self._failed, self._repeat, self._no_progress, self._gap, self._execute,
        )
        for rule in decisions:
            decision = rule(ctx)
            if decision is not None:
                return decision
        raise AssertionError("the execute rule must always return a decision")  # pragma: no cover

    # -- rules, in order ---------------------------------------------------
    def _frozen(self, ctx: StepContext) -> RouteDecision | None:
        if not ctx.frozen:
            return None
        return RouteDecision(
            PERSIST, f"the growth guardian is frozen: {ctx.freeze_reason or 'no reason recorded'}",
            {"frozen": True, "reason": ctx.freeze_reason}, requires_model=False,
        )

    def _finish(self, ctx: StepContext) -> RouteDecision | None:
        if not ctx.finish_requested:
            return None
        return RouteDecision(
            VERIFY, "the model asked to finish; the harness decides whether the task is done",
            {"step": ctx.step, "observations": len(ctx.observations),
             # Already-inspected intents are still not evidence of anything: the task is
             # closed with them named rather than silently treated as resolved.
             "unresolved_intents": list(ctx.pending_intent_ids)},
            requires_model=False,
        )

    def _unresolved_intents(self, ctx: StepContext) -> RouteDecision | None:
        if not ctx.pending_intents:
            return None
        uninspected = [i for i in ctx.pending_intent_ids
                       if str(i) not in {str(x) for x in ctx.inspected_intent_ids}]
        if not uninspected and ctx.pending_intent_ids:
            # Every outstanding intent has already been looked at and is still unresolved:
            # inspecting again would repeat the same question, so the run may proceed --
            # and the closure will name the uncertainty rather than bury it.
            return None
        return RouteDecision(
            INSPECT_RESULT,
            f"{ctx.pending_intents} side effect(s) were recorded but never confirmed; their "
            "real outcome must be established before anything else happens",
            {"pending_intents": ctx.pending_intents,
             "intent_ids": list(ctx.pending_intent_ids)}, requires_model=False,
        )

    def _denied(self, ctx: StepContext) -> RouteDecision | None:
        denial = ctx.denials[-1] if ctx.denials else None
        if not denial:
            return None
        tool = str(denial.get("tool"))
        if denial.get("approval_required"):
            return RouteDecision(
                REQUEST_AUTHORIZATION,
                f"{tool} requires human approval before it can run "
                f"({denial.get('reason', 'the permission engine returned approval_required')})",
                {"tool": tool, "reason": denial.get("reason"),
                 "arguments": denial.get("arguments")}, tool_names=(tool,),
                requires_model=False,
            )
        return RouteDecision(
            CHANGE_APPROACH,
            f"{tool} was refused by the permission engine ({denial.get('reason')}); repeating "
            "the same request cannot succeed and the refusal is not negotiable from inside",
            {"tool": tool, "reason": denial.get("reason"), "arguments": denial.get("arguments"),
             "denied_count": len(ctx.denials)},
            tool_names=select_tools(
                str(ctx.task.get("objective", "")), available=ctx.available_tools,
                used=tuple(ctx.attempted_tools), forced=("tool_info",)),
        )

    def _failed(self, ctx: StepContext) -> RouteDecision | None:
        failure = ctx.failures[-1] if ctx.failures else None
        if not failure:
            return None
        tool = str(failure.get("tool"))
        classification = str(failure.get("classification") or UNDETERMINED)
        attempts = int(ctx.retries.get(tool, 0))

        if failure_is_retryable(classification) and attempts < self.max_retries_per_tool:
            return RouteDecision(
                RETRY,
                f"{tool} failed as {classification}, which is the kind of failure that can "
                f"differ on a second attempt; attempt {attempts + 1} of {self.max_retries_per_tool}",
                {"tool": tool, "classification": classification, "attempt": attempts + 1,
                 "limit": self.max_retries_per_tool, "error": failure.get("error")},
                tool_names=(tool,),
            )
        if tool in ctx.investigated_tools:
            return RouteDecision(
                CHANGE_APPROACH,
                f"{tool} has already been investigated once this run ({classification}); the "
                "approach has to change rather than the attempt repeat",
                {"tool": tool, "classification": classification, "attempts": attempts},
                tool_names=select_tools(
                    str(ctx.task.get("objective", "")), available=ctx.available_tools,
                    used=tuple(ctx.attempted_tools), forced=("tool_info",)),
            )
        return RouteDecision(
            INVESTIGATE_FAILURE,
            f"{tool} failed as {classification} and retrying it "
            f"{'is exhausted' if failure_is_retryable(classification) else 'would not help'}; "
            "the failure itself has to be examined before the next action",
            {"tool": tool, "classification": classification, "attempts": attempts,
             "error": failure.get("error"), "output": str(failure.get("output", ""))[:400]},
            tool_names=select_tools(
                str(ctx.task.get("objective", "")), available=ctx.available_tools,
                used=tuple(ctx.attempted_tools), forced=(tool, "read_file", "list_dir", "tool_info")),
        )

    def _repeat(self, ctx: StepContext) -> RouteDecision | None:
        repeats = {sig: count for sig, count in ctx.repeats.items() if count >= 2}
        if not repeats:
            return None
        signature, count = max(repeats.items(), key=lambda item: item[1])
        tool = signature.split(":", 1)[0]
        return RouteDecision(
            CHANGE_APPROACH,
            f"the identical call ({tool} with the same arguments) has now been issued {count} "
            "times; the result cannot differ and the approach must change",
            {"signature": signature, "count": count, "tool": tool},
            tool_names=select_tools(
                str(ctx.task.get("objective", "")), available=ctx.available_tools,
                used=tuple(ctx.attempted_tools), forced=("tool_info",)),
        )

    def _no_progress(self, ctx: StepContext) -> RouteDecision | None:
        if not ctx.text_only_reply:
            return None
        return RouteDecision(
            RETRIEVE_CONTEXT,
            "the last reply contained no tool call, so no observation was produced; widen "
            "retrieval before asking again",
            {"step": ctx.step, "observation_count": len(ctx.observations)},
            tool_names=select_tools(
                str(ctx.task.get("objective", "")), available=ctx.available_tools,
                used=tuple(ctx.attempted_tools)),
        )

    def _gap(self, ctx: StepContext) -> RouteDecision | None:
        """A missing capability is only pursued after the reuse ladder has run."""

        evidence = ctx.gap_evidence
        if not evidence:
            return None
        if ctx.capability_candidates:
            return RouteDecision(
                SELECT_CAPABILITY,
                "an existing capability matches the objective better than developing one",
                {"candidates": [c.get("name") for c in ctx.capability_candidates],
                 "gap": evidence},
                tool_names=tuple(
                    str(tool) for c in ctx.capability_candidates
                    for tool in (c.get("tools") or [])),
            )
        return RouteDecision(
            INVESTIGATE_GAP,
            "no available tool or registered capability covers this requirement, so the reuse "
            "ladder runs before any new code is written",
            {"gap": evidence, "attempted": list(ctx.attempted_tools),
             "required_tool": evidence.get("tool")},
            # The ladder itself is harness work, but the model still needs the tools that
            # let it act on the result: propose the smallest capability, inspect one, or
            # close the task honestly.
            tool_names=select_tools(
                str(ctx.task.get("objective", "")), available=ctx.available_tools,
                used=tuple(ctx.attempted_tools),
                forced=("request_capability", "tool_info", "finish_task", "tool_info")),
            requires_model=True,
        )

    def _execute(self, ctx: StepContext) -> RouteDecision:
        forced: tuple[str, ...] = ()
        if ctx.capability_candidates:
            forced = tuple(
                str(tool) for c in ctx.capability_candidates for tool in (c.get("tools") or []))
        tools = select_tools(
            str(ctx.task.get("objective", "")), available=ctx.available_tools,
            candidates=forced, used=tuple(ctx.attempted_tools),
            asked_for=tuple(ctx.asked_for),
        )
        evidence = {
            "step": ctx.step,
            "remaining_steps": max(0, ctx.max_steps - ctx.step),
            "exposed": list(tools),
            "available": len(ctx.available_tools),
            "capability_candidates": [c.get("name") for c in ctx.capability_candidates],
        }
        reason = f"progress continues; {len(tools)} of {len(ctx.available_tools)} tool " \
                 "interfaces are exposed for this step"
        if ctx.step >= ctx.max_steps - 1:
            evidence["wrap_up"] = True
            reason += "; this is the last step in the budget, so the task should be closed " \
                      "honestly rather than left mid-flight"
        if ctx.capability_candidates:
            reason += "; a registry candidate is exposed by name"
        return RouteDecision(EXECUTE, reason, evidence, tool_names=tools)


def signature_of(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}:{json.dumps(arguments, sort_keys=True, default=str)}"
