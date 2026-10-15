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

from .permissions import UNATTRIBUTED_NAMES
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
    # Recorded in the registry when this is promoted, so a caller can see what the
    # capability needs and what it does not do. Fill both in honestly: an empty
    # limitations list on a nontrivial tool is usually a sign nobody looked.
    "dependencies": [],
    "permissions": [],
    "limitations": "TODO: known limits, or 'none known' only if you checked.",
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
    description: str | None = None
    #: The tools the capability provides, as ``[{name, description, parameters}]``.
    #: Empty until the implementation exists: a registry that guessed at an interface
    #: before the code was written would be describing something that is not there.
    tools: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    dependencies: list[str] = dataclasses.field(default_factory=list)
    required_permissions: list[str] = dataclasses.field(default_factory=list)
    implementation_ref: str | None = None
    limitations: str | None = None
    version: int = 1

    @property
    def validated(self) -> bool:
        """True only if this capability passed its tests and was promoted.

        The registry distinguishes *registered* from *working*: 'proposed' and
        'experimental' mean the code either does not exist or has not been shown to
        work, and anything selecting capabilities must treat them differently.
        """

        return self.status in ("active", "maintained")

    @property
    def provides(self) -> tuple[str, ...]:
        return tuple(str(tool.get("name")) for tool in self.tools if tool.get("name"))

    def as_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["validated"] = self.validated
        payload["provides"] = list(self.provides)
        return payload

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
    def __init__(self, store: Store, *, repo_root: pathlib.Path | None = None,
                 lab: pathlib.Path | None = None, promoted: pathlib.Path | None = None):
        self.store = store
        self.repo_root = repo_root or pathlib.Path(__file__).resolve().parent.parent
        self.lab = lab or (self.repo_root / ".livium" / "capability-lab")
        self.promoted_dir = promoted or (self.repo_root / "livium" / "promoted")

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

    def sync_metadata(self, name: str) -> dict[str, Any] | None:
        """Re-read a capability's declaration from the module it points at.

        A row can legitimately be older than the metadata columns: a capability promoted
        before the registry recorded interfaces would otherwise be selected on a
        description nobody can act on. This copies what the module *declares* -- it never
        touches status, verification, or who promoted what, so it cannot launder an
        unvalidated module into a validated one.
        """

        capability = self.get(name)
        if capability is None or not capability.implementation:
            return None
        path = pathlib.Path(capability.implementation)
        if not path.exists():
            self.store.append_event(
                "capability_metadata_refresh_failed",
                {"name": name, "reason": f"implementation missing: {path}"},
            )
            return None
        ok, detail, declaration = inspect_capability_module(path, expected_name=name)
        if not ok:
            self.store.append_event(
                "capability_metadata_refresh_failed", {"name": name, "reason": detail}
            )
            return None
        tools = [t for t in (declaration.get("tools") or []) if isinstance(t, dict)]
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE capability SET description=?, tools=?, dependencies=?, "
                "required_permissions=?, limitations=?, implementation_ref=?, updated_at=? "
                "WHERE name=?",
                (
                    declaration.get("description") or capability.description,
                    json.dumps(tools),
                    json.dumps(list(declaration.get("dependencies") or [])),
                    json.dumps(list(declaration.get("permissions") or [])),
                    declaration.get("limitations") or capability.limitations,
                    str(path), now(), name,
                ),
            )
        return {"name": name, "tools": [t.get("name") for t in tools],
                "description": declaration.get("description")}

    def interface_for(self, name: str) -> dict[str, Any] | None:
        """The detailed interface of one capability, or None if there is not one yet.

        This is the lazy half of discovery: the context carries a one-line catalog
        entry, and the full argument schemas are fetched only when a capability is
        actually being used.
        """

        capability = self.get(name)
        if capability is None or not capability.validated:
            return None
        return {
            "name": capability.name,
            "description": capability.description or capability.requirement,
            "tools": capability.tools,
            "dependencies": capability.dependencies,
            "required_permissions": capability.required_permissions,
            "limitations": capability.limitations,
            "implementation": capability.implementation_ref or capability.implementation,
            "version": capability.version,
            "validation": capability.status,
        }

    def search(self, query: str, *, limit: int = 5,
               include_unvalidated: bool = False) -> list[dict[str, Any]]:
        """Keyword search over the registry, with the truth about each match attached.

        Structured queries and term overlap only: no embeddings, no vector store. Each
        result says whether the capability can actually be used (``validated``) and what
        it provides, so a caller cannot mistake a proposal for a working tool.
        """

        terms = {t for t in re.findall(r"[a-z0-9_]+", query.lower()) if len(t) >= 3}
        results: list[dict[str, Any]] = []
        for capability in self.all():
            if capability.status in ("rejected", "deprecated"):
                continue
            if not include_unvalidated and not capability.validated:
                continue
            haystack = " ".join(filter(None, [
                capability.name.replace("_", " "), capability.requirement,
                capability.description or "", capability.limitations or "",
                " ".join(capability.provides),
            ])).lower()
            overlap = sorted(term for term in terms if term in haystack)
            if not overlap:
                continue
            results.append({
                "name": capability.name,
                "status": capability.status,
                "validated": capability.validated,
                "description": capability.description or capability.requirement,
                "tools": list(capability.provides),
                "required_permissions": capability.required_permissions,
                "limitations": capability.limitations,
                "matched_terms": overlap,
                "score": len(overlap),
            })
        results.sort(key=lambda item: (-item["score"], item["name"]))
        return results[:limit]

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
        description: str = "",
        dependencies: Sequence[str] = (),
        required_permissions: Sequence[str] = (),
        limitations: str = "",
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
                "verification, evidence, created_at, updated_at, description, dependencies, "
                "required_permissions, limitations) "
                "VALUES(?,'proposed',?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET status='proposed', requirement=excluded.requirement, "
                "rationale=excluded.rationale, reuse_analysis=excluded.reuse_analysis, "
                "verification=excluded.verification, description=excluded.description, "
                "dependencies=excluded.dependencies, "
                "required_permissions=excluded.required_permissions, "
                "limitations=excluded.limitations, updated_at=excluded.updated_at",
                (name, requirement, rationale, reuse_analysis,
                 f"plan: {verification_plan}" if verification_plan else None,
                 f"reversible={reversible}", now(), now(), description or None,
                 json.dumps(list(dependencies)), json.dumps(list(required_permissions)),
                 limitations or None),
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

        if not approved_by or approved_by.strip().lower() in UNATTRIBUTED_NAMES:
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
        changed, detail, declaration = inspect_capability_module(source, expected_name=name)
        if not changed:
            raise CapabilityError(f"capability module failed validation: {detail}")

        self.promoted_dir.mkdir(parents=True, exist_ok=True)
        destination = self.promoted_dir / f"{name}.py"
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()

        # Record the interface the module actually declares, not one typed into the
        # proposal: the registry's metadata has to describe code that exists.
        tools = [t for t in (declaration.get("tools") or []) if isinstance(t, dict)]
        self._set_status(
            name, "active", implementation=str(destination), promoted_by=approved_by.strip(),
            verification=(capability.verification or "") + f"; promoted after SHA-256 {digest[:16]}",
            description=declaration.get("description") or capability.description,
            tools=json.dumps(tools),
            dependencies=json.dumps(list(declaration.get("dependencies") or [])),
            required_permissions=json.dumps(list(declaration.get("permissions") or [])),
            limitations=declaration.get("limitations") or capability.limitations,
            implementation_ref=str(destination),
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


def inspect_capability_module(path: pathlib.Path, *, expected_name: str | None = None
                              ) -> tuple[bool, str, dict[str, Any]]:
    """Validate a module and return its declaration.

    The declaration is what the registry records, so capability metadata comes from the
    module that actually exists rather than from what someone typed into a proposal.
    """

    ok, detail, declaration = _probe_capability_module(path, expected_name=expected_name)
    return ok, detail, declaration


def _probe_capability_module(path: pathlib.Path, *, expected_name: str | None = None
                            ) -> tuple[bool, str, dict[str, Any]]:
    """Check a candidate module really conforms to the capability contract.

    Imported in a subprocess so that a malformed module cannot take down the
    promoting process, and so import-time side effects are contained.
    """

    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('candidate', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "cap = getattr(module, 'CAPABILITY', None) or {}\n"
        "handler = getattr(module, 'handle', None)\n"
        "tools = cap.get('tools', []) or []\n"
        "out = {'cap': bool(cap), 'handler': callable(handler),\n"
        "       'name': cap.get('name'),\n"
        "       'tools': [t.get('name') for t in tools],\n"
        "       'declaration': {'name': cap.get('name'), 'description': cap.get('description'),\n"
        "                       'requirement': cap.get('requirement'),\n"
        "                       'limitations': cap.get('limitations'),\n"
        "                       'dependencies': cap.get('dependencies') or [],\n"
        "                       'permissions': cap.get('permissions') or [],\n"
        "                       'tools': [{'name': t.get('name'),\n"
        "                                  'description': t.get('description'),\n"
        "                                  'parameters': t.get('parameters') or {},\n"
        "                                  'side_effects': bool(t.get('side_effects', False))}\n"
        "                                 for t in tools if isinstance(t, dict)]},\n"
        "       'has_requirement': bool(cap.get('requirement')),\n"
        "       'has_description': bool(cap.get('description')),\n"
        "       'descriptions': all(bool(t.get('description')) for t in tools)}\n"
        "print(json.dumps(out))\n"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not import candidate: {type(exc).__name__}: {exc}", {}
    if completed.returncode != 0:
        return False, (
            f"candidate raised while importing: "
            f"{completed.stderr.decode(errors='replace')[-1500:]}"
        ), {}
    try:
        info = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"candidate import produced no parseable report: {completed.stdout[:300]!r}", {}

    declaration = dict(info.get("declaration") or {})
    if not info["cap"] or not info["handler"]:
        return False, "module must define both CAPABILITY and a callable handle()", declaration
    if not info["has_requirement"]:
        return False, "CAPABILITY must state the requirement it addresses", declaration
    if not info["descriptions"]:
        return False, "every declared tool needs a description the model can act on", declaration
    if expected_name and info["name"] != expected_name:
        return False, (f"CAPABILITY name {info['name']!r} does not match {expected_name!r}",
                       declaration)
    markers = placeholder_markers(path)
    if markers:
        return False, (
            f"module still contains scaffold placeholders: {', '.join(sorted(set(markers)))}"
        ), declaration
    bad = [t for t in info["tools"] if not t or t.startswith("_")]
    if bad:
        return False, f"tool names must be public identifiers; got {bad}", declaration
    return True, f"conforms; tools={info['tools']}", declaration


def validate_capability_module(path: pathlib.Path, *, expected_name: str | None = None
                               ) -> tuple[bool, str]:
    """Check a candidate module conforms to the capability contract (ok, detail)."""

    ok, detail, _declaration = _probe_capability_module(path, expected_name=expected_name)
    return ok, detail


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row_to_capability(row) -> Capability:
    keys = set(row.keys())
    return Capability(
        name=row["name"], status=row["status"], requirement=row["requirement"],
        rationale=row["rationale"], implementation=row["implementation"], tests=row["tests"],
        verification=row["verification"], evidence=row["evidence"],
        reuse_analysis=row["reuse_analysis"], lines=row["lines"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        promoted_at=row["promoted_at"], promoted_by=row["promoted_by"],
        description=row["description"] if "description" in keys else None,
        tools=_json_list(row["tools"]) if "tools" in keys else [],
        dependencies=[str(d) for d in _json_list(row["dependencies"])] if "dependencies" in keys else [],
        required_permissions=[str(d) for d in _json_list(row["required_permissions"])]
        if "required_permissions" in keys else [],
        implementation_ref=row["implementation_ref"] if "implementation_ref" in keys else None,
        limitations=row["limitations"] if "limitations" in keys else None,
        version=row["version"] if "version" in keys and row["version"] is not None else 1,
    )
