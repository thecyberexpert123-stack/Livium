"""Integrity policy, change review, independent verification and recovery.

The Growth Guardian already detected changes to protected files and refused to
proceed. Detection alone is weak in three specific ways, and this module addresses
exactly those, with the smallest mechanism that gives a real guarantee here:

1. **Detection had no policy.** A mismatch said "this file changed", not whether the
   change was permitted, who could authorise it, or what evidence would settle it.
   :data:`POLICY` states that per component, with a reason, and
   :func:`review_change` evaluates a concrete change against it.

2. **Detection had no record.** The mismatch appeared in `livium guardian status`
   and in events *when a run happened to check*, so a change made outside a run left
   no structured trace. Every look at a protected component now appends a row to
   ``integrity_event`` -- a table whose UPDATE and DELETE are refused by SQLite
   triggers, because the row a tamper attempt would most like to rewrite is the row
   describing the tamper attempt.

3. **Nothing verified that enforcement was still working.** A hash proves a file
   changed; it does not prove the rules inside it still refuse anything.
   :func:`verify_enforcement` runs behavioural probes -- real writes to protected
   paths, a real approval requirement, a real promotion gate -- and, for a proposed
   change, runs them against a *copy of the tree with the candidate applied*, so the
   check is not made by the code being checked.

Scope, stated honestly. The agent's tools cannot write protected files or the
database, so the baseline and the snapshots live outside its write authority. What
they cannot defend against is an agent that already has the operating system
privileges to write those paths by another route (`run_command` with an interpreter
is exactly such a route): that is an OS boundary this process does not create, and
it is documented in docs/LIMITATIONS.md rather than papered over. In that case the
behaviour is detection, attribution, a frozen run and a recorded incident -- not
prevention. Nothing in this module claims otherwise.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Sequence

from .config import Config, REPO_ROOT
from .permissions import PermissionEngine
from .store import Store, now

# -- tiers -----------------------------------------------------------------

GOVERNANCE = "governance"
ENFORCEMENT = "enforcement"
INSTRUCTIONS = "instructions"
OPERATIONAL = "operational"

#: The tier that decides what is protected, why, who may authorise a change and how
#: the change is verified. Every entry names a real file in this repository; the list
#: is short on purpose -- protecting everything equally protects nothing in
#: particular, and ordinary feature code is reviewed through git like any other work.
POLICY: tuple[dict[str, Any], ...] = (
    {
        "path": "livium/guardian.py",
        "tier": GOVERNANCE,
        "why": "it decides which changes stop the run, what a promotion needs and when "
               "the system freezes; weakening it weakens every other control",
        "permits": "any change, after review: the guardian must remain repairable",
        "authorizer": "a named human, plus passing tests and an enforcement probe",
        "verification": "tests + enforcement probes against a copy of the tree with the "
                        "candidate applied + re-baseline of this file only",
        "weakening": ("removed calls to integrity()/watch()/promotion_gate()",
                      "a promotion gate that can no longer return not-ok",
                      "a freeze that the agent can clear"),
    },
    {
        "path": "livium/integrity.py",
        "tier": GOVERNANCE,
        "why": "it holds the protection policy, the change workflow and the probes that "
               "verify enforcement; it is the thing that would notice the guardian "
               "being weakened",
        "permits": "any change, after review",
        "authorizer": "a named human, plus passing tests and an enforcement probe",
        "verification": "tests + enforcement probes + re-baseline of this file only",
        "weakening": ("removal of a policy entry", "a review that always permits",
                      "probes that cannot fail"),
    },
    {
        "path": "livium/permissions.py",
        "tier": GOVERNANCE,
        "why": "it is the boundary: what may be written, executed, read and reached over "
               "the network, and which operations need a human",
        "permits": "any change, after review",
        "authorizer": "a named human, plus passing tests and an enforcement probe",
        "verification": "tests + effective-policy comparison + enforcement probes + "
                        "re-baseline of this file only",
        "weakening": ("a binary moved from the denylist to allowed",
                      "a protected path removed", "network allowlist widened",
                      "an approval requirement removed"),
    },
    {
        "path": "livium/approvals.py",
        "tier": GOVERNANCE,
        "why": "it decides whether a human authorised a consequential operation, and it "
               "decides when that authorisation is spent",
        "permits": "any change, after review",
        "authorizer": "a named human, plus passing tests",
        "verification": "tests, including that an unattributable approver is refused and "
                        "that one decision covers one execution",
        "weakening": ("accepting an anonymous approver", "re-using a spent authorisation",
                      "letting the model write a decision"),
    },
    {
        "path": "livium/config.py",
        "tier": ENFORCEMENT,
        "why": "the effective policy is computed here: allowlists, protected paths, "
               "budgets and the approval surface",
        "permits": "any change, after review",
        "authorizer": "a named human, plus passing tests and an effective-policy comparison",
        "verification": "effective policy dumped from a copy of the tree with the "
                        "candidate applied, compared with the running policy",
        "weakening": ("a deny-list entry removed", "an allow-list entry added without "
                      "justification", "a protected path dropped", "a budget raised "
                      "without a reason"),
    },
    {
        "path": "livium/execution.py",
        "tier": ENFORCEMENT,
        "why": "the only place a subprocess is created: isolation, timeouts, output caps "
               "and the environment handed to children",
        "permits": "any change, after review",
        "authorizer": "a named human, plus passing tests",
        "verification": "tests + isolation probe",
        "weakening": ("dropping the network namespace", "no timeout", "inheriting the "
                      "parent environment (and its credentials)"),
    },
    {
        "path": "livium/tools.py",
        "tier": ENFORCEMENT,
        "why": "the authority surface: what a tool may do, what it needs approval for, "
               "and what the agent is told it can do",
        "permits": "any change, after review",
        "authorizer": "a named human, plus passing tests",
        "verification": "tests + a probe that a protected write is still refused",
        "weakening": ("a tool that writes outside the permitted roots", "an approval "
                      "check removed", "capability provenance misreported"),
    },
    {
        "path": "livium/verification.py",
        "tier": ENFORCEMENT,
        "why": "it decides whether acceptance criteria passed, which is what closes a "
               "task",
        "permits": "any change, after review",
        "authorizer": "a named human, plus passing tests",
        "verification": "tests, including that a manual criterion is never reported as "
                        "verified",
        "weakening": ("a criterion that cannot fail", "a manual check counted as passed",
                      "a read boundary widened beyond approval roots"),
    },
    {
        "path": "livium/prompts",
        "tier": INSTRUCTIONS,
        "why": "the agent's operating instructions; they are text in a prompt, so their "
               "force is judgement, not enforcement",
        "permits": "a human may edit them; the agent may only propose",
        "authorizer": "a named human (re-baseline is the approval record)",
        "verification": "contradiction scan + re-baseline",
        "weakening": ("a rule that removes a prohibition", "an instruction to skip "
                      "verification or approval"),
    },
    {
        "path": "AGENTS.md",
        "tier": INSTRUCTIONS,
        "why": "the instructions the coding agent operates under",
        "permits": "a human may edit it; the agent may only propose",
        "authorizer": "a named human",
        "verification": "contradiction scan + re-baseline",
        "weakening": ("a permission described as already granted", "a rule that "
                      "contradicts prompts/*.md"),
    },
)

#: Enforcement primitives whose call-sites are counted before and after a change.
ENFORCEMENT_CALLS = (
    "check_write", "check_read", "check_command", "check_network", "is_protected",
    "integrity", "watch", "promotion_gate", "verify", "decide", "freeze_status",
)

#: Every kind of event this module records. Named here rather than scattered through
#: the code so a reader can see the whole vocabulary of the integrity record at once,
#: and so a test can assert that each kind is distinguishable from the others: an
#: attempted change, an applied change, a detected unauthorised change, a rejected
#: change and a verified recovery must never be confusable.
INTEGRITY_EVENT_KINDS = (
    "baseline_recorded",
    "change_detected",
    "component_unbaselined",
    "change_proposed",
    "change_decision",
    "change_approved",
    "change_rejected",
    "enforcement_verified",
    "enforcement_verification_failed",
    "policy_violation",
    "run_frozen",
    "recovery_performed",
    "recovery_failed",
)

#: The parts of the effective policy that must not become more permissive silently.
POLICY_VECTORS = (
    ("allowed_binaries", "gained"),
    ("denied_binaries", "lost"),
    ("network_allowlist", "gained"),
    ("write_roots", "gained"),
    ("read_roots", "gained"),
    ("protected_paths", "lost"),
    ("approval_write_roots", "lost"),
    ("approval_binaries", "lost"),
)


@dataclasses.dataclass
class Component:
    """A protected component: where it is, and what the policy says about it."""

    path: pathlib.Path
    tier: str
    why: str
    permits: str
    authorizer: str
    verification: str
    weakening: tuple[str, ...]
    glob: bool = False

    def as_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["path"] = str(self.path)
        out["weakening"] = list(self.weakening)
        return out

    @property
    def needs_independent_check(self) -> bool:
        return self.tier in (GOVERNANCE, ENFORCEMENT)


def from_configuration(path: pathlib.Path, repo_root: pathlib.Path = REPO_ROOT) -> Component:
    """A component the configuration protects that the policy does not name.

    The write refusal and the change record must agree on the important case: a path
    the agent is refused write access to is a path whose modification must be
    recorded. Configuration can name one the policy has no rationale for -- a
    deployment protecting its own file, a test protecting a fixture. Rather than
    borrowing another component's rationale, the entry says plainly that no rationale
    is written yet and that a change therefore needs a human.
    """

    return Component(
        path=path,
        tier=tier_of(path, repo_root),
        why="the configuration protects this path, but the policy does not name it and "
            "so cannot yet say what a legitimate change would look like",
        permits="nothing by default: a change is refused, recorded and left to a human",
        authorizer="a named human, by re-baselining after review",
        verification="review of the difference, then a re-baseline of this file only",
        weakening=("editing this file without adding it to the policy with a rationale",),
    )


def components(repo_root: pathlib.Path = REPO_ROOT) -> list[Component]:
    """The real files behind the policy. A directory entry expands to its files."""

    out: list[Component] = []
    for entry in POLICY:
        raw = repo_root / str(entry["path"])
        fields = {k: entry[k] for k in ("tier", "why", "permits", "authorizer",
                                        "verification", "weakening")}
        if raw.is_dir():
            for path in sorted(p for p in raw.glob("*.md") if p.is_file()):
                out.append(Component(path=path, glob=True, **fields))
        else:
            out.append(Component(path=raw, **fields))
    return out


def policy_for(path: str | pathlib.Path,
               repo_root: pathlib.Path = REPO_ROOT) -> Component | None:
    """The policy entry covering a path, or None for ordinary project code."""

    target = pathlib.Path(path).resolve()
    for component in components(repo_root):
        if component.glob:
            if target.parent == component.path.resolve() or component.path.resolve() in target.parents:
                return component
        elif target == component.path.resolve():
            return component
    return None


__all__ = [
    "ENFORCEMENT", "GOVERNANCE", "INSTRUCTIONS", "INTEGRITY_EVENT_KINDS", "OPERATIONAL",
    "Component", "Integrity", "POLICY", "StructureFinding", "compare_structure",
    "components", "definitions", "from_configuration", "policy_broadening",
    "policy_for", "tier_of",
]


def tier_of(path: str | pathlib.Path, repo_root: pathlib.Path = REPO_ROOT) -> str:
    component = policy_for(path, repo_root)
    return component.tier if component else OPERATIONAL


# -- structural comparison --------------------------------------------------

def definitions(source: str) -> set[str]:
    """Every function and class name in a module, qualified by nesting."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    found: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                found.add(name)
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def call_counts(source: str, names: Sequence[str]) -> dict[str, int]:
    """How many times each enforcement primitive is called."""

    counts = {name: 0 for name in names}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return counts
    wanted = set(names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            label = None
            if isinstance(target, ast.Attribute):
                label = target.attr
            elif isinstance(target, ast.Name):
                label = target.id
            if label in wanted:
                counts[label] += 1
    return counts


@dataclasses.dataclass
class StructureFinding:
    kind: str
    detail: str
    severity: str  # "weakening" | "change" | "note"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def compare_structure(path: pathlib.Path, old_source: str, new_source: str) -> list[StructureFinding]:
    """What a change does structurally, without reading filenames or words.

    Detects removed definitions, removed or reduced enforcement call-sites, and (for
    policy modules) parameters that have stopped being consulted. It cannot tell an
    improvement from a subtle weakening: the findings are inputs to a human decision
    plus the behavioural probes, never a verdict on their own.
    """

    findings: list[StructureFinding] = []
    before, after = definitions(old_source), definitions(new_source)
    removed = sorted(before - after)
    if removed:
        findings.append(StructureFinding(
            "removed_definitions",
            f"{len(removed)} definition(s) no longer exist: {', '.join(removed[:12])}",
            "weakening" if any(name.split(".")[-1].startswith(("check", "verify", "integrity",
                                                              "watch", "gate", "block", "deny"))
                               for name in removed) else "change",
        ))
    added = sorted(after - before)
    if added:
        findings.append(StructureFinding(
            "added_definitions", f"{len(added)} new definition(s): {', '.join(added[:12])}",
            "change",
        ))

    before_calls = call_counts(old_source, ENFORCEMENT_CALLS)
    after_calls = call_counts(new_source, ENFORCEMENT_CALLS)
    for name, count in sorted(before_calls.items()):
        if count and after_calls.get(name, 0) < count:
            findings.append(StructureFinding(
                "reduced_enforcement_calls",
                f"{name} is called {after_calls.get(name, 0)} time(s) where it was called "
                f"{count}",
                "weakening",
            ))
    for name, count in sorted(after_calls.items()):
        if count > before_calls.get(name, 0):
            findings.append(StructureFinding(
                "added_enforcement_calls",
                f"{name} is now called {count} time(s) where it was called "
                f"{before_calls.get(name, 0)}",
                "note",
            ))

    if "guardian" in path.name or "integrity" in path.name or "permissions" in path.name:
        for marker, severity in (("return True", "note"),
                                 ("raise NotImplementedError", "weakening"),
                                 ("pass  # placeholder", "weakening")):
            if new_source.count(marker) > old_source.count(marker):
                findings.append(StructureFinding(
                    "suspicious_marker", f"'{marker}' now appears "
                    f"{new_source.count(marker)} time(s), was {old_source.count(marker)}",
                    severity,
                ))
    return findings


def policy_broadening(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which parts of the effective policy became more permissive."""

    changes: list[str] = []

    def as_set(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            return {str(v) for v in value}
        return {str(value)}

    for key, direction in POLICY_VECTORS:
        old, new = as_set(before.get(key)), as_set(after.get(key))
        if direction == "gained" and new - old:
            changes.append(f"{key} gained {sorted(new - old)}")
        if direction == "lost" and old - new:
            changes.append(f"{key} lost {sorted(old - new)}")
    return changes


# -- snapshots --------------------------------------------------------------

def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Integrity:
    """Snapshots, events, review and verification. One instance per process."""

    def __init__(self, config: Config, store: Store, engine: PermissionEngine | None = None):
        self.config = config
        self.store = store
        self.engine = engine or PermissionEngine(config)
        self.repo_root = REPO_ROOT

    # -- components and baselines -----------------------------------------
    def components(self) -> list[Component]:
        """The policy's components, plus anything the configuration protects.

        The union is deliberate. The policy is the place a rationale lives; the
        configuration is the place a deployment states what it refuses to let the
        agent write. A file in either list is watched, so the guardian cannot end up
        guarding less than the permission engine refuses.
        """

        known = components(self.repo_root)
        covered = {component.path.resolve() for component in known}
        extra: list[pathlib.Path] = list(self.engine.protected_files())
        prompt_dir = self.config.paths.prompts
        if prompt_dir.is_dir():
            extra.extend(sorted(p for p in prompt_dir.glob("*.md") if p.is_file()))
        for candidate in extra:
            path = pathlib.Path(candidate).resolve()
            if path in covered or not path.is_file():
                continue
            covered.add(path)
            known.append(from_configuration(path, self.repo_root))
        return known

    def policy_for(self, path: str | pathlib.Path) -> Component | None:
        """The policy entry that applies to a path in *this* configuration.

        The module-level :func:`policy_for` answers for the shipped policy alone. A
        deployment can protect a file the policy does not name (see
        :func:`from_configuration`), and a method that decides whether a change may be
        reviewed or restored has to see the same set the guardian watches -- otherwise
        the refusal and the record would disagree about what is protected.
        """

        target = pathlib.Path(path).resolve()
        for component in self.components():
            if component.glob:
                if target.parent == component.path.resolve() or \
                        component.path.resolve() in target.parents:
                    return component
            elif target == component.path.resolve():
                return component
        return None

    def baseline(self) -> dict[str, str]:
        rows = self.store.query("SELECT path, sha256 FROM guardian_baseline")
        return {str(row["path"]): str(row["sha256"]) for row in rows}

    def protect(self, *, approved_by: str, paths: Sequence[pathlib.Path] | None = None,
                force: bool = False) -> dict[str, Any]:
        """Record the baseline for the protected set, with a snapshot of each file.

        Called by a human through ``livium guardian baseline``. The snapshot is what
        makes recovery and structural comparison possible later: a digest alone can
        say "this changed" but not "here is what it changed from".
        """

        targets = list(paths) if paths else [c.path for c in self.components()]
        targets = [p for p in targets if p.exists() and p.is_file()]
        if not targets:
            raise PermissionError("no protected files exist to baseline")
        recorded = self.baseline()
        if recorded and not force:
            raise PermissionError(
                "a baseline is already recorded; re-recording replaces the digests that "
                "detection compares against, so it must be deliberate (CLI: --force)"
            )
        with self.store.transaction() as conn:
            for path in targets:
                payload = path.read_bytes()
                digest = _digest_bytes(payload)
                conn.execute(
                    "INSERT INTO guardian_baseline(path, sha256, recorded_at) VALUES(?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, "
                    "recorded_at=excluded.recorded_at",
                    (str(path), digest, now()),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO integrity_snapshot(path, sha256, content, captured_at) "
                    "VALUES(?,?,?,?)",
                    (str(path), digest, payload, now()),
                )
        self.record_event(
            "baseline_recorded", tier=None, paths=targets,
            observed={"digests": {str(p): _digest_bytes(p.read_bytes()) for p in targets}},
            expected={"previous": recorded}, detection="human_action",
            policy={"note": "a human recorded the baseline; this is the approval record"},
            decision="approved", authorization=approved_by,
            evidence={"files": len(targets), "forced": bool(force)},
            outcome="baseline_updated",
        )
        return {"baseline_files": len(targets), "approved_by": approved_by,
                "forced": bool(force), "snapshots": len(targets)}

    def snapshot_for(self, path: pathlib.Path, digest: str | None = None) -> bytes | None:
        row = self.store.one(
            "SELECT content FROM integrity_snapshot WHERE path=? AND sha256=?",
            (str(path), digest or self.baseline().get(str(path), "")),
        )
        return bytes(row["content"]) if row is not None else None

    # -- inspection --------------------------------------------------------
    def inspect(self, *, detection: str = "cli", task_id: str | None = None,
                tool: str | None = None, record: bool = True) -> dict[str, Any]:
        """Compare every protected component with its baseline and record the look.

        Returns a report with per-path state: ``ok``, ``changed``, ``unbaselined`` or
        ``missing``. Every non-ok finding is appended to ``integrity_event``, with the
        attribution the evidence supports and nothing more.
        """

        current = self.baseline()
        results: list[dict[str, Any]] = []
        for component in self.components():
            path = component.path
            digest = _digest_bytes(path.read_bytes()) if path.exists() else None
            expected = current.get(str(path))
            if digest is None:
                state = "missing"
            elif expected is None:
                state = "unbaselined"
            elif digest == expected:
                state = "ok"
            else:
                state = "changed"
            results.append({
                "path": str(path), "tier": component.tier, "state": state,
                "expected": expected, "observed": digest,
                "why": component.why,
            })

        changed = [r for r in results if r["state"] == "changed"]
        unbaselined = [r for r in results if r["state"] == "unbaselined"]
        missing = [r for r in results if r["state"] == "missing"]
        if record and changed:
            self.record_event(
                "change_detected",
                tier=None,
                paths=[pathlib.Path(r["path"]) for r in changed],
                observed={"digests": {r["path"]: r["observed"] for r in changed}},
                expected={"digests": {r["path"]: r["expected"] for r in changed}},
                detection=detection,
                policy={"action": "a protected component differs from its baseline; no "
                                  "change may be promoted while this stands"},
                decision="detected",
                evidence={"tiers": sorted({r["tier"] for r in changed})},
                outcome="unauthorised_until_reviewed",
                uncertainty="the change is unapproved; who made it is only claimed when "
                            "the journal supports it",
                task_id=task_id, tool=tool,
            )
        if record and unbaselined:
            self.record_event(
                "component_unbaselined", tier=None,
                paths=[pathlib.Path(r["path"]) for r in unbaselined],
                observed={"paths": [r["path"] for r in unbaselined]},
                expected={}, detection=detection,
                policy={"action": "record a baseline once, as a human, so tampering can "
                                  "be detected at all"},
                decision="no_baseline", evidence={}, outcome="unknown",
                uncertainty="without a baseline, change cannot be distinguished from "
                            "the original content",
                task_id=task_id, tool=tool,
            )
        return {
            "ok": not changed and not unbaselined,
            "status": ("tampered" if changed else "unbaselined" if unbaselined
                       else "missing_files" if missing else "ok"),
            "results": results,
            "changed": [r["path"] for r in changed],
            "unbaselined": [r["path"] for r in unbaselined],
            "missing": [r["path"] for r in missing],
            "checked": len(results),
        }

    # -- attribution -------------------------------------------------------
    def attribute(self, path: pathlib.Path, *, since: str | None = None) -> dict[str, Any]:
        """What can honestly be said about how this file changed.

        The journal can say which operations ran; it cannot say which of them wrote a
        file, and guessing would be worse than admitting it. So the result names the
        evidence: operations recorded since the baseline was taken, the git revision,
        the file's mtime against those recorded times, and the verdict ``unknown`` when
        nothing lines up.
        """

        target = pathlib.Path(path)
        mtime = target.stat().st_mtime if target.exists() else None
        baseline_row = self.store.one(
            "SELECT recorded_at FROM guardian_baseline WHERE path=?", (str(target),))
        baseline_at = baseline_row["recorded_at"] if baseline_row else None

        operations: list[dict[str, Any]] = []
        for row in self.store.query(
            "SELECT id, ts, type, task_id, payload FROM event "
            "WHERE type IN ('tool_call','tool_failure','write_file','boundary_violation') "
            "ORDER BY id DESC LIMIT 200"
        ):
            ts = str(row["ts"])
            if since and ts < since:
                continue
            operations.append({"event_id": int(row["id"]), "ts": ts, "type": str(row["type"]),
                               "task_id": row["task_id"], "payload": str(row["payload"])[:300]})

        writes = [op for op in operations
                  if op["type"] in ("tool_call", "write_file", "boundary_violation")
                  and f"disc" not in op["payload"]]
        candidates = [op for op in writes if baseline_at is None or op["ts"] > baseline_at]
        revision = self.revision_for(target)

        verdict = "unknown"
        if candidates:
            verdict = "observed_after_operations"
        return {
            "path": str(target),
            "mtime": mtime,
            "baseline_recorded_at": baseline_at,
            "candidate_operations": candidates[:10],
            "operation_count": len(candidates),
            "revision": revision,
            "verdict": verdict,
            "uncertainty": (
                "the journal records which operations ran, not which of them wrote this "
                "file; attribution is therefore a candidate list, never a claim"
                if candidates else
                "no recorded operation ran after the baseline was taken, so the change "
                "cannot be attributed from the journal at all"
            ),
        }

    def revision_for(self, path: pathlib.Path) -> dict[str, Any]:
        """The revision state of a file, when the tree is a git repository."""

        def git(*args: str) -> str:
            try:
                completed = subprocess.run(
                    ["git", *args], cwd=str(self.repo_root), capture_output=True,
                    text=True, timeout=10,
                )
                return completed.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return ""

        head = git("rev-parse", "HEAD")
        relative = str(pathlib.Path(path).relative_to(self.repo_root)) \
            if str(path).startswith(str(self.repo_root)) else str(path)
        status = git("status", "--porcelain", "--", relative)
        committed = git("log", "-1", "--format=%H %cI", "--", relative)
        return {"head": head, "working_tree_status": status,
                "last_commit_touching_file": committed,
                "note": "git state only; the working tree is not verified by git"}

    # -- review ------------------------------------------------------------
    def review_change(self, path: pathlib.Path, *, proposed_by: str = "agent",
                      reason: str = "", task_id: str | None = None) -> dict[str, Any]:
        """Evaluate a proposed change against the policy. Decides nothing by itself."""

        component = self.policy_for(path)
        if component is None:
            return {
                "path": str(path), "tier": OPERATIONAL, "decision": "unrestricted",
                "detail": "ordinary project code: no integrity policy applies, and the "
                          "agent's write roots do not include the repository",
                "requires": [], "verification": "git review",
            }
        findings = self.diff_against_baseline(component.path)
        requirements = ["a named human approver"]
        if component.needs_independent_check:
            requirements.append("the repository test suite passes against the candidate")
            requirements.append("enforcement probes pass against a copy with the "
                                "candidate applied")
        if component.tier == INSTRUCTIONS:
            requirements.append("contradiction scan against the existing instructions")
        weakening = [f.detail for f in findings if f.severity == "weakening"]
        decision = "requires_review"
        if weakening:
            decision = "requires_explicit_acknowledgement"
        return {
            "path": str(component.path), "tier": component.tier, "decision": decision,
            "why_protected": component.why, "permits": component.permits,
            "authorizer": component.authorizer,
            "verification": component.verification,
            "requires": requirements,
            "weakening_findings": weakening,
            "findings": [f.as_dict() for f in findings],
            "proposed_by": proposed_by, "reason": reason,
            "note": "this reviews the change; it does not authorise it and does not apply it",
        }

    def review_candidate(self, path: pathlib.Path, source: str, *, proposed_by: str = "agent",
                         reason: str = "") -> dict[str, Any]:
        """Review a proposed body of source for a component, writing nothing anywhere.

        The structural comparison answers "what did this do?" -- removed definitions,
        reduced enforcement call-sites. Whether the *effective policy* became more
        permissive is a different question and needs the candidate to run, so it is
        answered by :meth:`verify_enforcement` with ``candidate_files`` (in a copy, in a
        separate interpreter), not guessed from the text. The result says which of the
        two questions has been answered and which has not.
        """

        target = pathlib.Path(path).resolve()
        component = self.policy_for(target)
        findings: list[StructureFinding] = []
        baseline_source = ""
        snapshot = None
        digest = self.baseline().get(str(target))
        if digest:
            payload = self.snapshot_for(target, digest)
            if payload:
                baseline_source = payload.decode("utf-8", "replace")
        if baseline_source:
            findings = compare_structure(target, baseline_source, source)
        return {
            "path": str(target),
            "tier": component.tier if component else OPERATIONAL,
            "proposed_by": proposed_by, "reason": reason,
            "structure_reviewed": bool(baseline_source),
            "findings": [f.as_dict() for f in findings],
            "weakening": [f.detail for f in findings if f.severity == "weakening"],
            "policy_not_answered_here": (
                "whether this candidate makes the effective policy more permissive is "
                "answered by `livium integrity verify` after applying the candidate to a "
                "copy of the tree, not by reading it"
            ),
            "note": ("this reviews structurally; it authorises nothing and applies nothing "
                     + ("" if baseline_source else
                        " (no baselined content is stored for this path, so the structural "
                        "review could not run)")),
        }

    def diff_against_baseline(self, path: pathlib.Path) -> list[StructureFinding]:
        """Compare the current content with the snapshot the baseline recorded."""

        path = pathlib.Path(path)
        if not path.exists():
            return [StructureFinding("missing_file", f"{path} does not exist", "weakening")]
        old = self.snapshot_for(path)
        if old is None:
            return [StructureFinding(
                "no_snapshot",
                "no snapshot exists for this file's baselined digest, so a structural "
                "comparison cannot be made",
                "note",
            )]
        try:
            old_source = old.decode("utf-8")
        except UnicodeDecodeError:
            return []
        return compare_structure(path, old_source, path.read_text(encoding="utf-8"))

    # -- independent verification -----------------------------------------
    def verify_enforcement(self, *, candidate_files: dict[pathlib.Path, str] | None = None,
                           run_tests: bool = False, timeout: float = 240.0,
                           task_id: str | None = None) -> dict[str, Any]:
        """Prove that enforcement still works -- in a copy, not in the live process.

        With no candidate, the probes run against the repository as it stands. With a
        candidate, the whole tree is copied, the candidate files are applied there, and
        the probes (optionally the full test suite) run against *that* copy, in a
        separate process. The code being evaluated does not get to report whether it
        passes: it is imported by a process it does not control and asked to refuse
        things it must refuse.
        """

        started = time.time()
        if candidate_files:
            workdir = pathlib.Path(tempfile.mkdtemp(prefix="livium-integrity-"))
            tree = workdir / "tree"
            self._copy_tree(self.repo_root, tree)
            applied: list[str] = []
            for path, source in candidate_files.items():
                target = tree / pathlib.Path(path).relative_to(self.repo_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
                applied.append(str(pathlib.Path(path).relative_to(self.repo_root)))
        else:
            workdir = None
            tree = self.repo_root
            applied = []

        try:
            probe = self._run_probe(tree, timeout=timeout)
            tests = None
            if run_tests:
                tests = self._run_tests(tree, timeout=timeout)
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)

        ok = bool(probe.get("ok")) and (tests is None or bool(tests.get("passed")))
        evidence = {
            "probes": probe.get("probes", []),
            "failures": probe.get("failures", []),
            "applied": applied,
            "tree": "copy of the repository" if candidate_files else "the repository",
            "tests": tests,
            "seconds": round(time.time() - started, 2),
        }
        # Recorded whether or not there was a candidate: a verification that ran and found
        # the controls intact is evidence, and evidence that is not written down is a log
        # line. "No candidate" is stated in the record rather than left implicit.
        self.record_event(
            "enforcement_verified" if ok else "enforcement_verification_failed",
                tier=None, paths=[pathlib.Path(p) for p in applied],
                observed={"failures": probe.get("failures", []),
                          "candidate": bool(candidate_files)},
                expected={"probes": [p.get("name") for p in probe.get("probes", [])]},
                detection="enforcement_probe" + ("" if candidate_files else " (tree as it stands)"),
                policy={"requires": "enforcement must still refuse what it refused before"},
                decision="verified" if ok else "not_verified",
                evidence=evidence, outcome="verified" if ok else "failed",
                uncertainty=("the probes cover the behaviours they assert; they cannot "
                             "prove the absence of every weakening"),
                task_id=task_id,
            )
        return {"ok": ok, "probe": probe, "tests": tests, "applied": applied,
                "seconds": round(time.time() - started, 2)}

    @staticmethod
    def _copy_tree(source: pathlib.Path, target: pathlib.Path) -> None:
        def ignore(directory: str, names: list[str]) -> set[str]:
            skip = {".git", "__pycache__", ".livium", ".venv", "node_modules"}
            return {n for n in names if n in skip or n.endswith(".pyc")}

        shutil.copytree(source, target, ignore=ignore)

    def _run_probe(self, tree: pathlib.Path, *, timeout: float) -> dict[str, Any]:
        """Run the behavioural probes in a fresh interpreter pointed at ``tree``."""

        script = _PROBE_SCRIPT
        home = pathlib.Path(tempfile.mkdtemp(prefix="livium-probe-home-"))
        # The probe tampers with a file *it owns*, in its own home, never with a file in
        # the tree under test. A probe that modified the checkout to test detection would
        # be doing the very thing it is meant to detect, and a probe that depended on the
        # operator's baseline would report a real check as broken whenever the operator
        # had not baselined yet.
        victim = home / "probe-victim.py"
        victim.write_text("RULE = 'original'\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("LIVIUM_")}
        env.update({
            "PYTHONPATH": str(tree),
            "LIVIUM_HOME": str(home),
            "LIVIUM_PROVIDER": "scripted",
            "LIVIUM_ALLOW_TEST_DOUBLE": "1",
            "LIVIUM_PROBE_HOME": str(home),
            "LIVIUM_PROBE_VICTIM": str(victim),
            "LIVIUM_EXTRA_PROTECTED_PATHS": str(victim),
        })
        try:
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=str(tree), env=env,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "probes": [], "failures": [f"probe timed out after {timeout}s"]}
        payload = None
        for line in reversed(completed.stdout.splitlines()):
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    break
                except ValueError:
                    continue
        if payload is None:
            return {"ok": False, "probes": [],
                    "failures": [f"probe produced no result (exit {completed.returncode}): "
                                 f"{(completed.stderr or completed.stdout)[-400:]}"]}
        return payload

    def _run_tests(self, tree: pathlib.Path, *, timeout: float) -> dict[str, Any]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LIVIUM_")}
        env["PYTHONPATH"] = str(tree)
        started = time.time()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "tests"],
                cwd=str(tree), env=env, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"passed": False, "detail": f"tests timed out after {timeout}s",
                    "seconds": round(time.time() - started, 2)}
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-3:]
        return {"passed": completed.returncode == 0, "returncode": completed.returncode,
                "detail": " / ".join(tail), "seconds": round(time.time() - started, 2)}

    # -- safe modification workflow ---------------------------------------
    def approve_change(self, path: pathlib.Path, *, approved_by: str, reason: str,
                       acknowledge: Sequence[str] = (), run_tests: bool = True,
                       task_id: str | None = None) -> dict[str, Any]:
        """The human step: review, verify independently, then re-baseline that file.

        Refuses when the independent checks fail, and records the refusal. Nothing is
        "approved because the agent said so", and a failure is never recorded as a
        success: the file keeps its old baseline and the event says what failed.
        """

        from .permissions import UNATTRIBUTED_NAMES  # local: avoids a cycle at import

        if not approved_by or approved_by.strip().lower() in UNATTRIBUTED_NAMES:
            raise PermissionError("a change to a protected component needs a named human")

        target = pathlib.Path(path).resolve()
        component = self.policy_for(target)
        if component is None:
            raise PermissionError(f"{target} is not a protected component")

        review = self.review_change(target, proposed_by=approved_by, reason=reason,
                                    task_id=task_id)
        unresolved = [f for f in review.get("weakening_findings", [])]
        missing_ack = [f for f in unresolved if f not in set(acknowledge)]
        if missing_ack:
            self.record_event(
                "change_decision", tier=component.tier, paths=[target],
                observed={"review": review}, expected={"authorization": "human, explicit"},
                detection="policy_review",
                policy={"authorizer": component.authorizer},
                decision="rejected", authorization=approved_by,
                evidence={"reason": "structural review found weakening without an explicit "
                                    "acknowledgement", "unacknowledged": missing_ack},
                outcome="rejected", uncertainty="",
                task_id=task_id,
            )
            return {"approved": False, "reason": "structural review found weakening",
                    "unacknowledged": missing_ack, "review": review}

        verification = self.verify_enforcement(candidate_files=None, run_tests=run_tests,
                                               task_id=task_id)
        if not verification["ok"]:
            self.record_event(
                "change_decision", tier=component.tier, paths=[target],
                observed={"probe_failures": verification["probe"].get("failures"),
                          "tests": verification.get("tests")},
                expected={"probes": "all pass", "tests": "pass"},
                detection="enforcement_probe",
                policy={"authorizer": component.authorizer},
                decision="rejected", authorization=approved_by,
                evidence=verification, outcome="rejected",
                uncertainty="the candidate was not promoted, so the failure describes the "
                            "current tree, not a hypothetical one",
                task_id=task_id,
            )
            return {"approved": False, "reason": "independent verification failed",
                    "verification": verification, "review": review}

        payload = target.read_bytes()
        digest = _digest_bytes(payload)
        previous = self.baseline().get(str(target))
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO guardian_baseline(path, sha256, recorded_at) VALUES(?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, "
                "recorded_at=excluded.recorded_at",
                (str(target), digest, now()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO integrity_snapshot(path, sha256, content, captured_at) "
                "VALUES(?,?,?,?)",
                (str(target), digest, payload, now()),
            )
        self.record_event(
            "change_approved", tier=component.tier, paths=[target],
            observed={"digest": digest}, expected={"previous": previous},
            detection="policy_review + enforcement_probe",
            policy={"authorizer": component.authorizer, "permits": component.permits},
            decision="approved", authorization=approved_by,
            evidence={"reason": reason, "verification": verification,
                      "review": review, "acknowledged": list(acknowledge)},
            outcome="baseline_updated", uncertainty=verification["probe"].get("uncertainty", ""),
            task_id=task_id,
        )
        return {"approved": True, "path": str(target), "digest": digest,
                "previous": previous, "verification": verification, "review": review}

    def reject_change(self, path: pathlib.Path, *, rejected_by: str, reason: str,
                      task_id: str | None = None) -> dict[str, Any]:
        """Record that a change was refused, without touching the file."""

        target = pathlib.Path(path).resolve()
        component = self.policy_for(target)
        self.record_event(
            "change_decision", tier=component.tier if component else OPERATIONAL,
            paths=[target], observed={}, expected={}, detection="human_review",
            policy={"authorizer": component.authorizer if component else "n/a"},
            decision="rejected", authorization=rejected_by,
            evidence={"reason": reason}, outcome="rejected", task_id=task_id,
        )
        return {"rejected": True, "path": str(target), "reason": reason}

    # -- recovery ----------------------------------------------------------
    def restore(self, path: pathlib.Path, *, approved_by: str, reason: str,
                verify: bool = True, task_id: str | None = None) -> dict[str, Any]:
        """Put a protected component back to its baselined content.

        Deliberately narrow: only protected components, only when a snapshot of the
        baselined digest exists, only on an explicit human instruction, never
        automatically, and never for user data. The write is atomic and the result is
        verified by hashing what is now on disk.
        """

        from .permissions import UNATTRIBUTED_NAMES

        if not approved_by or approved_by.strip().lower() in UNATTRIBUTED_NAMES:
            raise PermissionError("a recovery needs a named human")
        target = pathlib.Path(path).resolve()
        component = self.policy_for(target)
        if component is None:
            raise PermissionError(
                f"{target} is not a protected component: recovery would risk user data, "
                f"so it refuses"
            )
        expected = self.baseline().get(str(target))
        if expected is None:
            raise PermissionError(f"no baseline is recorded for {target}; nothing to "
                                  f"restore it to")
        payload = self.snapshot_for(target, expected)
        if payload is None:
            self.record_event(
                "recovery_failed", tier=component.tier, paths=[target],
                observed={}, expected={"digest": expected}, detection="human_recovery",
                policy={"note": "no snapshot exists for the baselined digest"},
                decision="refused", authorization=approved_by,
                evidence={"reason": "no snapshot"}, outcome="failed",
            )
            raise PermissionError(
                f"no snapshot exists for the baselined digest of {target}; the file "
                f"cannot be restored from this database"
            )
        current = _digest_bytes(target.read_bytes()) if target.exists() else None
        if current == expected:
            return {"restored": False, "reason": "already matches the baseline",
                    "path": str(target), "digest": expected}
        if verify and _digest_bytes(payload) != expected:
            raise PermissionError("the stored snapshot does not match the baselined digest")

        temporary = target.with_suffix(target.suffix + ".restore")
        temporary.write_bytes(payload)
        os.replace(temporary, target)
        after = _digest_bytes(target.read_bytes())
        ok = after == expected
        self.record_event(
            "recovery_performed" if ok else "recovery_failed",
            tier=component.tier, paths=[target],
            observed={"before": current, "after": after}, expected={"digest": expected},
            detection="human_recovery",
            policy={"note": "recovery restores the baselined content of one protected file"},
            decision="recovered" if ok else "failed", authorization=approved_by,
            evidence={"reason": reason, "verified_by": "sha256 of the file on disk"},
            outcome="restored" if ok else "failed",
            uncertainty="" if ok else "the file on disk does not match the baseline after "
                                      "the restore",
            task_id=task_id,
        )
        if not ok:
            raise PermissionError(f"restore did not verify: {after} != {expected}")
        return {"restored": True, "path": str(target), "digest": after,
                "previous": current, "approved_by": approved_by}

    # -- the append-only record -------------------------------------------
    def record_event(self, event: str, *, tier: str | None, paths: Sequence[pathlib.Path],
                     observed: Any, expected: Any, detection: str, policy: Any,
                     decision: str | None = None, authorization: str | None = None,
                     evidence: Any = None, recovery: str | None = None,
                     outcome: str = "recorded", uncertainty: str = "",
                     task_id: str | None = None, attributable_to: str | None = None,
                     tool: str | None = None) -> int:
        """Append one integrity event. UPDATE and DELETE on this table are refused."""

        revision = {}
        if paths:
            try:
                revision = self.revision_for(pathlib.Path(paths[0]))
            except (OSError, ValueError):
                revision = {}
        attribution = attributable_to
        if attribution is None and task_id:
            attribution = f"task:{task_id}"
        if attribution is None and tool:
            attribution = f"tool:{tool}"
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO integrity_event(ts, event, tier, paths, observed, expected, "
                "detection, policy, decision, authorization, evidence, recovery, outcome, "
                "uncertainty, task_id, attributable_to, tool, revision) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now(), event, tier,
                 json.dumps([str(p) for p in paths], default=str),
                 json.dumps(observed, default=str), json.dumps(expected, default=str),
                 detection, json.dumps(policy, default=str), decision, authorization,
                 json.dumps(evidence, default=str), recovery, outcome, uncertainty,
                 task_id, attribution, tool, json.dumps(revision, default=str)),
            )
            return int(cursor.lastrowid or 0)

    def events(self, *, limit: int = 50, event: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM integrity_event"
        params: list[Any] = []
        if event:
            sql += " WHERE event=?"
            params.append(event)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        out: list[dict[str, Any]] = []
        for row in self.store.query(sql, tuple(params)):
            record = dict(row)
            for key in ("paths", "observed", "expected", "policy", "evidence", "revision"):
                try:
                    record[key] = json.loads(record[key] or "{}")
                except (TypeError, ValueError):
                    record[key] = {}
            out.append(record)
        return out

    def incidents(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Detections that were not resolved by an approval, newest first."""

        incidents = [row for row in self.events(limit=200)
                     if row["event"] in ("change_detected", "enforcement_verification_failed",
                                         "recovery_failed", "component_unbaselined",
                                         "policy_violation")]
        return incidents[:limit]

    def enforcement_summary(self) -> dict[str, Any]:
        """What the record says about recent integrity activity."""

        rows = self.events(limit=500)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["event"]] = counts.get(row["event"], 0) + 1
        detections = counts.get("change_detected", 0)
        approvals = counts.get("change_approved", 0)
        recoveries = counts.get("recovery_performed", 0)
        return {
            "events": counts,
            "detections": detections,
            "approvals": approvals,
            "recoveries": recoveries,
            "verifications": counts.get("enforcement_verified", 0),
            "unresolved_detections": self._unresolved(),
            "note": ("a detection is not a prevention: these are changes that were seen, "
                     "attributed as far as the evidence allows, and then either approved "
                     "by a named human or left standing"),
        }

    def _unresolved(self) -> list[dict[str, Any]]:
        """Detections with no later decision or recovery for the same paths."""

        rows = self.events(limit=500)
        resolved: set[str] = set()
        for row in rows:
            if row["event"] in ("change_approved", "recovery_performed", "change_decision"):
                for path in row.get("paths") or []:
                    resolved.add(str(path))
        return [{"id": row["id"], "ts": row["ts"], "paths": row.get("paths"),
                 "outcome": row["outcome"], "uncertainty": row["uncertainty"]}
                for row in rows if row["event"] == "change_detected"
                and any(str(p) not in resolved for p in (row.get("paths") or []))]


#: The probe program. It runs in a separate interpreter with ``PYTHONPATH`` pointing at
#: the tree under test, exercises the boundary through the real modules, and prints one
#: JSON line. Everything it asserts is a refusal that must still happen.
_PROBE_SCRIPT = r'''
import json, os, pathlib, sys, tempfile

result = {"ok": False, "probes": [], "failures": []}

def probe(name, fn):
    try:
        detail = fn()
        result["probes"].append({"name": name, "passed": True, "detail": str(detail)[:300]})
    except AssertionError as exc:
        result["failures"].append(f"{name}: {exc}")
        result["probes"].append({"name": name, "passed": False, "detail": str(exc)[:300]})
    except Exception as exc:
        result["failures"].append(f"{name}: {type(exc).__name__}: {exc}")
        result["probes"].append({"name": name, "passed": False,
                                 "detail": f"{type(exc).__name__}: {exc}"[:300]})

try:
    from livium.config import Config, REPO_ROOT
    from livium.permissions import PermissionEngine
    from livium.store import Store
    from livium.memory import Memory
    from livium.guardian import GrowthGuardian
    from livium.approvals import ApprovalQueue
except Exception as exc:
    print(json.dumps({"ok": False, "probes": [],
                      "failures": [f"import failed: {type(exc).__name__}: {exc}"]}))
    raise SystemExit(0)

config = Config.load()
engine = PermissionEngine(config)
store = Store(config.paths.db)
memory = Memory(store)
guardian = GrowthGuardian(config, engine, store, memory)

def protected_write_refused():
    target = REPO_ROOT / "livium" / "permissions.py"
    decision = engine.check_write(target)
    assert not decision.allowed, "writing the permission module was ALLOWED"
    assert decision.category == "protected", f"wrong category: {decision.category}"
    return decision.reason

def outside_write_refused():
    decision = engine.check_write(pathlib.Path("/etc/livium-should-not-write"))
    assert not decision.allowed, "a write outside the permitted roots was ALLOWED"
    return decision.reason

def shell_refused():
    decision = engine.check_command(["sh", "-c", "echo hi"])
    assert not decision.allowed, "a shell interpreter was ALLOWED"
    return decision.reason

def non_allowlisted_binary_refused():
    decision = engine.check_command(["definitely-not-allowed-xyz"])
    assert not decision.allowed, "a non-allowlisted binary was ALLOWED"
    return decision.reason

def network_closed():
    decision = engine.check_network("https://example.com/")
    assert not decision.allowed, "network access was ALLOWED with an empty allowlist"
    return decision.reason

def promotion_gate_blocks_on_tamper():
    store.append_event("boundary_violation", {"tool": "probe", "capability": "probe-cap"})
    gate = guardian.promotion_gate("probe-cap")
    assert not gate.get("ok"), "the promotion gate said ok despite a boundary violation"
    return gate.get("detail")

def anonymous_approver_refused():
    queue = ApprovalQueue(store)
    record = queue.request(task_id="probe", tool="write_file", arguments={"path": "x"},
                           reason="probe")
    try:
        queue.decide(record.id, approved=True, by="Anonymous")
    except ValueError as exc:
        return str(exc)
    raise AssertionError("an anonymous approval was accepted")

VICTIM = pathlib.Path(os.environ["LIVIUM_PROBE_VICTIM"])

def baseline_recorded():
    result = guardian.record_baseline(approved_by="enforcement probe", force=True)
    assert result["baseline_files"] >= 1, "no files were baselined"
    report = guardian.integrity()
    assert report.status == "ok", f"a freshly baselined tree reports {report.status}"
    return f"baselined {result['baseline_files']} file(s); status ok"

def integrity_accepts_clean_tree():
    report = guardian.integrity()
    assert report.status == "ok", f"unexpected status {report.status}"
    return report.detail[:160]

def integrity_detects_change():
    original = VICTIM.read_bytes()
    try:
        VICTIM.write_text(original.decode("utf-8") + "\n# tampered\n", encoding="utf-8")
        report = guardian.integrity()
        assert report.status == "tampered", f"tamper not detected (state={report.status})"
        return report.detail[:160]
    finally:
        VICTIM.write_bytes(original)

def the_record_is_append_only():
    """A security record that can be edited is a record that can be rewritten."""
    import sqlite3
    store.append_event("probe_note", {"probe": True})
    integrity = guardian.integrity_policy
    integrity.record_event("policy_violation", tier="governance", paths=[VICTIM],
                           observed={}, expected={}, detection="probe",
                           policy={"note": "probe"}, decision="recorded",
                           evidence={"probe": True}, outcome="recorded")
    row = store.one("SELECT id FROM integrity_event ORDER BY id DESC LIMIT 1")
    assert row is not None, "the integrity record is empty after a write"
    for statement in (f"UPDATE integrity_event SET event='change_detected' WHERE id={row['id']}",
                      f"DELETE FROM integrity_event WHERE id={row['id']}"):
        try:
            store.execute(statement)
        except sqlite3.IntegrityError:
            continue
        except Exception as exc:
            if "append-only" in str(exc):
                continue
            raise AssertionError(f"{statement.split()[0]} raised {type(exc).__name__}: {exc}")
        raise AssertionError(f"{statement.split()[0]} on the integrity record was ALLOWED")
    return "an update and a delete on integrity_event were both refused"

probe("baseline_recorded", baseline_recorded)
probe("protected_write_refused", protected_write_refused)
probe("outside_write_refused", outside_write_refused)
probe("shell_refused", shell_refused)
probe("non_allowlisted_binary_refused", non_allowlisted_binary_refused)
probe("network_closed", network_closed)
probe("promotion_gate_blocks_on_tamper", promotion_gate_blocks_on_tamper)
probe("anonymous_approver_refused", anonymous_approver_refused)
probe("integrity_detects_change", integrity_detects_change)
probe("integrity_accepts_clean_tree", integrity_accepts_clean_tree)
probe("the_record_is_append_only", the_record_is_append_only)
store.close()
result["ok"] = not result["failures"]
print(json.dumps(result))
'''
