"""Capability lifecycle: Proposed -> Experimental -> Validated -> Active -> Maintained
-> Deprecated.

The point of this module is that the stages have teeth. "Validated" is only
reachable by executing the capability's tests and recording their real output;
"Active" additionally requires a human to run the promote command, because the
agent has no tool that can promote anything. Promotion copies real files into
`livium/promoted/`, where the loader picks them up and their tools become
callable on the next run -- so growth through this path is an actual change in
what the agent can do, not a record that says it can.

Deliberately absent: a registry of capabilities that do not exist. Every row here
is created either by the agent's proposal tool (status=proposed, no
implementation) or by a real test run/promotion, and the row carries the paths
and evidence that justify its status.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

from .store import Store, now

STATUSES = (
    "proposed",        # described only; nothing implemented
    "experimental",    # code exists, executed or tested, tests not passing / not run
    "validated",       # tests executed and their real output recorded as passing
    "active",          # human-approved; loaded and callable
    "maintained",      # active and in use, with at least one recorded use
    "deprecated",      # superseded
    "rejected",        # deliberately not pursued; reason retained
    "rollback",        # reverted after a defect; reason and evidence retained
)

VALID_TRANSITIONS = {
    "proposed": {"experimental", "rejected"},
    "experimental": {"validated", "rejected", "rollback", "proposed"},
    "validated": {"active", "experimental", "rejected", "rollback"},
    "active": {"maintained", "deprecated", "rollback"},
    "maintained": {"deprecated", "rollback"},
    "deprecated": {"rollback"},
    "rejected": {"proposed"},
    "rollback": {"proposed"},
}

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

CAPABILITY_TEMPLATE = '''"""Promoted capability: {name}

Requirement: {requirement}

This module was written in the capability lab, tested there, and promoted by a
human. It is loaded by livium.capabilities.loader when Livium starts.

Contract with the loader:
  * ``CAPABILITY`` describes the capability and the tools it adds.
  * ``handle(tool_name, args, ctx)`` returns a ``livium.tools.ToolResult``.
Import only the standard library plus livium modules, so the loader stays safe.
"""

from __future__ import annotations

from typing import Any

CAPABILITY = {{
    "name": {name!r},
    "requirement": {requirement!r},
    "description": "TODO: one line, describing what the tools below actually do.",
    "tools": [
        # Example entry -- replace with the tools this capability really provides,
        # or delete it if the capability adds no tools.
        {{
            "name": "{name}_demo",
            "description": "TODO: what this tool does and when to call it.",
            "parameters": {{
                "type": "object",
                "properties": {{}},
                "required": [],
            }},
        }},
    ],
}}


def handle(tool_name: str, args: dict[str, Any], ctx: Any):
    from livium.tools import ToolResult

    if tool_name == "{name}_demo":
        return ToolResult(tool=tool_name, ok=True, output="TODO: real behaviour")
    return ToolResult(
        tool=tool_name, ok=False, output=f"unknown tool {{tool_name!r}}", error="unknown_tool"
    )
'''

TEST_TEMPLATE = '''"""Tests for this capability. Executed by `livium capability test`."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import MODULE  # noqa: E402


class TestCapabilityContract(unittest.TestCase):
    def test_capability_declares_requirement(self):
        self.assertTrue(MODULE.CAPABILITY.get("requirement"), "requirement must be stated")

    def test_declared_tools_are_callable(self):
        for spec in MODULE.CAPABILITY.get("tools", []):
            result = MODULE.handle(spec["name"], {}, None)
            self.assertIsNotNone(result, f"tool {spec['name']} returned nothing")

    def test_the_implementation_is_not_still_a_scaffold(self):
        """A skeleton must never be able to validate itself.

        Without this check the generated placeholder tests pass, the capability
        reaches 'validated', and a human is invited to promote an unimplemented
        module -- appearance substituting for function. This test fails until the
        TODO markers are replaced with real behaviour.
        """

        source = pathlib.Path(__file__).with_name("MODULE_FILE").read_text(encoding="utf-8")
        self.assertNotIn(
            "TODO",
            source,
            "the capability module still contains scaffold placeholders; implement it and "
            "replace this test's expectation with assertions about the real behaviour",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
'''


@dataclasses.dataclass
class Capability:
    name: str
    status: str
    requirement: str
    rationale: str | None
    implementation: str | None
    tests: str | None
    verification: str | None
    evidence: str | None
    reuse_analysis: str | None
    lines: int | None
    created_at: str
    updated_at: str
    promoted_at: str | None
    promoted_by: str | None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def render(self) -> str:
        parts = [
            f"capability {self.name} [{self.status}]",
            f"  requirement: {self.requirement}",
        ]
        if self.reuse_analysis:
            parts.append(f"  reuse analysis: {self.reuse_analysis}")
        if self.implementation:
            parts.append(f"  implementation: {self.implementation}"
                         + (f" ({self.lines} lines)" if self.lines else ""))
        if self.tests:
            parts.append(f"  tests: {self.tests}")
        if self.verification:
            parts.append(f"  verification: {self.verification}")
        if self.promoted_by:
            parts.append(f"  promoted by {self.promoted_by} at {self.promoted_at}")
        return "\n".join(parts)


class CapabilityError(RuntimeError):
    pass


class CapabilityRegistry:
    def __init__(self, store: Store, *, repo_root: pathlib.Path | None = None, lab: pathlib.Path | None = None):
        self.store = store
        self.repo_root = repo_root or pathlib.Path(__file__).resolve().parent.parent
        self.lab = lab or (self.repo_root / ".livium" / "capability-lab")
        self.promoted_dir = self.repo_root / "livium" / "promoted"

    # -- reads ------------------------------------------------------------
    def get(self, name: str) -> Capability | None:
        row = self.store.one("SELECT * FROM capability WHERE name=?", (name,))
        return _row_to_capability(row) if row else None

    def all(self) -> list[Capability]:
        return [
            _row_to_capability(row)
            for row in self.store.query("SELECT * FROM capability ORDER BY name")
        ]

    def describe_for_context(self, limit: int = 8) -> list[str]:
        return [
            capability.render()
            for capability in self.all()
            if capability.status not in ("rejected", "deprecated")
        ][:limit]

    # -- transitions ------------------------------------------------------
    def propose(
        self,
        *,
        name: str,
        requirement: str,
        reuse_analysis: str,
        rationale: str = "",
        verification_plan: str = "",
        reversible: bool = True,
        task_id: str | None = None,
    ) -> Capability:
        if not _NAME.match(name):
            raise CapabilityError(
                f"capability name {name!r} must be lower_snake_case, 3-40 characters"
            )
        existing = self.get(name)
        if existing is not None and existing.status in ("active", "maintained", "validated"):
            raise CapabilityError(
                f"capability {name!r} already exists with status {existing.status!r}. Extend it "
                f"rather than proposing a duplicate."
            )
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO capability(name, status, requirement, rationale, reuse_analysis, "
                "verification, evidence, created_at, updated_at) "
                "VALUES(?,'proposed',?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET status='proposed', requirement=excluded.requirement, "
                "rationale=excluded.rationale, reuse_analysis=excluded.reuse_analysis, "
                "verification=excluded.verification, updated_at=excluded.updated_at",
                (name, requirement, rationale, reuse_analysis,
                 f"plan: {verification_plan}" if verification_plan else None,
                 f"reversible={reversible}", now(), now()),
            )
        self.store.append_event(
            "capability_proposed",
            {"name": name, "requirement": requirement, "reuse_analysis": reuse_analysis},
            task_id=task_id,
        )
        capability = self.get(name)
        assert capability is not None
        return capability

    def scaffold(self, name: str) -> dict[str, pathlib.Path]:
        """Create the lab directory with an implementation and test skeleton.

        The skeleton is scaffolding for *this* capability, which the agent then
        has to fill in; it is not a stub pretending to be a feature. Status stays
        'experimental' until tests actually pass.
        """

        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}; propose it first")
        directory = self.lab / name
        directory.mkdir(parents=True, exist_ok=True)
        module_path = directory / f"{name}.py"
        test_path = directory / f"test_{name}.py"
        if not module_path.exists():
            module_path.write_text(
                CAPABILITY_TEMPLATE.format(name=name, requirement=capability.requirement),
                encoding="utf-8",
            )
        if not test_path.exists():
            test_path.write_text(
                TEST_TEMPLATE.replace("MODULE", name).replace("MODULE_FILE", f"{name}.py"),
                encoding="utf-8",
            )
        (directory / "__init__.py").write_text("", encoding="utf-8")
        self._set_status(name, "experimental", implementation=str(module_path),
                         tests=str(test_path), lines=_count_lines(module_path))
        self.store.append_event(
            "capability_scaffolded", {"name": name, "dir": str(directory)}
        )
        return {"module": module_path, "tests": test_path, "dir": directory}

    def run_tests(self, name: str, *, timeout: float = 120.0) -> dict[str, Any]:
        """Execute the capability's tests for real and record the raw outcome.

        Uses the same isolation mechanism as the agent's own command execution, so
        a capability under test cannot reach the network on hosts that support
        network namespaces.
        """

        from .execution import Runner

        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}")
        directory = self.lab / name
        if not directory.exists():
            raise CapabilityError(
                f"{directory} does not exist: there is nothing to test. Scaffold and implement "
                f"the capability first."
            )
        test_files = sorted(directory.glob("test_*.py"))
        if not test_files:
            raise CapabilityError(f"no test_*.py in {directory}: nothing would be verified")

        module_path = directory / f"{name}.py"
        markers = placeholder_markers(module_path)
        if markers:
            detail = (
                f"the implementation at {module_path} still contains scaffold placeholders "
                f"({', '.join(sorted(set(markers)))}). Tests cannot validate an unimplemented "
                f"module, so the status stays 'experimental' regardless of the test result. "
                f"Implement the capability and replace the placeholder test."
            )
            self._set_status(
                name, "experimental",
                tests=", ".join(str(p) for p in test_files), lines=_count_lines(module_path),
                verification="NOT validated: module still contains scaffold placeholders",
                evidence=detail,
            )
            self.store.append_event(
                "capability_test_refused", {"name": name, "reason": "placeholders remain"}
            )
            return {
                "name": name, "exit_code": None, "status": "experimental", "passed": False,
                "duration_ms": 0, "stdout": "", "stderr": detail, "evidence": detail,
            }

        runner = Runner(max_seconds=timeout, max_output_bytes=40_000)
        argv = ["python3", "-m", "unittest", "discover", "-s", str(directory),
                "-p", "test_*.py", "-v"]
        result = runner.run(argv, cwd=directory)
        lines = _count_lines(directory / f"{name}.py")
        passed = result.exit_code == 0
        evidence = (
            f"argv={argv}\n"
            f"exit_code={result.exit_code} duration_ms={result.duration_ms} "
            f"network_isolated={result.network_isolated}\n"
            f"--- stderr (test runner output) ---\n{result.stderr[-6000:]}"
        )
        status = "validated" if passed else "experimental"
        self._set_status(
            name, status,
            tests=", ".join(str(p) for p in test_files),
            lines=lines,
            verification=("tests executed and passed" if passed
                          else "tests executed and FAILED"),
            evidence=evidence,
        )
        self.store.append_event(
            "capability_tested",
            {"name": name, "exit_code": result.exit_code, "status": status,
             "duration_ms": result.duration_ms, "network_isolated": result.network_isolated},
        )
        return {
            "name": name, "exit_code": result.exit_code, "status": status,
            "passed": passed, "duration_ms": result.duration_ms,
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-8000:],
            "evidence": evidence,
        }

    def promote(self, name: str, *, approved_by: str, guardian_check) -> Capability:
        """Copy a validated capability into the package so it actually loads.

        Requires: human attribution, status 'validated', and a passing guardian
        integrity check. Promotion is the only path that makes a new capability
        callable, and the agent cannot call it.
        """

        if not approved_by or approved_by.strip().lower() in ("agent", "livium", "self", "auto"):
            raise CapabilityError(
                "promotion requires a named human approver who is not the agent; "
                "self-approval is not accepted"
            )
        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}")
        if capability.status != "validated":
            raise CapabilityError(
                f"capability {name!r} is {capability.status!r}, not 'validated'. Run its tests "
                f"first and make them pass; passing tests are a precondition, not a formality."
            )
        check = guardian_check()
        if not check.get("ok"):
            raise CapabilityError(
                f"guardian integrity check failed, so promotion is blocked: {check.get('detail')}"
            )
        source = self.lab / name / f"{name}.py"
        if not source.exists():
            raise CapabilityError(f"lab implementation missing: {source}")
        changed, detail = validate_capability_module(source, expected_name=name)
        if not changed:
            raise CapabilityError(f"capability module failed validation: {detail}")

        self.promoted_dir.mkdir(parents=True, exist_ok=True)
        destination = self.promoted_dir / f"{name}.py"
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()

        self._set_status(
            name, "active", implementation=str(destination), promoted_by=approved_by.strip(),
            verification=(capability.verification or "") + f"; promoted after SHA-256 {digest[:16]}",
        )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE capability SET promoted_at=?, promoted_by=? WHERE name=?",
                (now(), approved_by.strip(), name),
            )
        self.store.append_event(
            "capability_promoted",
            {"name": name, "approved_by": approved_by.strip(), "path": str(destination),
             "sha256": digest},
        )
        promoted = self.get(name)
        assert promoted is not None
        return promoted

    def rollback(self, name: str, *, reason: str) -> Capability:
        """Withdraw an active capability: remove the promoted file and record why."""

        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}")
        target = self.promoted_dir / f"{name}.py"
        removed = False
        if target.exists():
            target.unlink()
            removed = True
        self._set_status(name, "rollback", verification=f"rolled back: {reason}")
        self.store.append_event(
            "capability_rollback", {"name": name, "reason": reason, "file_removed": removed}
        )
        rolled = self.get(name)
        assert rolled is not None
        return rolled

    def reject(self, name: str, *, reason: str) -> Capability:
        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}")
        self._set_status(name, "rejected", verification=f"rejected: {reason}")
        self.store.append_event("capability_rejected", {"name": name, "reason": reason})
        rejected = self.get(name)
        assert rejected is not None
        return rejected

    # -- internals --------------------------------------------------------
    def _set_status(self, name: str, status: str, **fields: Any) -> None:
        if status not in STATUSES:
            raise CapabilityError(f"unknown status {status!r}")
        current = self.get(name)
        if current and current.status != status:
            allowed = VALID_TRANSITIONS.get(current.status, set())
            if status not in allowed and current.status != status:
                raise CapabilityError(
                    f"illegal transition {current.status!r} -> {status!r} for {name!r}; "
                    f"allowed: {', '.join(sorted(allowed)) or 'none'}"
                )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE capability SET status=?, updated_at=? WHERE name=?", (status, now(), name)
            )
            for column, value in fields.items():
                if value is None:
                    continue
                conn.execute(
                    f"UPDATE capability SET {column}=? WHERE name=?",  # noqa: S608 - fixed keys
                    (value, name),
                )


def placeholder_markers(path: pathlib.Path) -> list[str]:
    """Scaffold markers left in an implementation module.

    Kept deliberately blunt and tiny: the marker exists so that a skeleton cannot be
    mistaken for a capability. It is a precondition check, not a code-quality gate.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [marker for marker in ("TODO:", "TODO", "NotImplemented") if marker in text]


def _count_lines(path: pathlib.Path) -> int | None:
    try:
        return sum(
            1 for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    except OSError:
        return None


def validate_capability_module(path: pathlib.Path, *, expected_name: str | None = None) -> tuple[bool, str]:
    """Check a candidate module really conforms to the capability contract.

    Imported in a subprocess so that a malformed module cannot take down the
    promoting process, and so import-time side effects are contained.
    """

    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('candidate', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "cap = getattr(module, 'CAPABILITY', None)\n"
        "handler = getattr(module, 'handle', None)\n"
        "out = {'cap': bool(cap), 'handler': callable(handler),\n"
        "       'name': (cap or {}).get('name'),\n"
        "       'tools': [t.get('name') for t in (cap or {}).get('tools', [])],\n"
        "       'has_requirement': bool((cap or {}).get('requirement')),\n"
        "       'descriptions': all(bool(t.get('description')) for t in (cap or {}).get('tools', []))}\n"
        "print(json.dumps(out))\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not import candidate: {type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return False, (
            f"candidate raised while importing: "
            f"{completed.stderr.decode(errors='replace')[-1500:]}"
        )
    try:
        info = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"candidate import produced no parseable report: {completed.stdout[:300]!r}"

    if not info["cap"] or not info["handler"]:
        return False, "module must define both CAPABILITY and a callable handle()"
    if not info["has_requirement"]:
        return False, "CAPABILITY must state the requirement it addresses"
    if not info["descriptions"]:
        return False, "every declared tool needs a description the model can act on"
    if expected_name and info["name"] != expected_name:
        return False, f"CAPABILITY name {info['name']!r} does not match {expected_name!r}"
    markers = placeholder_markers(path)
    if markers:
        return False, (
            f"module still contains scaffold placeholders: {', '.join(sorted(set(markers)))}"
        )
    bad = [t for t in info["tools"] if not t or t.startswith("_")]
    if bad:
        return False, f"tool names must be public identifiers; got {bad}"
    return True, f"conforms; tools={info['tools']}"


def _row_to_capability(row) -> Capability:
    return Capability(
        name=row["name"], status=row["status"], requirement=row["requirement"],
        rationale=row["rationale"], implementation=row["implementation"], tests=row["tests"],
        verification=row["verification"], evidence=row["evidence"],
        reuse_analysis=row["reuse_analysis"], lines=row["lines"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        promoted_at=row["promoted_at"], promoted_by=row["promoted_by"],
    )
