"""The learning-efficiency ledger.

Every finished task writes one row recording what the run actually cost and what it
actually reused. The purpose is to be able to answer questions like "did the second
task of this kind need fewer model calls", "did we develop a capability while an
existing one already matched", or "how much context are we paying for" with
measurements instead of impressions.

Two rules govern everything here:

1. **Actual and estimated are different fields, and neither invents the other.**
   Provider-reported token counts are stored in ``*_actual``; if the provider
   reported nothing, those stay NULL rather than becoming a zero, and
   ``usage_reported_calls`` says how many calls were actually accounted for. The
   context manager's own budget arithmetic is stored in ``tokens_estimated`` and is
   never presented as a measurement.

2. **Reuse is only claimed with evidence.** ``experience_influenced`` is a
   *documented proxy*, not proof of causation: it is set only when the run injected
   specific records **and** a mechanical link exists between them and the run's
   behaviour (see :func:`evidence_for_experience`). The records and the decisions it
   is based on are stored alongside it in ``experience_evidence``, so a reader can
   check the claim rather than trust it.

The module also produces the investigations named in the specification (repeated
investigations, oversized contexts, unnecessary model calls, repeated retrieval of
irrelevant memories, excessive retries, development despite an existing solution).
Each reports counts and the tasks they came from; none of them asserts that a change
caused an improvement.
"""

from __future__ import annotations

import dataclasses
import json
import statistics
from typing import Any, Sequence

from .store import Store, now

#: Task categories are derived from the tools a run actually executed, not from the
#: objective's wording: the tools are an observation, the wording is an intention.
CATEGORY_BY_TOOL = (
    ("memory_write", "memory-maintenance"),
    ("memory_update", "memory-maintenance"),
    ("memory_forget", "memory-maintenance"),
    ("memory_search", "recall"),
    ("memory_recent", "recall"),
    ("request_capability", "capability-growth"),
    ("run_command", "command-execution"),
    ("write_file", "file-production"),
    ("read_file", "inspection"),
    ("list_dir", "inspection"),
    ("sha256", "inspection"),
)

DEFAULT_CATEGORY = "reasoning"


def classify(tools_used: Sequence[str], criteria: Sequence[dict[str, Any]] = ()) -> str:
    """A task's category, from the earliest tool in the precedence list it used."""

    used = set(tools_used)
    for tool, category in CATEGORY_BY_TOOL:
        if tool in used:
            return category
    kinds = {str(c.get("kind")) for c in criteria}
    if kinds & {"event"}:
        return "verification"
    return DEFAULT_CATEGORY


def evidence_for_experience(
    *,
    injected: Sequence[dict[str, Any]],
    tools_executed: Sequence[str],
    failed_tools: Sequence[str],
    route_decisions: Sequence[dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """A mechanical, checkable proxy for "previous experience changed this run".

    Two situations count, both of which need the record to have been *injected into
    the prompt* and a matching behavioural fact:

    *reuse* — a record of kind ``capability``, or tagged ``reuse:<tool>``, was injected
    and that same tool was then executed. The record named the tool; the run used it.

    *avoided repetition* — a record of kind ``failure`` naming a tool that failed in
    this run was injected **before** the step whose route decision for that tool was
    not another blind attempt (``change_approach``/``retry``/``investigate_failure``).

    Returns ``(influenced, evidence)``; the evidence is stored with the row so the
    claim can be audited. This is a proxy and is labelled as one wherever it surfaces.
    """

    evidence: dict[str, Any] = {"reuse": [], "avoided_repetition": []}
    executed = set(tools_executed)
    failed = sorted(set(failed_tools))
    decisions = [
        (str(d.get("decision")), json.dumps(d.get("evidence") or {}, default=str))
        for d in route_decisions
    ]

    for record in injected:
        kind = str(record.get("kind") or "")
        tags = [str(t) for t in (record.get("tags") or [])]
        content = str(record.get("content") or "")
        named = {t.split(":", 1)[1] for t in tags if t.startswith("reuse:")}
        if kind == "capability" and record.get("key"):
            named.add(str(record["key"]))

        matched = sorted(tool for tool in named if tool in executed)
        if matched:
            evidence["reuse"].append({
                "memory_id": record.get("id"), "kind": kind, "key": record.get("key"),
                "tools": matched,
                "why": "the record named a tool that this run then executed",
            })
            continue

        if kind != "failure":
            continue
        for tool in failed:
            if tool not in content:
                continue
            linked = [name for name, payload in decisions
                      if name in ("change_approach", "retry", "investigate_failure")
                      and tool in payload]
            if linked:
                evidence["avoided_repetition"].append({
                    "memory_id": record.get("id"), "tool": tool, "decisions": linked,
                    "why": "a recorded failure naming this tool was in context before the "
                           "run chose something other than another blind attempt",
                })

    influenced = bool(evidence["reuse"] or evidence["avoided_repetition"])
    return influenced, evidence


@dataclasses.dataclass
class LedgerRow:  # noqa: D101 - the fields are the row's columns, named as they are stored
    task_id: str
    category: str
    status: str
    verification_state: str
    model_calls: int
    tools_executed: int
    retries: int
    failures: int
    prompt_tokens_actual: int | None
    completion_tokens_actual: int | None
    usage_reported_calls: int
    tokens_estimated: int | None
    capability_reused: bool
    new_development: bool
    experience_influenced: bool
    created_at: str
    approvals_requested: int = 0

    @property
    def total_tokens_actual(self) -> int | None:
        if self.prompt_tokens_actual is None and self.completion_tokens_actual is None:
            return None
        return (self.prompt_tokens_actual or 0) + (self.completion_tokens_actual or 0)

    def render(self) -> str:
        actual = (
            f"{self.total_tokens_actual} reported across {self.usage_reported_calls}/"
            f"{self.model_calls} call(s)"
            if self.total_tokens_actual is not None else
            f"not reported by the provider ({self.usage_reported_calls}/{self.model_calls} "
            "calls accounted for)"
        )
        return (f"{self.task_id} [{self.category}/{self.status}/{self.verification_state}] "
                f"model_calls={self.model_calls} tools={self.tools_executed} "
                f"retries={self.retries} failures={self.failures} "
                f"tokens_actual={actual} tokens_estimated={self.tokens_estimated} "
                f"reused={self.capability_reused} new={self.new_development} "
                f"experience={self.experience_influenced}")


class Ledger:
    def __init__(self, store: Store):
        self.store = store

    # -- writing ----------------------------------------------------------
    def record(
        self,
        *,
        task_id: str,
        category: str,
        objective: str,
        tools_exposed: int,
        tools_available: int,
        capabilities_selected: Sequence[str] = (),
        memory_ids: Sequence[int] = (),
        capability_reused: bool = False,
        new_development: bool = False,
        experience_influenced: bool = False,
        experience_evidence: dict[str, Any] | None = None,
        model_calls: int = 0,
        model_failures: int = 0,
        prompt_tokens_actual: int | None = None,
        completion_tokens_actual: int | None = None,
        usage_reported_calls: int = 0,
        tokens_estimated: int | None = None,
        tools_executed: int = 0,
        retries: int = 0,
        denials: int = 0,
        approvals_requested: int = 0,
        failures: int = 0,
        route_decisions: Sequence[dict[str, Any]] = (),
        verification_state: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        #: Deferred work, measured rather than described: how many observations this
        #: task captured for later, how many follow-up investigations reached a
        #: conclusion for it, and how many were stopped by the budget and left ready.
        #: All three are read back from the discovery records, never from the model's
        #: account of what it did. The distinction between the last two matters: an
        #: investigation the budget stopped did not settle anything, and the record it
        #: belongs to is still waiting for a later cycle.
        discoveries_captured: int = 0,
        followups_investigated: int = 0,
        followups_stopped: int = 0,
    ) -> int:
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO ledger(task_id, category, objective_chars, tools_exposed, "
                "tools_available, capabilities_selected, memory_ids, capability_reused, "
                "new_development, experience_influenced, experience_evidence, model_calls, "
                "model_failures, prompt_tokens_actual, completion_tokens_actual, "
                "usage_reported_calls, tokens_estimated, tools_executed, retries, denials, "
                "approvals_requested, failures, route_decisions, verification_state, status, "
                "duration_ms, discoveries_captured, followups_investigated, "
                "followups_stopped, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id, category, len(objective), tools_exposed, tools_available,
                    json.dumps(list(capabilities_selected)), json.dumps(list(memory_ids)),
                    int(bool(capability_reused)), int(bool(new_development)),
                    int(bool(experience_influenced)),
                    json.dumps(experience_evidence or {}),
                    model_calls, model_failures, prompt_tokens_actual,
                    completion_tokens_actual, usage_reported_calls, tokens_estimated,
                    tools_executed, retries, denials, approvals_requested, failures,
                    json.dumps(list(route_decisions), default=str),
                    verification_state, status, duration_ms, discoveries_captured,
                    followups_investigated, followups_stopped, now(),
                ),
            )
            return int(cur.lastrowid or 0)

    # -- reading ----------------------------------------------------------
    def rows(self, limit: int = 100, *, category: str | None = None) -> list[LedgerRow]:
        sql = "SELECT * FROM ledger"
        params: list[Any] = []
        if category:
            sql += " WHERE category=?"
            params.append(category)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [self._row(r) for r in self.store.query(sql, params)]

    @staticmethod
    def _row(raw: Any) -> LedgerRow:
        return LedgerRow(
            task_id=raw["task_id"], category=raw["category"] or DEFAULT_CATEGORY,
            status=raw["status"] or "unknown",
            verification_state=raw["verification_state"] or "unverified",
            model_calls=raw["model_calls"], tools_executed=raw["tools_executed"],
            retries=raw["retries"], failures=raw["failures"],
            approvals_requested=raw["approvals_requested"],
            prompt_tokens_actual=raw["prompt_tokens_actual"],
            completion_tokens_actual=raw["completion_tokens_actual"],
            usage_reported_calls=raw["usage_reported_calls"],
            tokens_estimated=raw["tokens_estimated"],
            capability_reused=bool(raw["capability_reused"]),
            new_development=bool(raw["new_development"]),
            experience_influenced=bool(raw["experience_influenced"]),
            created_at=raw["created_at"],
        )

    def summary(self) -> dict[str, Any]:
        """Totals and medians per category, with actual and estimated kept apart."""

        rows = [r for r in (dict(row) for row in self.store.query("SELECT * FROM ledger"))]
        if not rows:
            return {"tasks": 0, "categories": {}, "note": "no task has been recorded yet"}
        categories: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = categories.setdefault(row["category"] or DEFAULT_CATEGORY,
                                           {"tasks": 0, "model_calls": [], "tools": [],
                                            "actual_tokens": 0, "actual_tokens_covered": 0,
                                            "estimated_tokens": 0, "reused": 0,
                                            "new_development": 0, "experience": 0,
                                            "retries": 0, "failures": 0, "verified": 0})
            bucket["tasks"] += 1
            bucket["model_calls"].append(row["model_calls"])
            bucket["tools"].append(row["tools_executed"])
            if row["prompt_tokens_actual"] is not None or row["completion_tokens_actual"] is not None:
                bucket["actual_tokens"] += (row["prompt_tokens_actual"] or 0) + \
                                           (row["completion_tokens_actual"] or 0)
                bucket["actual_tokens_covered"] += 1
            bucket["estimated_tokens"] += row["tokens_estimated"] or 0
            bucket["reused"] += int(bool(row["capability_reused"]))
            bucket["new_development"] += int(bool(row["new_development"]))
            bucket["experience"] += int(bool(row["experience_influenced"]))
            bucket["retries"] += row["retries"]
            bucket["failures"] += row["failures"]
            bucket["verified"] += int(row["verification_state"] == "verified")
        for bucket in categories.values():
            bucket["median_model_calls"] = statistics.median(bucket["model_calls"])
            del bucket["model_calls"]
            del bucket["tools"]
        return {
            "tasks": len(rows),
            "categories": categories,
            "note": ("actual token totals cover only the tasks where the provider reported "
                     "usage; estimated totals are the context manager's own arithmetic and are "
                     "not measurements"),
        }

    # -- investigations ----------------------------------------------------
    def investigations(self) -> list[dict[str, Any]]:
        """The questions the specification asks this data to answer.

        Each returns the counts and the task ids it is based on, so a reader can go and
        look. None of them claims that a change caused an improvement: with one task
        per category, or with tasks of differing difficulty, that would not be
        supportable.
        """

        rows = [dict(r) for r in self.store.query("SELECT * FROM ledger ORDER BY id")]
        findings: list[dict[str, Any]] = []

        def add(key: str, title: str, count: int, tasks: list[str], detail: str) -> None:
            findings.append({"key": key, "title": title, "count": count,
                             "task_ids": tasks, "detail": detail})

        repeated = [r for r in rows if r["failures"] >= 2]
        add("repeated_investigations",
            "Tasks with two or more failed operations",
            len(repeated), [r["task_id"] for r in repeated],
            "A high count for one category is where a reusable capability is worth "
            "considering; one occurrence is not evidence of a pattern.")

        oversized = [r for r in rows
                     if (r["tokens_estimated"] or 0) > 0 and r["model_calls"] > 0
                     and (r["tokens_estimated"] or 0) / max(1, r["model_calls"]) > 20000]
        add("oversized_context",
            "Tasks averaging more than 20k estimated prompt tokens per model call",
            len(oversized), [r["task_id"] for r in oversized],
            "Estimated, not measured: the context manager's own arithmetic.")

        many_calls = [r for r in rows if r["model_calls"] >= 5 and r["tools_executed"] <= r["model_calls"] // 2]
        add("unnecessary_model_calls",
            "Tasks where most model calls produced no tool execution",
            len(many_calls), [r["task_id"] for r in many_calls],
            "Text-only replies, retries after provider errors and refusals all land here.")

        no_retrieval = [r for r in rows if json.loads(r["memory_ids"] or "[]") == []
                        and r["category"] in ("inspection", "file-production", "recall")]
        add("no_memory_retrieved",
            "Tasks in a memory-relevant category that injected no memory records",
            len(no_retrieval), [r["task_id"] for r in no_retrieval],
            "Either nothing relevant was stored, or retrieval is too narrow. The "
            "context report for each task says which records were considered.")

        retry_heavy = [r for r in rows if r["retries"] >= 3]
        add("excessive_retries", "Tasks with three or more retries",
            len(retry_heavy), [r["task_id"] for r in retry_heavy],
            "Retries are bounded per task by configuration; this counts the ones used.")

        developed_anyway = [r for r in rows if r["new_development"] and r["capability_reused"]]
        add("development_despite_existing",
            "Tasks that both reused a capability and started new development",
            len(developed_anyway), [r["task_id"] for r in developed_anyway],
            "Worth reading: the router is supposed to exhaust reuse before development.")

        return findings
