"""Deferred discoveries: opportunities noticed during a task, kept for later.

A task often produces observations that are not that task: a reusable capability
that would simplify future work, a bottleneck unrelated to the defect being fixed,
an undocumented dependency, a validation weakness. Investigating them the moment
they appear is how an agent loses the task it was given; ignoring them is how it
loses the opportunity. This module is the third option: record it, keep working,
and decide after the primary task has been verified.

Three properties are enforced here rather than assumed:

**The record is data, not instruction.** Text that arrives from an inspected file,
log or tool result is stored with ``provenance = "observed_content"`` and scanned
for imperative phrasing, which is recorded as a flag on the record. Nothing in this
module executes anything a discovery says: a discovery is an observation to be
evaluated against the task's objective, the permission engine and the guardian,
exactly like any other request that reaches the agent.

**Transitions are checked.** The lifecycle is a small state machine, and a move that
is not in ``TRANSITIONS`` raises. A record cannot be quietly marked complete, and a
finished discovery cannot be silently reopened as pending.

**Nothing here grants authority.** A queued discovery can recommend a change to
Livium itself; it cannot authorise one. Permissions, verification and the guardian
gate are unchanged, and the discovery queue is not a route around any of them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Sequence

from .store import Store, now

# -- lifecycle -------------------------------------------------------------

PENDING = "pending"
READY = "ready"
INVESTIGATING = "investigating"
BLOCKED = "blocked"
COMPLETED = "completed"
REJECTED = "rejected"
POSTPONED = "postponed"

STATUSES = (PENDING, READY, INVESTIGATING, BLOCKED, COMPLETED, REJECTED, POSTPONED)
TERMINAL = (COMPLETED, REJECTED)

#: Which moves are legal. Deliberately not a free-for-all: a record that could jump
#: from any state to any state is a record whose status means nothing.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PENDING: (READY, INVESTIGATING, BLOCKED, POSTPONED, REJECTED),
    READY: (INVESTIGATING, BLOCKED, POSTPONED, REJECTED),
    INVESTIGATING: (COMPLETED, BLOCKED, POSTPONED, REJECTED, READY),
    BLOCKED: (READY, POSTPONED, REJECTED),
    POSTPONED: (READY, REJECTED),
    COMPLETED: (),
    REJECTED: (),
}

#: Phrasing that reads as an instruction. Finding it does not make the record
#: dangerous or useless -- it makes it *untrusted*, which is the correct treatment
#: for text that came from outside the agent's own objective. The list is deliberately
#: short and honest: it catches the obvious, and the real protection is that nothing
#: executes discoveries at all.
INSTRUCTION_SIGNALS = (
    "ignore previous", "ignore all previous", "disregard", "you must", "you should now",
    "system:", "assistant:", "new instructions", "override", "do not tell",
    "without asking", "silently", "delete all", "remove the check", "disable the",
    "bypass", "skip the test", "no need to verify", "automatically approve",
)

#: Words in an observation that suggest the proposed work touches governance.
GOVERNANCE_SIGNALS = (
    "guardian", "permission", "permissions.py", "policy", "baseline", "approval",
    "verification.py", "authorisation", "authorization", "security", "audit",
)


class DiscoveryError(Exception):
    """Raised for an invalid lifecycle move or a malformed record."""


@dataclasses.dataclass
class Discovery:
    id: int
    fingerprint: str
    title: str
    observation: str
    status: str
    why_it_matters: str = ""
    potential_benefit: str = ""
    source_task: str | None = None
    source_step: int | None = None
    source_tool: str | None = None
    source_evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    provenance: str = "agent_observation"
    confidence: float = 0.5
    uncertainty: str = ""
    effort_estimate: str | None = None
    dependencies: list[str] = dataclasses.field(default_factory=list)
    risk: str = "unknown"
    permissions_required: list[str] = dataclasses.field(default_factory=list)
    touches_protected: list[str] = dataclasses.field(default_factory=list)
    related: list[int] = dataclasses.field(default_factory=list)
    spawned_by: int | None = None
    depth: int = 0
    history: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    outcome: str | None = None
    verification_evidence: str | None = None
    flagged: list[str] = dataclasses.field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def open(self) -> bool:
        return self.status not in TERMINAL

    def render(self, *, full: bool = False) -> str:
        head = (f"[disc:{self.id}] {self.status.upper()} {self.title} "
                f"(confidence {self.confidence:.2f}, risk {self.risk})")
        if not full:
            return head + f" — {self.observation[:160]}"
        lines = [head,
                 f"  observation: {self.observation}",
                 f"  why it matters: {self.why_it_matters or 'not recorded'}",
                 f"  potential benefit: {self.potential_benefit or 'not recorded'}",
                 f"  uncertainty: {self.uncertainty or 'not recorded'}",
                 f"  effort: {self.effort_estimate or 'not estimated'}",
                 f"  dependencies: {', '.join(self.dependencies) or 'none recorded'}",
                 f"  risk: {self.risk}; permissions needed: "
                 f"{', '.join(self.permissions_required) or 'none'}",
                 f"  provenance: {self.provenance}"
                 + (f" (flags: {'; '.join(self.flagged)})" if self.flagged else ""),
                 f"  source: task={self.source_task or '-'} step={self.source_step or '-'} "
                 f"tool={self.source_tool or '-'}",
                 f"  evidence: {json.dumps(self.source_evidence, default=str)[:400]}",
                 f"  touches protected: {', '.join(self.touches_protected) or 'no'}",
                 f"  recorded {self.created_at}, updated {self.updated_at}"]
        if self.related:
            lines.append(f"  related: {', '.join('disc:%d' % r for r in self.related)}")
        if self.outcome:
            lines.append(f"  outcome: {self.outcome}")
        if self.verification_evidence:
            lines.append(f"  verification: {self.verification_evidence}")
        for entry in self.history[-4:]:
            lines.append(f"  history {entry.get('at', '?')}: {entry.get('event', '?')} — "
                         f"{str(entry.get('note', ''))[:160]}")
        return "\n".join(lines)


# -- deterministic helpers -------------------------------------------------

def fingerprint(title: str, observation: str) -> str:
    """A stable fingerprint of exactly what was observed.

    Case, punctuation and whitespace are normalised, and that is all: the fingerprint
    is taken over the whole normalised text. An earlier version hashed the set of
    significant tokens instead, which made "the parser drops empty lines" and "the
    parser drops blank lines" the same record *and* -- far worse -- made any two
    observations that happened to share their long words collapse into one, silently
    discarding the observation that differed. Exact-duplicate detection has to be
    exact; similar text is a *proposal* to merge, decided by the token-overlap path
    with its own evidence, not by a hash that quietly loses records.
    """

    normalised = " ".join(re.findall(r"[a-z0-9_]+", f"{title} {observation}".lower()))
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def _significant(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]{4,}", (text or "").lower())
    stop = {"that", "this", "with", "from", "when", "then", "than", "they", "them",
            "there", "their", "have", "has", "was", "were", "will", "would", "could",
            "should", "into", "over", "under", "about", "because", "which", "while"}
    return [w for w in words if w not in stop]


def overlap(left: Sequence[str], right: Sequence[str]) -> float:
    """Jaccard overlap of two token sets. Used only to *propose* a merge."""

    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def flag_untrusted(text: str) -> list[str]:
    """Instruction-like phrasing inside an observation. Recorded, never obeyed."""

    lowered = (text or "").lower()
    return [signal for signal in INSTRUCTION_SIGNALS if signal in lowered]


def governance_references(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [signal for signal in GOVERNANCE_SIGNALS if signal in lowered]


def score(record: Discovery) -> tuple[float, list[str]]:
    """How worth investigating this is, and why. Deterministic and explainable.

    The weights are a judgement, written down so it can be argued with: confidence
    dominates, evidence helps, breadth of benefit helps, risk and uncertainty
    subtract. Nothing here decides permission to act -- the guardian and the
    permission engine do that, whatever this number says.
    """

    reasons: list[str] = []
    value = max(0.0, min(1.0, float(record.confidence)))
    reasons.append(f"confidence {value:.2f}")
    evidence = record.source_evidence or {}
    if evidence.get("file") or evidence.get("tool") or evidence.get("error"):
        value += 0.1
        reasons.append("has concrete evidence")
    if record.potential_benefit.strip():
        value += 0.1
        reasons.append("benefit stated")
    if record.uncertainty.strip():
        value -= 0.1
        reasons.append("uncertainty recorded")
    if record.risk == "high":
        value -= 0.2
        reasons.append("high risk")
    elif record.risk == "low":
        value += 0.05
        reasons.append("low risk")
    if record.dependencies:
        value -= 0.05
        reasons.append(f"{len(record.dependencies)} dependency/ies")
    if record.touches_protected:
        value -= 0.1
        reasons.append("touches protected components (authorization required)")
    if record.flagged:
        value -= 0.15
        reasons.append("contains instruction-like text: treat as untrusted")
    return round(max(0.0, min(1.5, value)), 4), reasons


# -- the queue -------------------------------------------------------------

class DiscoveryQueue:
    """Durable storage for deferred observations, on the existing SQLite store."""

    def __init__(self, store: Store, *, max_depth: int = 2):
        self.store = store
        self.max_depth = max_depth

    # -- capture ----------------------------------------------------------
    def capture(
        self,
        *,
        title: str,
        observation: str,
        why_it_matters: str = "",
        potential_benefit: str = "",
        confidence: float = 0.5,
        uncertainty: str = "",
        effort_estimate: str | None = None,
        dependencies: Iterable[str] = (),
        risk: str = "unknown",
        permissions_required: Iterable[str] = (),
        provenance: str = "agent_observation",
        source_task: str | None = None,
        source_step: int | None = None,
        source_tool: str | None = None,
        source_evidence: dict[str, Any] | None = None,
        spawned_by: int | None = None,
    ) -> tuple[Discovery, str]:
        """Store an observation, or fold it into one that already exists.

        Returns ``(record, action)`` where action is ``created``, ``merged`` or
        ``related_created``. Never raises for a duplicate: a repeat observation is
        normal, and losing the second piece of evidence would be a defect.
        """

        title = (title or "").strip()
        observation = (observation or "").strip()
        if not title:
            raise DiscoveryError("a discovery needs a title")
        if not observation:
            raise DiscoveryError("a discovery needs an observation to record")

        evidence = dict(source_evidence or {})
        print_digest = fingerprint(title, observation)
        flagged = flag_untrusted(f"{title}. {observation}. {why_it_matters}")
        touches = sorted(set(governance_references(
            f"{title} {observation} {why_it_matters} {potential_benefit}")))
        if provenance == "observed_content" and not flagged:
            # Not a finding, a statement of trust: text that came from outside still
            # gets evaluated, it just does not pretend to be the agent's own idea.
            flagged = ["carried from inspected content"]

        depth = 0
        if spawned_by is not None:
            parent = self.get(spawned_by)
            depth = parent.depth + 1

        existing = self._existing(print_digest, title, observation, evidence)
        if existing is not None:
            return self._merge(existing, evidence, title, observation,
                               why_it_matters, potential_benefit, flagged), "merged"

        with self.store.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO discovery(fingerprint, title, observation, why_it_matters, "
                "potential_benefit, source_task, source_step, source_tool, source_evidence, "
                "provenance, confidence, uncertainty, effort_estimate, dependencies, risk, "
                "permissions_required, touches_protected, related, status, spawned_by, depth, "
                "history, flagged, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (print_digest, title, observation, why_it_matters, potential_benefit,
                 source_task, source_step, source_tool, json.dumps(evidence, default=str),
                 provenance, float(max(0.0, min(1.0, confidence))), uncertainty,
                 effort_estimate, json.dumps(list(dependencies)),
                 risk, json.dumps(list(permissions_required)),
                 json.dumps(touches), json.dumps(
                     [] if spawned_by is None else [int(spawned_by)]),
                 READY if depth <= self.max_depth else BLOCKED,
                 None if spawned_by is None else int(spawned_by), depth,
                 json.dumps([{"at": now(), "event": "captured",
                              "note": f"provenance={provenance}",
                              "evidence": evidence}]),
                 json.dumps(flagged), now(), now()),
            )
            record_id = int(cursor.lastrowid or 0)

        record = self.get(record_id)
        self.store.append_event(
            "discovery_captured",
            {"discovery_id": record.id, "title": record.title, "status": record.status,
             "provenance": record.provenance, "depth": record.depth,
             "flagged": record.flagged, "source_task": record.source_task,
             "source_tool": record.source_tool},
            task_id=source_task,
        )
        if depth > self.max_depth:
            self.store.append_event(
                "discovery_chain_limit",
                {"discovery_id": record.id, "depth": depth, "max_depth": self.max_depth,
                 "reason": "a discovery found while investigating another discovery enters "
                           "the queue instead of starting nested work"},
                task_id=source_task,
            )
        return record, "created"

    def _existing(self, print_digest: str, title: str, observation: str,
                  evidence: dict[str, Any]) -> Discovery | None:
        rows = self.store.query(
            "SELECT id FROM discovery WHERE fingerprint=? AND status NOT IN (?,?) "
            "ORDER BY id LIMIT 5",
            (print_digest, COMPLETED, REJECTED),
        )
        for row in rows:
            return self.get(int(row["id"]))
        # Same source and materially the same words: probably the same observation
        # seen twice. Proposed, then checked against risk and permissions, because
        # two observations that *sound* alike can still be different work.
        reference = self._reference(evidence)
        if not reference:
            return None
        tokens = _significant(title + " " + observation)
        for row in self.store.query(
            "SELECT id, title, observation FROM discovery WHERE status NOT IN (?,?) "
            "ORDER BY id DESC LIMIT 40",
            (COMPLETED, REJECTED),
        ):
            other = self.get(int(row["id"]))
            if self._reference(other.source_evidence) != reference:
                continue
            other_tokens = _significant(other.title + " " + other.observation)
            shared = set(tokens) & set(other_tokens)
            # A high overlap between two two-word sets is a coincidence, not a duplicate:
            # sharing "observation" and "behind" says nothing about whether it is the same
            # observation. Three shared significant words is the floor for calling two
            # records the same thing.
            if len(shared) < 3:
                continue
            if overlap(tokens, other_tokens) >= 0.6:
                return other
        return None

    @staticmethod
    def _reference(evidence: dict[str, Any]) -> str:
        if not evidence:
            return ""
        return str(evidence.get("file") or evidence.get("path") or evidence.get("tool")
                   or evidence.get("error") or "")

    def _merge(self, existing: Discovery, evidence: dict[str, Any], title: str,
               observation: str, why_it_matters: str, potential_benefit: str,
               flagged: list[str]) -> Discovery:
        """Attach evidence to an existing record without rewriting its provenance."""

        differences: list[str] = []
        merged_evidence = dict(existing.source_evidence)
        for key, value in evidence.items():
            if key not in merged_evidence:
                merged_evidence[key] = value
                differences.append(f"evidence.{key} added")
            elif merged_evidence[key] != value:
                differences.append(f"evidence.{key} differs")
        if title and title != existing.title:
            differences.append(f"title said: {title[:120]}")
        if why_it_matters and why_it_matters != existing.why_it_matters:
            differences.append("a second reason was given")
        if potential_benefit and potential_benefit != existing.potential_benefit:
            differences.append("a second benefit was given")
        merged_flags = sorted(set(existing.flagged) | set(flagged))
        entry = {"at": now(), "event": "merged",
                 "note": "; ".join(differences) or "repeat observation, no new detail",
                 "evidence": evidence}
        history = existing.history + [entry]
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE discovery SET source_evidence=?, history=?, flagged=?, updated_at=? "
                "WHERE id=?",
                (json.dumps(merged_evidence, default=str), json.dumps(history),
                 json.dumps(merged_flags), now(), existing.id),
            )
        self.store.append_event(
            "discovery_merged",
            {"discovery_id": existing.id, "differences": differences,
             "merged_fields": sorted(evidence)},
            task_id=existing.source_task,
        )
        return self.get(existing.id)

    def link(self, left: int, right: int) -> None:
        """Record that two discoveries are related without merging them."""

        for source, target in ((left, right), (right, left)):
            record = self.get(source)
            related = sorted(set(record.related) | {int(target)})
            with self.store.transaction() as conn:
                conn.execute("UPDATE discovery SET related=?, updated_at=? WHERE id=?",
                             (json.dumps(related), now(), source))

    # -- lifecycle --------------------------------------------------------
    def transition(self, discovery_id: int, status: str, *, note: str,
                   evidence: str | None = None, by: str = "agent",
                   outcome: str | None = None) -> Discovery:
        """Move a record, or refuse. The check is here, not in the caller."""

        record = self.get(discovery_id)
        if status not in STATUSES:
            raise DiscoveryError(f"unknown status {status!r}; known: {', '.join(STATUSES)}")
        if status == record.status:
            raise DiscoveryError(
                f"disc:{discovery_id} is already {status}; a transition must change state"
            )
        if status not in TRANSITIONS[record.status]:
            raise DiscoveryError(
                f"disc:{discovery_id} cannot move from {record.status} to {status} "
                f"(allowed: {', '.join(TRANSITIONS[record.status]) or 'nothing: terminal'})"
            )
        if status == REJECTED and not note.strip():
            raise DiscoveryError("a rejection must record why it was rejected")
        if status == COMPLETED and not (evidence or "").strip():
            # Same rule the task loop applies to itself: completion is a claim that
            # needs evidence, not a status that can be asserted.
            raise DiscoveryError("a discovery cannot be completed without verification evidence")

        entry = {"at": now(), "event": f"{record.status} -> {status}", "by": by,
                 "note": note, "evidence": evidence}
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE discovery SET status=?, history=?, updated_at=?, "
                "outcome=COALESCE(?, outcome), "
                "verification_evidence=COALESCE(?, verification_evidence) WHERE id=?",
                (status, json.dumps(record.history + [entry]), now(),
                 outcome, evidence, discovery_id),
            )
        updated = self.get(discovery_id)
        self.store.append_event(
            "discovery_transitioned",
            {"discovery_id": discovery_id, "from": record.status, "to": status,
             "by": by, "note": note[:400], "evidence": (evidence or "")[:400]},
            task_id=record.source_task,
        )
        return updated

    # -- reading ----------------------------------------------------------
    def get(self, discovery_id: int) -> Discovery | None:
        """One record, or ``None`` when there is no such id.

        A lookup that raises turns "that record is gone" into a failed request: the CLI
        reports it as an unknown id and the viewer answers 404, and both of those read the
        return value. Callers that need an error for a missing record raise it themselves,
        where they know what the operation was.
        """

        row = self.store.one("SELECT * FROM discovery WHERE id=?", (int(discovery_id),))
        return None if row is None else self._row(row)

    def all(self, *, limit: int = 50, statuses: Sequence[str] | None = None) -> list[Discovery]:
        sql = "SELECT * FROM discovery"
        params: list[Any] = []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [self._row(row) for row in self.store.query(sql, tuple(params))]

    def eligible(self, *, limit: int = 5) -> list[Discovery]:
        """Ready records, best first, with the reason they were chosen."""

        ready = [r for r in self.all(limit=200, statuses=(READY,)) if r.open]
        ranked = sorted(ready, key=lambda r: (-score(r)[0], r.id))
        return ranked[:limit]

    def by_source_task(self, task_id: str) -> list[Discovery]:
        return [self._row(row) for row in self.store.query(
            "SELECT * FROM discovery WHERE source_task=? ORDER BY id", (task_id,))]

    def relevant(self, query: str, *, limit: int = 5) -> list[Discovery]:
        """Discoveries whose words overlap the query, for context assembly.

        Deterministic token overlap rather than semantic search: the same reason the
        memory layer uses FTS -- a dependency that has to be running for retrieval to
        work is a dependency this system cannot justify.
        """

        tokens = set(_significant(query))
        if not tokens:
            return []
        scored: list[tuple[float, int, Discovery]] = []
        for record in self.all(limit=200):
            if record.status in TERMINAL:
                continue
            text = _significant(f"{record.title} {record.observation} {record.why_it_matters}")
            if not text:
                continue
            fraction = overlap(tokens, text)
            if fraction > 0:
                scored.append((fraction, -record.id, record))
        scored.sort(reverse=True)
        return [record for _, _, record in scored[:limit]]

    def counts(self) -> dict[str, int]:
        rows = self.store.query(
            "SELECT status, COUNT(*) AS n FROM discovery GROUP BY status")
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        counts["open"] = sum(counts[s] for s in STATUSES if s not in TERMINAL)
        return counts

    def review(self, *, limit: int = 20) -> list[tuple[Discovery, float, list[str]]]:
        """Every open discovery with the score and reasons behind it."""

        out: list[tuple[Discovery, float, list[str]]] = []
        for record in self.all(limit=limit):
            if not record.open:
                continue
            value, reasons = score(record)
            out.append((record, value, reasons))
        return sorted(out, key=lambda item: (-item[1], item[0].id))

    @staticmethod
    def _row(row: Any) -> Discovery:
        def _json(value: Any, fallback: Any) -> Any:
            try:
                return json.loads(value) if value else fallback
            except (TypeError, ValueError):
                return fallback

        return Discovery(
            id=int(row["id"]), fingerprint=str(row["fingerprint"]),
            title=str(row["title"]), observation=str(row["observation"]),
            status=str(row["status"]),
            why_it_matters=str(row["why_it_matters"] or ""),
            potential_benefit=str(row["potential_benefit"] or ""),
            source_task=row["source_task"], source_step=row["source_step"],
            source_tool=row["source_tool"],
            source_evidence=_json(row["source_evidence"], {}),
            provenance=str(row["provenance"] or "agent_observation"),
            confidence=float(row["confidence"] or 0.0),
            uncertainty=str(row["uncertainty"] or ""),
            effort_estimate=row["effort_estimate"],
            dependencies=[str(x) for x in _json(row["dependencies"], [])],
            risk=str(row["risk"] or "unknown"),
            permissions_required=[str(x) for x in _json(row["permissions_required"], [])],
            touches_protected=[str(x) for x in _json(row["touches_protected"], [])],
            related=[int(x) for x in _json(row["related"], [])],
            spawned_by=None if row["spawned_by"] is None else int(row["spawned_by"]),
            depth=int(row["depth"] or 0),
            history=_json(row["history"], []),
            outcome=row["outcome"], verification_evidence=row["verification_evidence"],
            flagged=[str(x) for x in _json(row["flagged"], [])],
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )


# -- bounded follow-up cycles ----------------------------------------------

@dataclasses.dataclass
class BudgetExhausted(RuntimeError):
    """Raised by a spend callback when the next unit of work would pass a limit.

    A refusal has to travel, not be returned and forgotten: the caller that was about
    to make a model call must not make it, and the reason must reach the record. So the
    callback raises and the runtime catches it, stops cleanly and says which limit
    stopped it.
    """

    def __init__(self, kind: str, limit: float, spent: float,
                 reason: str | None = None) -> None:
        self.kind = kind
        self.limit = limit
        self.spent = spent
        # The sentence the record will carry. One limit, one spelling: a stop that is
        # named "seconds budget exhausted" in the exception and "time budget exhausted"
        # on the record is two limits as far as a reader is concerned.
        self.reason = reason or f"{kind} budget exhausted ({spent:g} of {limit:g})"
        super().__init__(self.reason)


def no_spend(kind: str, amount: float = 1) -> bool:
    """The default spend callback: an ordinary task is charged to nothing.

    The runner is handed a callback rather than reaching for a budget it does not have,
    which is why this exists: a task that was not started by a follow-up cycle has no
    follow-up budget to charge, and must not acquire one by accident. Every default in
    the runtime is this function, so the only thing that ever refuses a call is a budget
    that a cycle deliberately passed in.
    """

    return True


@dataclasses.dataclass
class FollowUpBudget:
    """What one follow-up cycle may spend. Every limit is checked before starting."""

    max_items: int = 2
    max_seconds: float = 120.0
    max_model_calls: int = 6
    max_steps: int = 6
    max_tokens: int = 20000
    enabled: bool = True

    #: True when the runtime can observe real token counts for this cycle. When it
    #: cannot, ``max_tokens`` is an estimate limit and the report says so rather than
    #: claiming a guarantee it cannot enforce.
    tokens_are_measured: bool = False

    #: The counters, one per limit, charged by whoever spends: the cycle between items
    #: and the runner's own calls during one. There is exactly one of each, which is the
    #: point -- a cycle that counted separately from the runner would report a total that
    #: neither of them enforced.
    spent: dict[str, float] = dataclasses.field(
        default_factory=lambda: {"model_calls": 0.0, "steps": 0.0, "tokens": 0.0})

    #: Set by the charge that was refused, so the stop can be reported with the limit's
    #: own name instead of a sentence someone assembled afterwards.
    exhausted: str = ""

    #: The clock and the moment the time limit runs out, both set by the cycle when it
    #: starts. Without them ``max_seconds`` could only be checked between items -- which
    #: says nothing about the item that is running, and that is the one that can overrun
    #: it. A budget built and used on its own has no deadline and enforces no time.
    clock: Callable[[], float] | None = None
    deadline: float | None = None

    def limit_for(self, kind: str) -> float:
        limits = {"model_calls": self.max_model_calls, "steps": self.max_steps,
                  "tokens": self.max_tokens}
        if kind not in limits:
            raise KeyError(f"no budget limit named {kind!r}; charge one of "
                           f"{sorted(limits)} instead")
        return float(limits[kind])

    def spend(self, kind: str, amount: float = 1) -> bool:
        """Charge the budget *before* the work happens, or refuse it.

        This is the callback the runner passes down to its model calls and its command
        executions. Refusing is a raise rather than a ``False`` because the caller is
        about to do something with a cost: a return value that is easy to ignore is how
        a budget becomes a report of what was already spent.
        """

        if not self.enabled:
            return True
        if amount < 0:
            raise ValueError("a budget is charged, never refunded")
        self._check_deadline()
        limit = self.limit_for(kind)
        used = float(self.spent.get(kind, 0.0))
        if used + amount > limit:
            self.exhausted = f"{kind} budget exhausted"
            raise BudgetExhausted(kind, limit, used)
        self.spent[kind] = used + amount
        if kind == "tokens":
            # The counter is only ever charged with usage the provider reported, so the
            # token limit stops being an estimate the moment it is charged.
            self.tokens_are_measured = True
        return True

    def record(self, kind: str, amount: float) -> None:
        """Add spend that has already happened and could not be charged in advance.

        Token usage is the case: the size of a reply is not known until it arrives. The
        total stays honest either way, and an overspend recorded here is refused by the
        *next* charge rather than hidden.
        """

        if amount <= 0 or not self.enabled:
            return
        self.limit_for(kind)  # unknown kinds are a bug, not a silent no-op
        self.spent[kind] = float(self.spent.get(kind, 0.0)) + float(amount)

    def _check_deadline(self) -> None:
        """Refuse work once the cycle's time is up, wherever the charge comes from."""

        if self.deadline is None or self.clock is None:
            return
        elapsed = self.clock()
        remaining = self.deadline - elapsed
        if remaining <= 0:
            spent = self.max_seconds - remaining
            self.exhausted = "time budget exhausted"
            raise BudgetExhausted(
                "seconds", self.max_seconds, spent,
                reason=f"time budget exhausted ({spent:g}s of {self.max_seconds:g}s)")

    def remaining_seconds(self) -> float | None:
        """How long the cycle has left, or None when it is enforcing no deadline."""

        if self.deadline is None or self.clock is None:
            return None
        return self.deadline - self.clock()

    def exhausted_limit(self) -> str:
        """The limit that is used up, or "" while the budget still has room."""

        if not self.enabled:
            return ""
        if self.deadline is not None and self.clock is not None \
                and self.clock() >= self.deadline:
            return "time budget exhausted"
        for kind, limit in (("model_calls", self.max_model_calls), ("steps", self.max_steps),
                            ("tokens", self.max_tokens)):
            if float(self.spent.get(kind, 0.0)) >= float(limit):
                # One spelling for one limit: the reason a charge was refused and the
                # reason a cycle stopped have to be the same sentence, or the journal
                # ends up with two names for one thing.
                return f"{kind} budget exhausted"
        return ""

    def report(self) -> dict[str, Any]:
        """The budget as the record should hold it: limits, spend, and what stopped it."""

        return {"limits": {"max_items": self.max_items, "max_seconds": self.max_seconds,
                           "max_model_calls": self.max_model_calls,
                           "max_steps": self.max_steps, "max_tokens": self.max_tokens},
                "spent": {kind: (int(value) if kind != "seconds" else value)
                          for kind, value in self.spent.items()},
                "tokens_measured": self.tokens_are_measured,
                "exhausted": self.exhausted}

    def as_dict(self) -> dict[str, Any]:
        """The budget as plain data, for a caller that is about to store or print it.

        The clock and the deadline are left out on purpose. They are the cycle's control
        handles rather than part of the budget, and a callable that reaches a durable
        journal payload is silently recorded as the string representation of a function
        -- which is how this was found.
        """

        data = dataclasses.asdict(self)
        data.pop("clock", None)
        data.pop("deadline", None)
        return data


@dataclasses.dataclass
class FollowUpResult:
    discovery_id: int
    status: str
    reason: str
    spent: dict[str, Any] = dataclasses.field(default_factory=dict)
    #: What the investigation actually looked at, so the conclusion can be checked by
    #: someone who was not there. A result without evidence is a claim.
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class FollowUpCycle:
    """Runs eligible discoveries within a budget, using the task runner it is given.

    The runner is a callback, not a copy of ``LiviumAgent``: investigating a
    discovery *is* a task -- same loop, same tools, same permissions, same
    verification -- so this class must not grow a second execution engine.
    """

    def __init__(self, queue: DiscoveryQueue, *, budget: FollowUpBudget):
        self.queue = queue
        self.budget = budget
        #: Why the last run stopped. Set by run(); the caller reports it rather than
        #: inferring it, because "the queue was empty" and "the budget ran out" look
        #: exactly the same from the results list alone.
        self.last_stop_reason = "the cycle has not run"

    def run(
        self,
        runner: Callable[[Discovery, FollowUpBudget], dict[str, Any]],
        *,
        clock: Callable[[], float],
        task_id: str | None = None,
        only: int | None = None,
    ) -> list[FollowUpResult]:
        """Investigate eligible discoveries until the budget says stop.

        ``runner`` returns ``{"status": str, "reason": str, "evidence": str,
        "spent": {"model_calls": int, "steps": int, "seconds": float,
        "tokens": int | None}}``. Whatever it returns is what the record says
        happened: nothing here invents an outcome. ``only`` restricts the cycle to one
        record, which is what a human asking for a single investigation means.
        """

        results: list[FollowUpResult] = []
        if not self.budget.enabled:
            return results

        started = clock()
        # One clock for the one time limit: the budget is told when its time runs out, so
        # the charge that happens *during* an item can refuse it -- not only the check
        # between items, which cannot see an item that overruns.
        self.budget.clock = clock
        self.budget.deadline = started + float(self.budget.max_seconds)

        if only is not None:
            record = self.queue.get(only)
            if record is None or not record.open:
                return results
            candidates = [record]
        else:
            candidates = self.queue.eligible(limit=max(0, self.budget.max_items))

        for record in candidates:
            elapsed = clock() - started
            used_up = self.budget.exhausted_limit()
            if used_up:
                # The counters are the budget's own, so a limit the runner used up
                # during the previous item stops the cycle here rather than at the
                # point where the total happens to be checked.
                self._stop(used_up, record, task_id,
                           {**self.budget.spent, "seconds": round(elapsed, 3),
                            "limit": used_up})
                break

            before = dict(self.budget.spent)
            self.queue.transition(
                record.id, INVESTIGATING,
                note="follow-up cycle started this investigation",
                by="harness",
            )
            try:
                outcome = runner(record, self.budget) or {}
            except BudgetExhausted as exc:
                # The runner tried to spend past the limit and was refused. That is a
                # stop, not a crash: the item returns to ready for a later cycle.
                outcome = {"stopped_because": exc.reason}
            except Exception as exc:  # an investigation that crashes is not a success
                outcome = {"status": POSTPONED,
                           "reason": f"investigation raised {type(exc).__name__}: {exc}"}

            self._settle_reported_spend(outcome.get("spent") or {}, before)

            stopped = str(outcome.get("stopped_because") or "")
            if stopped:
                self.queue.transition(
                    record.id, READY,
                    note=f"budget stopped this investigation: {stopped}",
                    by="harness",
                )
                results.append(FollowUpResult(record.id, READY, stopped,
                                              self._spent_since(before)))
                self._stop(stopped, record, task_id, {**self.budget.spent, "limit": stopped})
                break

            status = str(outcome.get("status") or POSTPONED)
            reason = str(outcome.get("reason") or "no reason recorded by the investigation")
            evidence = outcome.get("evidence")
            try:
                if status == COMPLETED:
                    self.queue.transition(record.id, COMPLETED, note=reason,
                                          evidence=str(evidence or ""), by="harness",
                                          outcome=reason)
                elif status == REJECTED:
                    self.queue.transition(record.id, REJECTED, note=reason, by="harness",
                                          outcome=reason)
                elif status == BLOCKED:
                    self.queue.transition(record.id, BLOCKED, note=reason, by="harness",
                                          outcome=reason)
                else:
                    status = POSTPONED
                    self.queue.transition(record.id, POSTPONED, note=reason, by="harness",
                                          outcome=reason)
            except DiscoveryError as exc:
                # A refused transition is a real event, not something to swallow: the
                # record keeps its old status and the reason is journalled.
                self.queue.store.append_event(
                    "discovery_transition_refused",
                    {"discovery_id": record.id, "attempted": status, "error": str(exc)},
                    task_id=task_id,
                )
                status = record.status
                reason = f"transition to {status} refused: {exc}"

            results.append(FollowUpResult(record.id, status, reason,
                                          self._spent_since(before),
                                          evidence=str(evidence or "")))

        if self.last_stop_reason == "the cycle has not run":
            self.last_stop_reason = "every eligible discovery was evaluated"
        self.queue.store.append_event(
            "discovery_cycle_complete",
            {"investigated": [r.discovery_id for r in results],
             "budget": self.budget.report(),
             "spent": {"model_calls": int(self.budget.spent.get("model_calls", 0)),
                       "steps": int(self.budget.spent.get("steps", 0)),
                       "tokens": int(self.budget.spent.get("tokens", 0)),
                       "seconds": round(clock() - started, 3)},
             "tokens_measured": self.budget.tokens_are_measured,
             "stopped_because": (self.last_stop_reason
                                 if self.last_stop_reason != "every eligible discovery was "
                                                            "evaluated" else "")},
            task_id=task_id,
        )
        return results

    def _spent_since(self, before: dict[str, float]) -> dict[str, Any]:
        """What one item spent, read from the shared counters rather than counted twice."""

        spent = self.budget.spent
        remaining = self.budget.remaining_seconds()
        return {
            "model_calls": int(spent.get("model_calls", 0) - before.get("model_calls", 0)),
            "steps": int(spent.get("steps", 0) - before.get("steps", 0)),
            "tokens": int(spent.get("tokens", 0) - before.get("tokens", 0)),
            "tokens_measured": self.budget.tokens_are_measured,
            **({"seconds_left": round(remaining, 3)} if remaining is not None else {}),
        }

    def _settle_reported_spend(self, reported: dict[str, Any], before: dict[str, float]) -> None:
        """Add spend a runner reported but did not charge as it went.

        A runner that charges the shared counters itself (the real one does) reports what
        the counters already hold, and nothing is added. A runner that only returns
        figures at the end -- a scripted one in a test, say -- still has its spend
        counted, because the alternative is a budget that believes it has room it has
        already used. Nothing is ever subtracted: the counters only rise.
        """

        for kind in ("model_calls", "steps"):
            already = self.budget.spent.get(kind, 0) - before.get(kind, 0)
            extra = float(reported.get(kind) or 0) - already
            if extra > 0:
                self.budget.record(kind, extra)
        if reported.get("tokens"):
            already = self.budget.spent.get("tokens", 0) - before.get("tokens", 0)
            extra = float(reported.get("tokens") or 0) - already
            if extra > 0:
                self.budget.record("tokens", extra)
        if reported.get("tokens_measured"):
            # The runner's own count was observed rather than estimated, so the token
            # limit stops being an estimate limit.
            self.budget.tokens_are_measured = True

    def _stop(self, reason: str, next_record: Discovery, task_id: str | None,
              spent: dict[str, Any]) -> None:
        """Checkpoint: stop starting work, say why, and leave the queue resumable."""

        self.last_stop_reason = reason
        self.queue.store.append_event(
            "discovery_cycle_stopped",
            {"reason": reason, "spent": spent, "next": next_record.id,
             "next_status": next_record.status,
             "resumes": "a later cycle picks this up; nothing is lost by stopping"},
            task_id=task_id,
        )
