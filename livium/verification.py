"""Acceptance-criteria verification.

The agent cannot mark its own work as passing. It supplies a summary; the
harness checks the criteria by looking at the real world -- file contents, real
processes, exit codes, database rows -- through the same permission engine as
every other operation.

Criterion kinds (JSON, one object per criterion):

``{"kind":"file_exists","path":"notes.txt"}``
``{"kind":"file_contains","path":"notes.txt","text":"total 42"}``
``{"kind":"file_matches","path":"notes.txt","regex":"^total \\\\d+$"}``
``{"kind":"file_absent","path":"scratch.tmp"}``
``{"kind":"command","argv":["python3","-m","unittest","discover"],"expect_exit":0,
   "expect_stdout_contains":"OK","timeout_seconds":60}``
``{"kind":"memory","mem_kind":"decision","key":"storage"}``
``{"kind":"event","event_type":"capability_promoted"}``
``{"kind":"manual","description":"a human must judge readability"}``

Outcome is one of: passed | failed | error | manual | skipped. `manual` and
`skipped` do not count as verified, and any task with one of them is reported as
partially verified rather than complete.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any, Sequence

from .config import Config
from .execution import Runner
from .memory import Memory
from .permissions import PermissionEngine
from .store import Store, now

PASS, FAIL, ERROR, MANUAL, SKIPPED = "passed", "failed", "error", "manual", "skipped"


@dataclasses.dataclass
class CriterionResult:
    criterion: dict[str, Any]
    outcome: str
    detail: str
    evidence: str = ""

    @property
    def verified(self) -> bool:
        return self.outcome == PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion, "outcome": self.outcome,
            "detail": self.detail, "evidence": self.evidence,
        }

    def render(self) -> str:
        mark = {"passed": "PASS", "failed": "FAIL", "error": "ERROR",
                "manual": "MANUAL", "skipped": "SKIPPED"}[self.outcome]
        label = _label(self.criterion)
        line = f"[{mark}] {label} — {self.detail}"
        if self.evidence:
            line += f"\n      evidence: {self.evidence}"
        return line


def _label(criterion: dict[str, Any]) -> str:
    kind = criterion.get("kind", "?")
    if kind == "command":
        return f"command {' '.join(criterion.get('argv', []))}"
    if "path" in criterion:
        return f"{kind} {criterion['path']}"
    if kind == "manual":
        return f"manual: {criterion.get('description', '')}"
    return kind


def normalize_criteria(raw: Any) -> list[dict[str, Any]]:
    """Accept a list of dicts, or a JSON string of one. Reject anything else.

    A criterion that cannot be parsed is a hard error: silently dropping it would
    turn "unverified" into "verified by omission".
    """

    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            return normalize_criteria(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ValueError(f"acceptance criteria are not valid JSON: {exc}") from exc
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("acceptance criteria must be a list of objects")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"each criterion must be an object; got {type(item).__name__}")
        if not item.get("kind"):
            raise ValueError(f"criterion is missing 'kind': {item!r}")
        out.append(dict(item))
    return out


class Verifier:
    def __init__(
        self,
        config: Config,
        engine: PermissionEngine,
        store: Store,
        memory: Memory,
        runner: Runner | None = None,
    ):
        self.config = config
        self.engine = engine
        self.store = store
        self.memory = memory
        self.runner = runner or Runner(
            max_seconds=config.limits.max_command_seconds,
            max_output_bytes=config.limits.max_tool_output_bytes,
        )

    def verify(
        self, criteria: Sequence[dict[str, Any]], *, task_id: str | None = None
    ) -> list[CriterionResult]:
        results = [self._one(criterion, task_id=task_id) for criterion in criteria]
        with self.store.transaction() as conn:
            for result in results:
                conn.execute(
                    "INSERT INTO verification(task_id, criterion, kind, spec, outcome, detail, ts) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        task_id or "", _label(result.criterion), result.criterion.get("kind", ""),
                        json.dumps(result.criterion, sort_keys=True), result.outcome,
                        result.detail, now(),
                    ),
                )
        self.store.append_event(
            "task_verified",
            {
                "criteria": [r.as_dict() for r in results],
                "passed": sum(1 for r in results if r.outcome == PASS),
                "total": len(results),
            },
            task_id=task_id,
        )
        return results

    def _one(self, criterion: dict[str, Any], *, task_id: str | None) -> CriterionResult:
        kind = criterion.get("kind")
        try:
            if kind == "manual":
                return CriterionResult(
                    criterion, MANUAL,
                    "requires human judgement; not counted as verified by automation",
                )
            if kind == "file_exists":
                return self._file_exists(criterion)
            if kind == "file_absent":
                return self._file_absent(criterion)
            if kind == "file_contains":
                return self._file_contains(criterion)
            if kind == "file_matches":
                return self._file_matches(criterion)
            if kind == "command":
                return self._command(criterion, task_id=task_id)
            if kind == "memory":
                return self._memory(criterion)
            if kind == "event":
                return self._event(criterion)
            return CriterionResult(criterion, ERROR, f"unknown criterion kind {kind!r}")
        except Exception as exc:  # a verifier bug must not read as a pass
            return CriterionResult(criterion, ERROR, f"{type(exc).__name__}: {exc}")

    # -- helpers ----------------------------------------------------------
    def _path(self, criterion: dict[str, Any]) -> pathlib.Path:
        path = pathlib.Path(str(criterion.get("path", ""))).expanduser()
        if not path.is_absolute():
            path = self.config.paths.workspace / path
        return pathlib.Path(str(path))

    def _read(self, path: pathlib.Path, criterion: dict[str, Any]) -> tuple[str | None, CriterionResult | None]:
        decision = self.engine.check_read(path)
        if not decision.allowed:
            return None, CriterionResult(criterion, ERROR, f"blocked by permissions: {decision.reason}")
        if not path.exists():
            return None, CriterionResult(criterion, FAIL, f"no such file: {path}")
        if path.is_dir():
            return None, CriterionResult(criterion, ERROR, f"{path} is a directory")
        return path.read_text(encoding="utf-8", errors="replace"), None

    def _file_exists(self, criterion: dict[str, Any]) -> CriterionResult:
        path = self._path(criterion)
        decision = self.engine.check_read(path)
        if not decision.allowed:
            return CriterionResult(criterion, ERROR, f"blocked by permissions: {decision.reason}")
        if path.exists():
            size = path.stat().st_size
            return CriterionResult(criterion, PASS, f"{path} exists ({size} bytes)", str(path))
        return CriterionResult(criterion, FAIL, f"{path} does not exist")

    def _file_absent(self, criterion: dict[str, Any]) -> CriterionResult:
        path = self._path(criterion)
        if path.exists():
            return CriterionResult(criterion, FAIL, f"{path} exists but should not")
        return CriterionResult(criterion, PASS, f"{path} is absent", str(path))

    def _file_contains(self, criterion: dict[str, Any]) -> CriterionResult:
        path = self._path(criterion)
        text, failure = self._read(path, criterion)
        if failure:
            return failure
        needle = str(criterion.get("text", ""))
        if needle in (text or ""):
            lines = [f"line {i+1}: {l.strip()}" for i, l in enumerate((text or "").splitlines())
                     if needle in l][:3]
            return CriterionResult(criterion, PASS, f"{needle!r} present in {path}",
                                  "; ".join(lines) or str(path))
        return CriterionResult(criterion, FAIL, f"{needle!r} not found in {path} ({len(text or '')} chars read)")

    def _file_matches(self, criterion: dict[str, Any]) -> CriterionResult:
        path = self._path(criterion)
        text, failure = self._read(path, criterion)
        if failure:
            return failure
        # MULTILINE so `^`/`$` anchor to lines, which is what a per-file
        # pattern means in practice for multi-line artefacts.
        pattern = re.compile(str(criterion.get("regex", "")), re.MULTILINE)
        match = pattern.search(text or "")
        if match:
            return CriterionResult(criterion, PASS, f"regex matched in {path}", match.group(0)[:200])
        return CriterionResult(criterion, FAIL, f"regex {pattern.pattern!r} did not match {path}")

    def _command(self, criterion: dict[str, Any], *, task_id: str | None) -> CriterionResult:
        argv = criterion.get("argv")
        if not isinstance(argv, list) or not argv:
            return CriterionResult(criterion, ERROR, "command criterion needs a non-empty argv array")
        argv = [str(a) for a in argv]
        decision = self.engine.check_command(argv)
        if not decision.allowed:
            return CriterionResult(
                criterion, ERROR,
                f"the verification command is itself blocked by policy: {decision.reason}",
            )
        timeout = float(criterion.get("timeout_seconds") or self.config.limits.max_command_seconds)
        result = self.runner.run(argv, cwd=self.config.paths.workspace, timeout=timeout)
        expect_exit = criterion.get("expect_exit")
        evidence = (
            f"exit={result.exit_code} duration={result.duration_ms}ms "
            f"network_isolated={result.network_isolated}\n"
            f"stdout: {result.stdout[-1200:].strip()}\nstderr: {result.stderr[-1200:].strip()}"
        )
        if result.timed_out:
            return CriterionResult(criterion, FAIL, f"timed out after {timeout:g}s", evidence)
        if expect_exit is not None and result.exit_code != int(expect_exit):
            return CriterionResult(
                criterion, FAIL,
                f"expected exit {expect_exit}, observed {result.exit_code}", evidence,
            )
        needle = criterion.get("expect_stdout_contains")
        if needle and needle not in result.stdout:
            return CriterionResult(
                criterion, FAIL,
                f"stdout did not contain {needle!r}", evidence,
            )
        return CriterionResult(
            criterion, PASS,
            f"exit={result.exit_code} as expected" if expect_exit is not None
            else f"command ran (exit {result.exit_code})",
            evidence,
        )

    def _memory(self, criterion: dict[str, Any]) -> CriterionResult:
        mem_kind = str(criterion.get("mem_kind", criterion.get("kind_memory", "decision")))
        key = criterion.get("key")
        if key:
            record = self.memory.by_key(mem_kind, str(key))
        else:
            matches = self.memory.search(str(criterion.get("query", "")) or key or mem_kind, limit=1)
            record = matches[0] if matches else None
        if record is None:
            return CriterionResult(criterion, FAIL, f"no active {mem_kind} record for key {key!r}")
        return CriterionResult(
            criterion, PASS,
            f"mem:{record.id} ({record.kind}/{record.key}) present, verification={record.verification}",
            record.content[:200],
        )

    def _event(self, criterion: dict[str, Any]) -> CriterionResult:
        event_type = str(criterion.get("event_type", ""))
        rows = self.store.events(event_type=event_type, limit=1)
        if not rows:
            return CriterionResult(criterion, FAIL, f"no event of type {event_type!r} recorded")
        return CriterionResult(
            criterion, PASS, f"event {event_type!r} recorded at {rows[0]['ts']}",
            rows[0]["payload"][:300],
        )


def summarize(results: Sequence[CriterionResult]) -> dict[str, Any]:
    """Turn raw results into the honest summary used in reports.

    Any criterion that was not *passed* keeps the task out of 'verified'.
    """

    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    total = len(results)
    passed = counts.get(PASS, 0)
    if total == 0:
        state = "unverified (no acceptance criteria were defined)"
    elif passed == total:
        state = "verified"
    elif passed == 0:
        state = "not verified"
    else:
        state = "partially verified"
    return {
        "total": total,
        "passed": passed,
        "counts": counts,
        "state": state,
        "remaining": [
            {"criterion": r.criterion, "outcome": r.outcome, "detail": r.detail}
            for r in results if r.outcome != PASS
        ],
    }
