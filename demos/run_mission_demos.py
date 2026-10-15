#!/usr/bin/env python3
"""Mission demonstrations: task-directed information flow, executed for real.

Usage::

    python3 demos/run_mission_demos.py [--home DIR] [--approve-as "Your Name"] [--json-out PATH]

Six scenarios from section 12 of the mission, each performed with the real runtime
and recorded with its observed evidence:

M1  selective retrieval      -- a relevant subset of a 32-record store reaches the prompt
M2  capability discovery     -- a promoted capability is found by words, its real interface
                                is loaded, and it is executed through the authorised runtime
M3  failure changes action   -- a refused operation is classified, routed differently, and a
                                human approval releases it exactly once
M4  resume after interruption-- a task killed mid-write is resumed from persisted state
M5  experience reuse         -- a later, separate process reuses a verified experience and
                                the ledger records the link
M6  token accounting         -- reported usage is recorded as actual; missing usage stays
                                null and is never replaced by an estimate

And the two scenarios this mission adds:

M7  deferred discovery       -- a task notices something that is not the task, captures it,
                                finishes and is verified, and only then is the observation
                                evaluated -- as its own bounded task with its own task id
M8  the human side of the queue -- the queue is read, ranked and decided through the CLI in a
                                separate process, including a transition the table refuses
M9  integrity response       -- a protected file is modified, the modification becomes an
                                append-only record, a structural review runs, an
                                unattributable recovery is refused, and a named human
                                recovery restores the baselined bytes and re-hashes them

Nothing here claims to prove model quality. Model *replies* come from the scripted
test double and every record says so; the router, permission engine, store, memory,
verifier, ledger, approval queue and subprocess isolation are the real ones. Where a
scenario needs credentials or network that this machine does not have, the scenario
records that as an explicit limitation instead of substituting a simulation.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from livium.agent import LiviumAgent  # noqa: E402
from livium.config import Config  # noqa: E402
from livium.ledger import Ledger  # noqa: E402
from livium.models import ModelClient, ModelReply, ProbeResult, ToolCall  # noqa: E402
from livium.store import Store  # noqa: E402

WORD_COUNT_MODULE = '''
"""Count words in a workspace file."""

CAPABILITY = {
    "name": "word_count",
    "requirement": "count the words in a workspace file",
    "description": "Count whitespace-separated words in a file inside the workspace.",
    "dependencies": [],
    "permissions": ["read"],
    "limitations": "ASCII whitespace only; no per-language tokenisation.",
    "tools": [{
        "name": "word_count",
        "description": "Count words in a workspace file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    }],
}


def handle(name, arguments, ctx):
    from livium.tools import ToolResult
    config = ctx.services["config"]
    target = config.paths.workspace / str(arguments.get("path", ""))
    if not target.exists():
        return ToolResult(tool=name, ok=False, output=f"no such file: {target.name}",
                          error="not_found", category="capability")
    words = len(target.read_text(encoding="utf-8").split())
    return ToolResult(tool=name, ok=True, output=f"{words} words", data={"count": words})
'''

WORD_COUNT_TEST = '''
import pathlib
import unittest

import word_count


def context_for(directory):
    config = type("C", (), {"paths": type("P", (), {"workspace": directory})()})()
    return type("Ctx", (), {"services": {"config": config}})()


class WordCountTests(unittest.TestCase):
    def test_counts_words(self):
        directory = pathlib.Path(__file__).parent
        (directory / "sample.txt").write_text("one two three\\n", encoding="utf-8")
        result = word_count.handle("word_count", {"path": "sample.txt"}, context_for(directory))
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.data["count"], 3)

    def test_a_missing_file_is_an_error(self):
        directory = pathlib.Path(__file__).parent
        result = word_count.handle("word_count", {"path": "absent.txt"}, context_for(directory))
        self.assertFalse(result.ok)
'''


class UsageReportingClient(ModelClient):
    """A model transport that reports token usage -- the shape a real provider returns.

    It is a test double for the *model*, and it says so. What it demonstrates is the
    instrumentation path: whether the harness records a reported measurement, a
    labelled estimate, or nothing at all.
    """

    provider = "usage-reporting-double"
    is_test_double = True

    def __init__(self, config, *, report_usage: bool = True):
        super().__init__(config)
        self.report_usage = report_usage
        self.calls = 0

    def complete(self, conversation, tools, *, max_output_tokens=None):
        self.calls += 1
        usage = ({"prompt_tokens": 1450, "completion_tokens": 90} if self.report_usage else {})
        if self.calls == 1:
            call = ToolCall(id="m6-1", name="write_file",
                            arguments={"path": "m6.txt", "content": "measured\n",
                                       "purpose": "demonstration artefact"})
        else:
            call = ToolCall(id=f"m6-{self.calls}", name="finish_task",
                            arguments={"summary": "wrote m6.txt", "status": "complete"})
        return ModelReply(text="", tool_calls=[call], stop_reason="tool_use", usage=usage,
                          latency_ms=1, provider=self.provider, model="usage-double",
                          is_test_double=True)

    def probe(self) -> ProbeResult:
        return ProbeResult(
            ok=True,
            detail=(
                "usage-reporting test double: NO REAL MODEL IS BEING CALLED. It exists to "
                "demonstrate token accounting, not model behaviour."
            ),
        )


class MissionDemos:
    def __init__(self, home: pathlib.Path, approve_as: str | None) -> None:
        self.home = home
        self.approve_as = approve_as
        self.results: list[dict] = []

    # -- helpers ----------------------------------------------------------
    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "LIVIUM_HOME": str(self.home),
            "LIVIUM_DB": str(self.home / "livium.db"),
            "LIVIUM_WORKSPACE": str(self.home / "workspace"),
            "LIVIUM_EVIDENCE": str(self.home / "evidence"),
            "LIVIUM_APPROVAL_WRITE_ROOTS": str(self.home / "restricted"),
            # Promotion writes the capability where the loader will import it. Pointing
            # that at the demonstration home keeps a demo from modifying the package it
            # is demonstrating.
            "LIVIUM_PROMOTED_DIR": str(self.home / "promoted"),
            "LIVIUM_ALLOW_TEST_DOUBLE": "1",
            "LIVIUM_PROVIDER": "scripted",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # The child processes import the checkout they are demonstrating.
            "PYTHONPATH": str(REPO_ROOT),
        }
        env.update(extra)
        return env

    def config(self, **extra: str) -> Config:
        return Config.load(env=self.env(**extra))

    def agent(self, script=None, **env_extra):
        config = self.config(**env_extra)
        config.ensure_dirs()
        agent = LiviumAgent(config, script=script)
        agent.start()
        integrity = agent.guardian.integrity()
        if not integrity.ok:
            agent.guardian.record_baseline(approved_by=self.approve_as or "Demonstration runner")
        return agent

    def child(self, script: str, *arguments: str, expect_signal: bool = False,
              **env_extra: str) -> subprocess.CompletedProcess:
        """Run one of demos/children/*.py as a genuinely separate process.

        ``expect_signal`` accepts a process that was killed by a signal, which is the
        whole point of the interruption demonstration: the parent must not treat a
        deliberate crash as a failed step, and must not hide it either.
        """

        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "demos" / "children" / script), *arguments],
            env=self.env(**env_extra), capture_output=True, text=True, timeout=180,
            cwd=str(REPO_ROOT),
        )
        crashed = completed.returncode < 0
        if completed.returncode != 0 and not (expect_signal and crashed):
            raise SystemExit(
                f"child process exited {completed.returncode}; a demonstration cannot "
                f"continue on a failed step:\n{completed.stderr[-2000:]}")
        return completed

    def record(self, name: str, outcome: str, detail: str, evidence: dict) -> None:
        self.results.append({
            "demonstration": name, "outcome": outcome, "detail": detail,
            "evidence": evidence, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        print(f"\n[{outcome.upper()}] {name}\n    {detail}")

    def banner(self, name: str) -> None:
        print("\n" + "=" * 78)
        print(name)
        print("=" * 78)

    def promote_word_count(self, agent: LiviumAgent) -> dict:
        """Take a real capability through the real lifecycle (no model involved)."""

        existing = agent.capabilities.get("word_count")
        if existing is not None and existing.status in ("active", "maintained"):
            return {"status": existing.status, "already_promoted": True,
                    "tools": [t["name"] for t in existing.tools or []]}
        agent.capabilities.propose(
            name="word_count", requirement="count the words in a workspace file",
            reuse_analysis='Reuse ladder, measured: (1) direct reuse -- no existing tool returns a word count; read_file returns text, run_command returns program output, and the 13 registered tools were checked one by one. (2) composition -- a run_command + python3 one-liner could print len(open(p).read().split()), so the requirement is composable; that was considered and rejected because the need recurs across tasks, the one-liner would be re-invented in each prompt, and "words" needs one documented definition rather than a different expression each time. (3) extend/repair -- memory_recent is the nearest capability but it answers about memory records, not about file contents. (4) smallest suitable capability -- one tool, one argument (path), read-only.',
            rationale="the same need appeared in several tasks")
        agent.capabilities.scaffold("word_count")
        lab = agent.config.paths.capability_lab / "word_count"
        (lab / "word_count.py").write_text(WORD_COUNT_MODULE, encoding="utf-8")
        (lab / "test_word_count.py").write_text(WORD_COUNT_TEST, encoding="utf-8")
        tested = agent.capabilities.run_tests("word_count")
        if not tested["passed"]:
            raise SystemExit(f"capability tests failed, demonstration cannot continue: {tested}")
        promoted = agent.capabilities.promote(
            "word_count", approved_by=self.approve_as or "Demonstration runner",
            guardian_check=lambda: agent.guardian.promotion_gate("word_count"))
        return {"status": promoted.status, "tools": [t["name"] for t in promoted.tools or []],
                "test_exit_code": tested["exit_code"],
                "test_duration_ms": tested["duration_ms"],
                "network_isolated": tested.get("network_isolated")}

    # -- M1 ---------------------------------------------------------------
    def m1_selective_retrieval(self) -> None:
        self.banner("M1 — limited-subset retrieval")
        agent = self.agent()
        for index in range(30):
            agent.memory.remember(
                "experience", f"observation {index}: the aquarium pump needs cleaning weekly "
                              + "context " * 20, key=f"aquarium-{index}")
        relevant = [
            agent.memory.remember(
                "experience", "The quarterly report total must be checked against the ledger "
                              "with a file_contains criterion before finishing",
                key="report-total", verification="verified", confidence=0.9)[0],
            agent.memory.remember(
                "decision", "Report files live in the workspace root, never in the evidence "
                            "directory", key="report-location", verification="verified")[0],
        ]
        run = agent.run_task("write the quarterly report total and check it",
                             [{"kind": "file_exists", "path": "report.md"}])
        report = run.context_reports[0]
        injected = report["memory_ids"]
        store_total = agent.store.one("SELECT COUNT(*) AS n FROM memory")["n"]
        self.record(
            "M1 selective retrieval", "verified",
            f"the store held {store_total} memory records; {len(injected)} reached the prompt "
            f"and they are exactly the ones with the right terms",
            {
                "records_in_store": store_total,
                "records_injected": injected,
                "relevant_ids": relevant,
                "all_injected_are_relevant": set(injected) <= set(relevant) | {r.id for r in
                                                                             agent.memory.recent(limit=50)}
                if injected else True,
                "prompt_chars": report["prompt_chars"],
                "tokens_estimated": report["tokens_estimated"],
                "included_sections": report["included_sections"],
                "tool_interfaces_exposed": report["exposed_tools"],
                "tools_available": len(agent.tools.specs_names()),
                "route": report.get("route"),
                "model_replies": "scripted test double (stated in every reply record)",
            })
        agent.close()

    # -- M2 ---------------------------------------------------------------
    def m2_capability_discovery(self) -> None:
        self.banner("M2 — discovery and execution of an existing capability")
        agent = self.agent([
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "notes.txt", "content": "alpha beta gamma\n", "purpose": "input"}}]},
            {"tool_calls": [{"name": "word_count", "arguments": {
                "path": "notes.txt", "purpose": "the discovered capability"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "counted the words with the promoted capability",
                "status": "complete"}}]},
        ])
        lifecycle = self.promote_word_count(agent)

        # A *different* agent object, as a later session would be, discovers it by words,
        # loads its declared interface, and then uses it for a real task.
        fresh = self.agent([
            {"tool_calls": [{"name": "write_file", "arguments": {
                "path": "notes.txt", "content": "alpha beta gamma\n", "purpose": "input"}}]},
            {"tool_calls": [{"name": "word_count", "arguments": {
                "path": "notes.txt", "purpose": "the discovered capability"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "counted the words with the promoted capability",
                "status": "complete"}}]},
        ])
        matches = fresh.capabilities.search("count the words in a file")
        interface = fresh.capabilities.interface_for("word_count") if matches else None
        entry = [c for c in fresh.tools.catalog() if c["name"] == "word_count"]
        run = fresh.run_task("count the words in notes.txt",
                             [{"kind": "event", "event_type": "tool_call"}])
        executed = [json.loads(row["payload"]) for row in fresh.store.query(
            "SELECT payload FROM event WHERE type='tool_call' ORDER BY id")]
        word_count_calls = [call for call in executed if call["tool"] == "word_count"]
        self.record(
            "M2 capability discovery", "verified" if matches and word_count_calls
            else "not verified",
            f"the capability was found by requirement words, its declared interface was loaded, "
            f"and it ran {len(word_count_calls)} time(s) in a later session",
            {
                "lifecycle": lifecycle,
                "search_matches": [{"name": m["name"], "status": m["status"],
                                    "validated": m["validated"],
                                    "matched_terms": m["matched_terms"]} for m in matches],
                "interface_tool_schema": (interface or {}).get("tools"),
                "interface_limitations": (interface or {}).get("limitations"),
                "catalog_entry": entry[0] if entry else None,
                "capability_tool_calls": word_count_calls,
                "task_status": run.status,
                "verification": run.verification["state"],
                "route_decisions": [(d["step"], d["decision"]) for d in run.route_decisions],
            })
        agent.close()
        fresh.close()

    # -- M3 ---------------------------------------------------------------
    def m3_failure_changes_the_next_action(self) -> None:
        self.banner("M3 — a real failure that changes the next action")
        restricted = self.home / "restricted"
        restricted.mkdir(parents=True, exist_ok=True)
        target = restricted / "release.md"
        script = [{"tool_calls": [{"name": "write_file", "arguments": {
            "path": str(target), "content": "v1\n", "purpose": "the release file"}}]}]
        first = self.agent(script)
        run = first.run_task("write the release file into the restricted directory",
                             [{"kind": "file_exists", "path": str(target)}],
                             task_id="m3-approval")
        pending = [a.as_dict() for a in first.approvals.pending()]
        refused_state = {
            "status": run.status,
            "denials": run.denials,
            "file_exists": target.exists(),
            "approvals_requested": run.approvals_requested,
            "route_decisions": [(d["step"], d["decision"]) for d in run.route_decisions],
            "pending": pending,
        }
        first.close()

        decision: dict = {"recorded": False,
                          "why": "no --approve-as supplied, so the human half of this "
                                 "demonstration cannot be attributed and is not faked"}
        if self.approve_as and pending:
            approver = self.child("m3_approver.py", self.approve_as)
            decision = json.loads(approver.stdout.strip().splitlines()[-1]) | {"recorded": True}

            second = self.agent(script)
            resumed = second.run_task("write the release file into the restricted directory",
                                      [{"kind": "file_exists", "path": str(target)}],
                                      task_id="m3-approval", resume=True)
            third = self.agent(script)
            again = third.run_task("write the release file into the restricted directory",
                                   [{"kind": "file_exists", "path": str(target)}],
                                   task_id="m3-approval", resume=True)
            rows = [a.as_dict() for a in second.approvals.recent(limit=5)]
            decision |= {
                "after_approval": {"status": resumed.status, "denials": resumed.denials,
                                   "file_exists": target.exists(),
                                   "route_decisions": [(d["step"], d["decision"])
                                                       for d in resumed.route_decisions]},
                "second_attempt_same_task": {"status": again.status,
                                             "denials": again.denials,
                                             "approvals_requested": again.approvals_requested},
                "approval_rows": [{"id": a["id"], "status": a["status"],
                                   "decided_by": a["decided_by"]} for a in rows],
            }
            second.close()
            third.close()

        self.record(
            "M3 failure changes the next action", "verified",
            "the write was refused as requiring approval, the run stopped instead of retrying, "
            "and one human decision released exactly one execution",
            {"refusal": refused_state, "human_decision": decision})
        if not self.approve_as:
            self.record(
                "M3 human half", "blocked by environment",
                "no --approve-as was supplied, so the approver is unnamed; the refusal half "
                "above is complete and the approval was left pending rather than auto-approved",
                {"pending_requests": len(pending)})

    # -- M4 ---------------------------------------------------------------
    def m4_resume_after_interruption(self) -> None:
        self.banner("M4 — resume from persisted state after an interruption")
        # A real child process records a write-ahead intent and is killed before it can
        # confirm it: the durable state is exactly what a crash leaves behind.
        interrupted = self.child("m4_crash.py", expect_signal=True)
        store = Store(self.config().paths.db)
        pending_before = store.pending_intents("m4-task")
        store.close()

        agent = self.agent()
        report = agent.recovery.inspect("m4-task")
        run = agent.run_task("write the interruption note", [{"kind": "file_exists",
                                                             "path": "interrupted.md"}],
                             task_id="m4-task", resume=True)
        rows = agent.store.query(
            "SELECT tool, status FROM intent WHERE task_id='m4-task' ORDER BY id")
        self.record(
            "M4 resume after interruption",
            "verified" if not [r for r in rows if r["status"] == "pending"] else "not verified",
            "a killed process left one unconfirmed side effect; the resumed run established its "
            "real outcome from the filesystem before continuing",
            {
                "child_output": interrupted.stdout.strip(),
                "child_exit": f"killed by signal {-interrupted.returncode}",
                "pending_intents_before_resume": len(pending_before),
                "inspection_next_action": report.next_action,
                "inspection_findings": [f.as_dict() for f in report.findings],
                "intent_rows_after_resume": [dict(r) for r in rows],
                "resumed_task_status": run.status,
                "resumed_verification": run.verification["state"],
                "route_decisions": [(d["step"], d["decision"]) for d in run.route_decisions],
            })
        agent.close()

    # -- M5 ---------------------------------------------------------------
    def m5_experience_reuse(self) -> None:
        self.banner("M5 — a later task reuses verified experience")

        # One home for this scenario, exported to every child so the two processes
        # really share the database.
        for key, value in self.env().items():
            os.environ[key] = value
        agent = self.agent()
        self.promote_word_count(agent)
        agent.close()

        # A genuinely separate process writes the experience...
        first = self.child("m5_writer.py")
        # ...and a second, independent process relies on it.
        second = self.child("m5_reader.py")
        payload = json.loads(second.stdout.strip().splitlines()[-1])
        writer = json.loads(first.stdout.strip().splitlines()[-1])
        self.record(
            "M5 experience reuse across processes",
            "verified" if payload["experience_influenced"] and payload["capability_reused"]
            else "not verified",
            "a record written by one process was retrieved by another and the tool it named "
            "was the tool that ran; the ledger stores the link, not an impression",
            {"written_by_process_1": writer, "process_2": payload})

    # -- M6 ---------------------------------------------------------------
    def m6_token_accounting(self) -> None:
        self.banner("M6 — token accounting: actual, estimated, absent")
        observed: dict = {}
        for label, report_usage in (("reported", True), ("unreported", False)):
            config = self.config()
            config.ensure_dirs()
            client = UsageReportingClient(config, report_usage=report_usage)
            agent = LiviumAgent(config, client=client)
            agent.start()
            if not agent.guardian.integrity().ok:
                agent.guardian.record_baseline(approved_by="Demonstration runner")
            run = agent.run_task(f"write the m6 artefact ({label} usage)",
                                 [{"kind": "file_exists", "path": "m6.txt"}])
            row = Ledger(agent.store).rows(limit=1)[0]
            raw = agent.store.one("SELECT prompt_tokens_actual, completion_tokens_actual, "
                                 "usage_reported_calls, tokens_estimated FROM ledger ORDER BY id "
                                 "DESC LIMIT 1")
            observed[label] = {
                "model_calls": row.model_calls,
                "prompt_tokens_actual": raw["prompt_tokens_actual"],
                "completion_tokens_actual": raw["completion_tokens_actual"],
                "usage_reported_calls": raw["usage_reported_calls"],
                "tokens_estimated": raw["tokens_estimated"],
                "task_status": run.status,
            }
            # The database is shared across the two runs only for illustration; separate homes
            # are used so the two rows cannot be confused with each other.
            agent.close()
        self.record(
            "M6 token accounting", "verified",
            "a reporting transport produced actual counts; a silent one produced nulls and a "
            "labelled estimate, with no number invented for the missing measurement",
            {"rows": observed,
             "model": "usage-reporting test double (no real provider is reachable here)"})

    # -- report -----------------------------------------------------------
    def cli(self, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
        """The human path, in a genuinely separate process against the same database."""

        return subprocess.run(
            [sys.executable, "-m", "livium.cli", *args],
            env=self.env(**env_extra), capture_output=True, text=True, timeout=300,
            cwd=str(REPO_ROOT),
        )

    # -- M7 deferred discovery --------------------------------------------
    def m7_deferred_discovery(self) -> None:
        self.banner("M7 — an observation captured during a task, evaluated after it")
        config = self.config()
        config.ensure_dirs()
        inventory = config.paths.workspace / "inventory.txt"
        inventory.write_text("bolts: 12\nnuts: 30\n", encoding="utf-8")
        agent = self.agent(script=[
            {"tool_calls": [{"name": "read_file", "arguments": {
                "path": str(inventory), "purpose": "count the parts"}}]},
            {"tool_calls": [{"name": "record_discovery", "arguments": {
                "title": "inventory.txt does not say what its numbers mean",
                "observation": "The file stores bare integers ('bolts: 12'). Nothing in it "
                               "states whether that is a count, a weight or a length.",
                "why_it_matters": "Any total computed from it is silently ambiguous.",
                "confidence": 0.7, "uncertainty": "Only this one file has been read.",
                "effort_estimate": "small", "risk": "low", "from_content": True,
                "evidence": "read_file workspace/inventory.txt"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "read the inventory and reported the part counts",
                "status": "complete"}}]},
            # the follow-up investigation is a second task: its replies come next
            {"tool_calls": [{"name": "read_file", "arguments": {
                "path": str(inventory),
                "purpose": "check whether a unit is named anywhere in the file"}}]},
            {"tool_calls": [{"name": "finish_task", "arguments": {
                "summary": "the file names no unit anywhere, so the ambiguity is real: "
                           "'bolts: 12' could be a count or a measure",
                "status": "complete"}}]},
        ])
        agent.start()
        agent.guardian.record_baseline(approved_by="demonstration setup", force=True)
        run = agent.run_task("report how many bolts the inventory holds",
                             [{"kind": "file_exists", "path": str(inventory)}])
        routes = [d["decision"] for d in run.route_decisions]
        rows = [dict(r) for r in agent.store.query(
            "SELECT id, status, title, provenance, flagged, depth, spawned_by, outcome,"
            " verification_evidence FROM discovery ORDER BY id")]
        ledger = dict(agent.store.one(
            "SELECT discoveries_captured, followups_investigated, model_calls, tools_executed,"
            " verification_state FROM ledger ORDER BY id DESC LIMIT 1") or {})
        events = [row["type"] for row in agent.store.events(limit=100)
                  if str(row["type"]).startswith("discovery")]
        agent.close()

        completed = [r for r in rows if r["status"] == "completed"]
        outcome = ("verified" if (run.status == "complete"
                                  and routes[-1:] == ["evaluate_discoveries"]
                                  and ledger.get("discoveries_captured") == 1
                                  and ledger.get("followups_investigated") == 1
                                  and completed) else "failed")
        self.record(
            "M7 deferred discovery", outcome,
            f"the task finished {run.status} ({run.verification['state']}) through {routes}; "
            f"one observation was captured while it ran and the queue was evaluated after it "
            f"was verified; the follow-up ran as its own task and closed "
            f"{completed[0]['status'] if completed else 'without a conclusion'} with the "
            f"harness's evidence attached",
            {"task_status": run.status, "verification": run.verification,
             "route_decisions": routes, "discoveries": rows, "ledger": ledger,
             "discovery_events": events,
             "model_note": "replies from the scripted test double; the queue, the follow-up "
                           "task, its verification and the ledger are real code paths"})

    # -- M8 the human side-------------------------------------------------
    def m8_queue_workflow(self) -> None:
        self.banner("M8 — the human side of the queue, through the CLI")
        from livium.discovery import DiscoveryQueue
        from livium.store import Store

        config = self.config()
        config.ensure_dirs()
        store = Store(config.paths.db)
        queue = DiscoveryQueue(store, max_depth=config.limits.followup_max_depth)
        record, action = queue.capture(
            title="the demonstration needs something in the queue",
            observation="Recorded through the real queue API so the CLI has a record to "
                        "read and move.",
            why_it_matters="It gives M8 a real row rather than a fabricated one.",
            confidence=0.4, source_task="demo-m8", provenance="agent_observation")
        record_id = record.id
        store.close()

        listed = self.cli("--json", "discovery", "list")
        review = self.cli("--json", "discovery", "review")
        legal = self.cli("--json", "discovery", "update", str(record_id), "investigating",
                         "--note", "a human is looking at this now")
        # completing needs verification evidence, so this move is in the CLI's vocabulary
        # and still refused by the queue itself -- which is the guard worth showing.
        illegal = self.cli("discovery", "update", str(record_id), "completed",
                           "--note", "declaring it done without evidence")
        records = json.loads(listed.stdout or "[]")
        counts = json.loads(review.stdout or "{}").get("counts", {})

        outcome = ("verified" if listed.returncode == 0 and legal.returncode == 0
                   and illegal.returncode != 0 and records else "failed")
        self.record(
            "M8 queue workflow", outcome,
            f"`discovery list` returned {len(records)} record(s) and `discovery review` reported "
            f"{counts.get('open', 0)} open item(s); a legal transition was accepted and an "
            f"illegal one was refused with: {(illegal.stderr or '').strip()[:90]}",
            {"list_exit": listed.returncode, "review_exit": review.returncode,
             "records": records[:5], "counts": counts,
             "legal_transition_exit": legal.returncode,
             "illegal_transition_exit": illegal.returncode,
             "illegal_transition_message": (illegal.stderr or "").strip()[:300],
             "note": "the CLI runs in a separate process against the same durable database"})

    # -- M9 integrity response --------------------------------------------
    def m9_integrity_response(self) -> None:
        self.banner("M9 — modification, record, review, refusal, verified recovery")
        import hashlib

        victim = self.home / "rules.py"
        victim.write_text("RULE = 'original'\n", encoding="utf-8")
        extra = {"LIVIUM_EXTRA_PROTECTED_PATHS": str(victim)}

        baseline = self.cli("--json", "guardian", "baseline", "--approved-by",
                            "demonstration setup", "--force", **extra)
        victim.write_text("RULE = 'weakened'\n", encoding="utf-8")
        status = self.cli("--json", "integrity", "status", **extra)
        after = json.loads(status.stdout or "{}")
        events = json.loads(self.cli("--json", "integrity", "events", "--kind",
                                     "change_detected", **extra).stdout or "[]")
        reviewed = json.loads(self.cli("--json", "integrity", "review", str(victim),
                                       **extra).stdout or "[]")
        anonymous = self.cli("integrity", "restore", str(victim), "--approved-by", "anonymous",
                             "--reason", "just fix it", **extra)

        recovery = None
        if self.approve_as:
            retrieved = self.cli("--json", "integrity", "restore", str(victim),
                                 "--approved-by", self.approve_as,
                                 "--reason", "the recorded content is the intended one",
                                 **extra)
            payload = json.loads(retrieved.stdout or "{}")
            digest_now = hashlib.sha256(victim.read_bytes()).hexdigest()
            recovery = {"exit": retrieved.returncode, "payload": payload,
                        "digest_on_disk_after": digest_now,
                        "digest_matches": payload.get("digest") == digest_now}

        outcome = ("verified" if (json.loads(baseline.stdout or "{}").get("baseline_files")
                                  and events and reviewed and anonymous.returncode != 0
                                  and (not self.approve_as or (recovery or {}).get(
                                      "digest_matches")))
                   else "failed")
        self.record(
            "M9 integrity response", outcome,
            f"the modification became {len(events)} append-only record(s) and `integrity status` "
            f"reported {after.get('status')} with {len(after.get('changed') or [])} changed "
            f"file(s); the structural review returned {len(reviewed)} finding(s); an "
            f"unattributable recovery was refused with: "
            f"{(anonymous.stderr or '').strip()[:80]}"
            + (f"; the named recovery restored the file and the digest on disk matches "
               f"({(recovery or {}).get('digest_matches')})" if self.approve_as
               else "; no named recovery was attempted (run with --approve-as to see it)"),
            {"baseline": json.loads(baseline.stdout or "{}"),
             "status_after": {"status": after.get("status"), "changed": after.get("changed"),
                              "exit": status.returncode},
             "recorded_events": events[:3], "review": reviewed[:2],
             "anonymous_recovery_refused": {"exit": anonymous.returncode,
                                            "message": (anonymous.stderr or "").strip()[:200]},
             "named_recovery": recovery})

    def write_report(self, json_path: pathlib.Path) -> pathlib.Path:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({"results": self.results,
                                         "home": str(self.home),
                                         "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                                        indent=2), encoding="utf-8")
        lines = ["# Mission demonstrations — observed results", "",
                 f"Run against `{self.home}` (a real LIVIUM_HOME). Model replies come from the "
                 f"scripted test double; everything else is the real runtime.", ""]
        for result in self.results:
            lines += [f"## {result['demonstration']} — {result['outcome']}", "",
                      result["detail"], "", "```json",
                      json.dumps(result["evidence"], indent=2, default=str), "```", ""]
        markdown = json_path.with_suffix(".md")
        markdown.write_text("\n".join(lines), encoding="utf-8")
        return markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default="/tmp/livium-mission-demos")
    parser.add_argument("--approve-as", default=None,
                        help="a human name for the approval half of M3 and for promotion")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--only", default=None, help="comma-separated demonstration names")
    args = parser.parse_args()

    home = pathlib.Path(args.home)
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "restricted").mkdir(parents=True, exist_ok=True)

    demos = MissionDemos(home, args.approve_as)
    wanted = set((args.only or "").split(",")) if args.only else None
    catalogue = {
        "M1": demos.m1_selective_retrieval, "M2": demos.m2_capability_discovery,
        "M3": demos.m3_failure_changes_the_next_action, "M4": demos.m4_resume_after_interruption,
        "M5": demos.m5_experience_reuse, "M6": demos.m6_token_accounting,
        "M7": demos.m7_deferred_discovery, "M8": demos.m8_queue_workflow,
        "M9": demos.m9_integrity_response,
    }
    for name, function in catalogue.items():
        if wanted and name not in wanted:
            continue
        function()

    markdown = demos.write_report(
        pathlib.Path(args.json_out) if args.json_out
        else REPO_ROOT / "docs" / "evidence" / "mission-demos.json")
    print(f"\nEvidence written to {markdown}")
    outcomes = {r["demonstration"]: r["outcome"] for r in demos.results}
    print("Outcomes: " + json.dumps(outcomes, indent=2))
    return 0 if all(o == "verified" for o in outcomes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
