"""Command-line interface.

Human-facing operations live here, including the ones the agent deliberately
cannot perform: recording the guardian baseline, promoting a capability, running
a rollback. Those are the controls that must stay outside the agent's reach, and
this is the smallest dependable place to put them.

Every command prints what it actually did. ``--json`` emits the same information
for scripts; neither path invents a result.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import textwrap
from typing import Any, Sequence

from . import __version__
from .agent import LiviumAgent
from .capabilities import CapabilityError, CapabilityRegistry
from .config import Config, ConfigError
from .guardian import GrowthGuardian
from .memory import Memory
from .models import CredentialMissing, ModelError
from .permissions import PermissionEngine
from .report import build as build_report, render as render_report
from .store import Store


def _print(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _human(payload)


def _human(payload: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                print(f"{pad}{key}:")
                _human(value, indent + 1)
            else:
                print(f"{pad}{key}: {value}")
    elif isinstance(payload, list):
        if not payload:
            print(f"{pad}(none)")
        for item in payload:
            if isinstance(item, (dict, list)):
                _human(item, indent + 1)
                print()
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{payload}")


def _agent(config: Config) -> LiviumAgent:
    return LiviumAgent(config)


# ---------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    agent = _agent(config)
    try:
        startup = agent.start()
        report = {
            "version": __version__,
            "runtime": {
                "python": sys.version.split()[0],
                "executable": sys.executable,
            },
            "config": startup["config"],
            "provider_probe": startup["provider_probe"],
            "isolation": startup["isolation"],
            "guardian": {
                "integrity": startup["guardian_integrity"],
                "frozen": startup["guardian_frozen"],
            },
            "capabilities": startup["capabilities_loaded"],
            "tools": startup["tools"],
            "context": startup["context"],
            "credentials": {
                "present": config.credential_present(),
                "provider": config.model.provider,
                "note": (
                    "no credential is required for the ollama provider"
                    if config.model.provider == "ollama"
                    else "set LIVIUM_API_KEY (or the provider's conventional variable)"
                ),
            },
            "database": {"path": str(config.paths.db), "counts": agent.store.stats()},
        }
        _print(report, args.json)
        probe = startup["provider_probe"]
        if not probe.get("ok"):
            print(
                "\nModel provider is NOT verified: " + str(probe.get("detail")),
                file=sys.stderr,
            )
            print(
                "Livium will run, but any task it is given will end as 'blocked' with that "
                "reason until the provider is reachable and the model name is valid.",
                file=sys.stderr,
            )
        if not startup["guardian_integrity"]["ok"]:
            print(
                "\nGuardian integrity is not established: "
                + startup["guardian_integrity"]["detail"],
                file=sys.stderr,
            )
            print(
                "Run: livium guardian baseline --approved-by <your name>",
                file=sys.stderr,
            )
        return 0 if probe.get("ok") else 3
    finally:
        agent.close()


def cmd_run(args: argparse.Namespace, config: Config) -> int:
    criteria: list[dict[str, Any]] = []
    if args.criteria:
        raw = args.criteria
        if raw.startswith("@"):
            criteria = json.loads(pathlib.Path(raw[1:]).read_text(encoding="utf-8"))
        else:
            criteria = json.loads(raw)
    agent = _agent(config)
    try:
        startup = agent.start()
        if not startup["provider_probe"].get("ok") and not args.force:
            print(
                "refusing to start: the model provider is not usable.\n  "
                + str(startup["provider_probe"].get("detail"))
                + "\nFix the provider, or pass --force to run and receive the blocked report.",
                file=sys.stderr,
            )
            return 3
        run = agent.run_task(
            args.objective, criteria, task_id=args.task_id, resume=args.resume,
            on_step=(None if args.json else lambda event: print(
                f"  step {event['step']}: {event['status']} — "
                f"{' | '.join(event['observations'])[:200]}"
            )),
        )
        payload = run.as_dict()
        payload["startup"] = {
            "provider_probe": startup["provider_probe"],
            "guardian_integrity": startup["guardian_integrity"],
            "isolation": startup["isolation"],
        }
        _print(payload, args.json)
        if run.is_test_double:
            print(
                "\nNOTE: this run used the scripted test double. It exercised Livium's "
                "orchestration only. No real model produced these results.",
                file=sys.stderr,
            )
        return {"complete": 0, "partial": 1, "blocked": 2, "failed": 4}.get(run.status, 1)
    finally:
        agent.close()


def cmd_resume(args: argparse.Namespace, config: Config) -> int:
    agent = _agent(config)
    try:
        agent.start()
        report = agent.recovery.inspect(args.task_id)
        _print(report.as_dict(), args.json)
        if not args.json:
            print()
            print(report.render())
        return 0 if report.safe_to_resume else 2
    finally:
        agent.close()


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    agent = _agent(config)
    try:
        agent.start()
        _print(agent.status(), args.json)
        if not args.json:
            print("\nrecent context constructions:")
            for report in agent.store.query(
                "SELECT task_id, step, estimated, prompt_chars, omitted FROM context_report "
                "ORDER BY id DESC LIMIT 5"
            ):
                print(f"  task={report['task_id']} step={report['step']} "
                      f"estimated_tokens={report['estimated']} chars={report['prompt_chars']} "
                      f"omitted={report['omitted'][:120]}")
            print("\nunfinished tasks:")
            rows = agent.store.query(
                "SELECT id, objective, status, updated_at FROM task WHERE status='active' "
                "ORDER BY updated_at DESC LIMIT 5"
            )
            if not rows:
                print("  (none)")
            for row in rows:
                print(f"  {row['id']}  [{row['status']}] {row['objective'][:70]}  "
                      f"updated {row['updated_at']}")
        return 0
    finally:
        agent.close()


def cmd_events(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.paths.db)
    try:
        rows = store.events(task_id=args.task_id, event_type=args.type, limit=args.limit)
        payload = [
            {"id": row["id"], "ts": row["ts"], "task_id": row["task_id"], "type": row["type"],
             "payload": json.loads(row["payload"] or "{}")}
            for row in rows
        ]
        if args.json:
            _print(payload, True)
        else:
            for item in reversed(payload):
                body = json.dumps(item["payload"], default=str)
                print(f"{item['ts']}  {item['type']:<28} {body[:150]}")
        return 0
    finally:
        store.close()


def cmd_memory(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.paths.db)
    memory = Memory(store)
    try:
        if args.memory_action == "search":
            records = memory.search(args.query, kinds=args.kinds or None, limit=args.limit)
            if args.json:
                _print([r.as_dict() for r in records], True)
            else:
                for record in records:
                    print(record.render())
                    print()
            return 0
        if args.memory_action == "list":
            rows = memory.recent(kinds=args.kinds or None, limit=args.limit)
            _print([r.as_dict() for r in rows], args.json)
            return 0
        if args.memory_action == "history":
            _print([r.as_dict() for r in memory.history(int(args.id))], args.json)
            return 0
        if args.memory_action == "duplicates":
            _print(memory.find_duplicates(), args.json)
            return 0
        if args.memory_action == "consolidate":
            result = memory.consolidate_duplicates()
            _print(result, args.json)
            if not args.json:
                print(f"retired {len(result['retired'])} duplicate record(s); "
                      f"{len(result['kept'])} kept")
            return 0
        if args.memory_action == "stale":
            touched = memory.mark_stale(
                older_than_days=args.days, kinds=args.kinds or ("experience", "capability")
            )
            _print({"downgraded": touched}, args.json)
            return 0
        print("unknown memory action", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_capability(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.paths.db)
    engine = PermissionEngine(config)
    guardian = GrowthGuardian(config, engine, store, Memory(store))
    registry = CapabilityRegistry(store, lab=config.paths.capability_lab)
    try:
        action = args.capability_action
        if action == "list":
            items = registry.all()
            if args.json:
                _print([c.as_dict() for c in items], True)
            else:
                if not items:
                    print("(no capabilities recorded: nothing has been proposed yet)")
                for capability in items:
                    print(capability.render())
                    print()
            return 0
        if action == "show":
            capability = registry.get(args.name)
            if capability is None:
                print(f"no capability {args.name!r}", file=sys.stderr)
                return 2
            _print(capability.as_dict(), args.json)
            return 0
        if action == "propose":
            capability = registry.propose(
                name=args.name, requirement=args.requirement,
                reuse_analysis=args.reuse_analysis, rationale=args.rationale or "",
                verification_plan=args.verification_plan or "",
            )
            _print(capability.as_dict(), args.json)
            return 0
        if action == "scaffold":
            paths = registry.scaffold(args.name)
            _print({k: str(v) for k, v in paths.items()}, args.json)
            if not args.json:
                print(
                    "\nThe scaffold is a skeleton with TODOs, not an implementation. Status is "
                    "'experimental' until `livium capability test` passes."
                )
            return 0
        if action == "test":
            result = registry.run_tests(args.name, timeout=args.timeout)
            _print(result, args.json)
            if not args.json:
                print(f"\ntests {'PASSED' if result['passed'] else 'FAILED'} "
                      f"(exit {result['exit_code']}) -> status {result['status']}")
            return 0 if result["passed"] else 1
        if action == "promote":
            capability = registry.promote(
                args.name, approved_by=args.approved_by,
                guardian_check=lambda: guardian.promotion_gate(args.name),
            )
            _print(capability.as_dict(), args.json)
            if not args.json:
                print(
                    "\nPromoted: the module was copied into livium/promoted/ and its tools "
                    "become callable on the next run."
                )
            return 0
        if action == "rollback":
            capability = registry.rollback(args.name, reason=args.reason)
            _print(capability.as_dict(), args.json)
            return 0
        if action == "reject":
            capability = registry.reject(args.name, reason=args.reason)
            _print(capability.as_dict(), args.json)
            return 0
        print("unknown capability action", file=sys.stderr)
        return 2
    except CapabilityError as exc:
        print(f"capability error: {exc}", file=sys.stderr)
        return 5
    finally:
        store.close()


def cmd_guardian(args: argparse.Namespace, config: Config) -> int:
    store = Store(config.paths.db)
    engine = PermissionEngine(config)
    guardian = GrowthGuardian(config, engine, store, Memory(store))
    try:
        action = args.guardian_action
        if action == "status":
            payload = {
                "integrity": guardian.integrity().as_dict(),
                "frozen": guardian.freeze_status(),
                "protected_files": [str(p) for p in guardian.protected_files()],
                "baseline_recorded": len(guardian.load_baseline()),
                "consequences": [c.as_dict() for c in guardian.consequences(None)],
                "enforcement": {
                    "agent_can_write_protected_paths": False,
                    "agent_can_rebaseline": False,
                    "agent_can_promote_capabilities": False,
                    "mechanism": (
                        "write tool refuses protected paths; re-baselining and promotion exist "
                        "only as CLI commands requiring a named human; integrity is re-checked "
                        "after every command execution and before every promotion"
                    ),
                },
            }
            _print(payload, args.json)
            return 0
        if action == "baseline":
            result = guardian.record_baseline(approved_by=args.approved_by)
            _print(result, args.json)
            return 0
        if action == "propose-change":
            result = guardian.propose_instruction_change(
                target_file=args.target_file,
                observed_failure=args.observed_failure,
                suspected_cause=args.suspected_cause or "not determined",
                proposed_change=args.proposed_change,
                evidence=args.evidence or "not supplied",
                occurrence_count=args.occurrences,
                previous_version_digest=args.previous_digest or "not supplied",
            )
            _print(result, args.json)
            return 0
        print("unknown guardian action", file=sys.stderr)
        return 2
    finally:
        store.close()


def cmd_selftest(args: argparse.Namespace, config: Config) -> int:
    """Run the repository's own test suite. Real subprocess, real exit code."""

    import re
    import time

    root = pathlib.Path(__file__).resolve().parent.parent
    argv = [sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"),
            "-t", str(root / "tests"), "-v"]
    if args.pattern:
        argv = [sys.executable, "-m", "unittest", "-v"] + args.pattern
    print("$ " + " ".join(argv), flush=True)
    if args.record:
        completed = subprocess.run(argv, cwd=str(root), check=False, capture_output=True,
                                   text=True)
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        # unittest writes its summary to stderr, the test output to stdout: read both,
        # so a recorded count is never null.
        combined = completed.stdout + completed.stderr
        match = re.search(r"Ran (\d+) tests? in", combined)
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "command": " ".join(argv),
            "tests": int(match.group(1)) if match else None,
            "failures": int((re.search(r"failures=(\d+)", combined) or [None, 0])[1]
                            if re.search(r"failures=(\d+)", combined) else 0),
            "errors": int((re.search(r"errors=(\d+)", combined) or [None, 0])[1]
                          if re.search(r"errors=(\d+)", combined) else 0),
            "exit_code": completed.returncode,
            "result": "OK" if re.search(r"\nOK\s*$", combined) else "FAILED",
        }
        if record["tests"] is None:
            record["warning"] = "the suite summary line was not found; the count is unknown"
        path = config.paths.home / "selftest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(f"recorded: {path} (tests={record['tests']}, exit={record['exit_code']})")
        return completed.returncode

    completed = subprocess.run(argv, cwd=str(root), check=False)
    print(f"\nexit code: {completed.returncode}")
    return completed.returncode


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    """Print the final report, assembled from live state at this moment."""

    agent = _agent(config)
    try:
        agent.start()
        data = build_report(config, agent)
    finally:
        agent.close()

    if args.write:
        target = pathlib.Path(args.write)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"written: {target}")

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render_report(data))
    return 0


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    from .server import serve

    return serve(config, host=args.host, port=args.port)


# ---------------------------------------------------------------- parsing
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livium",
        description="Agent Livium — a small, persistent, experience-driven agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            quick start:
              livium doctor                       # what works, what does not, and why
              livium run "summarise README.md"    # a real task through the real provider
              livium serve                        # API + viewer at http://127.0.0.1:8787
            """
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version=f"livium {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="report runtime, provider, isolation, permissions, integrity")

    run = sub.add_parser("run", help="execute a task")
    run.add_argument("objective")
    run.add_argument("--criteria", help="JSON array, or @path/to/file.json")
    run.add_argument("--task-id")
    run.add_argument("--resume", action="store_true", help="reconstruct an interrupted task first")
    run.add_argument("--force", action="store_true", help="run even if the provider probe failed")

    resume = sub.add_parser("resume", help="inspect an interrupted task and report safe-to-resume")
    resume.add_argument("--task-id")

    sub.add_parser("status", help="configuration, stats, capabilities, recent context")

    events = sub.add_parser("events", help="read the append-only journal")
    events.add_argument("--task-id")
    events.add_argument("--type")
    events.add_argument("--limit", type=int, default=40)

    memory = sub.add_parser("memory", help="inspect and maintain persistent memory")
    memory_sub = memory.add_subparsers(dest="memory_action", required=True)
    search = memory_sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--kinds", nargs="*")
    search.add_argument("--limit", type=int, default=10)
    listing = memory_sub.add_parser("list")
    listing.add_argument("--kinds", nargs="*")
    listing.add_argument("--limit", type=int, default=20)
    history = memory_sub.add_parser("history")
    history.add_argument("id")
    memory_sub.add_parser("duplicates")
    memory_sub.add_parser("consolidate")
    stale = memory_sub.add_parser("stale")
    stale.add_argument("--days", type=int, default=30)
    stale.add_argument("--kinds", nargs="*")

    capability = sub.add_parser("capability", help="capability lifecycle")
    capability_sub = capability.add_subparsers(dest="capability_action", required=True)
    capability_sub.add_parser("list")
    show = capability_sub.add_parser("show")
    show.add_argument("name")
    propose = capability_sub.add_parser("propose")
    propose.add_argument("name")
    propose.add_argument("--requirement", required=True)
    propose.add_argument("--reuse-analysis", required=True)
    propose.add_argument("--rationale")
    propose.add_argument("--verification-plan")
    scaffold = capability_sub.add_parser("scaffold")
    scaffold.add_argument("name")
    test = capability_sub.add_parser("test")
    test.add_argument("name")
    test.add_argument("--timeout", type=float, default=120.0)
    promote = capability_sub.add_parser("promote")
    promote.add_argument("name")
    promote.add_argument("--approved-by", required=True)
    rollback = capability_sub.add_parser("rollback")
    rollback.add_argument("name")
    rollback.add_argument("--reason", required=True)
    reject = capability_sub.add_parser("reject")
    reject.add_argument("name")
    reject.add_argument("--reason", required=True)

    guardian = sub.add_parser("guardian", help="integrity and consequence controls")
    guardian_sub = guardian.add_subparsers(dest="guardian_action", required=True)
    guardian_sub.add_parser("status")
    baseline = guardian_sub.add_parser("baseline")
    baseline.add_argument("--approved-by", required=True)
    propose_change = guardian_sub.add_parser("propose-change")
    propose_change.add_argument("--target-file", required=True)
    propose_change.add_argument("--observed-failure", required=True)
    propose_change.add_argument("--suspected-cause")
    propose_change.add_argument("--proposed-change", required=True)
    propose_change.add_argument("--evidence")
    propose_change.add_argument("--occurrences", type=int, default=1)
    propose_change.add_argument("--previous-digest")

    selftest = sub.add_parser("selftest", help="run the repository test suite")
    selftest.add_argument("pattern", nargs="*")
    selftest.add_argument("--record", action="store_true",
                          help="store the result in $LIVIUM_HOME/selftest.json for `livium report`")

    report = sub.add_parser("report", help="final report, generated from live state")
    report.add_argument("--write", default=None, help="also write the JSON report to this path")

    serve = sub.add_parser("serve", help="HTTP API and viewer")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8787)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = Config.load()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    handlers = {
        "doctor": cmd_doctor, "run": cmd_run, "resume": cmd_resume, "status": cmd_status,
        "events": cmd_events, "memory": cmd_memory, "capability": cmd_capability,
        "guardian": cmd_guardian, "selftest": cmd_selftest, "report": cmd_report,
        "serve": cmd_serve,
    }
    try:
        return handlers[args.command](args, config)
    except CredentialMissing as exc:
        print(f"credential missing: {exc}", file=sys.stderr)
        return 3
    except ModelError as exc:
        print(f"model layer error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
