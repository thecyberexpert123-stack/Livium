"""Capability lifecycle: Proposed -> Experimental -> Validated -> Active -> Maintained
-> Deprecated.

The point of this module is that the stages have teeth. "Validated" is only
reachable by executing the capability's tests and recording their real output;
"Active" additionally requires a human to run the promote command, because the
agent has no tool that can promote anything. Promotion copies the exact tested
package into `$LIVIUM_HOME/promoted/NAME/`, where the loader checks the approved
manifest before import and exposes its tools on the next run -- growth is an
actual change in what the agent can do, not a record that says it can.

Deliberately absent: a registry of capabilities that do not exist. Every row here
is created either by the agent's proposal tool (status=proposed, no
implementation) or by a real test run/promotion, and the row carries the paths
and evidence that justify its status.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
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
    validated_manifest: dict[str, str] | None = None
    promoted_manifest: dict[str, str] | None = None

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


def file_manifest(directory: pathlib.Path) -> dict[str, str]:
    """Hash *all* authored lab files, including tests, helpers and data files.

    A digest of only the entrypoint allows tested code to import an untested helper;
    a digest of only the implementation allows tests to change after validation.
    Cache files created by Python are excluded, never followed. Symlinks and special
    files are refused, rather than copied outside the lab during promotion.
    """

    if directory.is_symlink() or not directory.is_dir():
        raise CapabilityError(f"capability directory missing or a symlink: {directory}")
    result: dict[str, str] = {}
    for child in sorted(directory.rglob("*")):
        relative = child.relative_to(directory)
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts or child.suffix == ".pyc":
            continue
        if child.is_symlink():
            raise CapabilityError(f"symlink in capability lab is not installable: {child}")
        if child.is_dir():
            continue
        if not child.is_file():
            raise CapabilityError(f"non-regular capability file: {child}")
        with child.open("rb") as handle:
            result[relative.as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def manifest_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json_manifest(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                           for k, v in value.items()):
        return None
    return value


class CapabilityRegistry:
    def __init__(self, store: Store, *, repo_root: pathlib.Path | None = None,
                 lab: pathlib.Path | None = None, promoted: pathlib.Path | None = None):
        self.store = store
        self.repo_root = repo_root or pathlib.Path(__file__).resolve().parent.parent
        self.lab = lab or (self.repo_root / ".livium" / "capability-lab")
        self.promoted_dir = promoted or (self.repo_root / ".livium" / "promoted")

    # -- reads ------------------------------------------------------------
    def get(self, name: str) -> Capability | None:
        row = self.store.one("SELECT * FROM capability WHERE name=?", (name,))
        return _row_to_capability(row) if row else None

    def all(self) -> list[Capability]:
        return [
            _row_to_capability(row)
            for row in self.store.query("SELECT * FROM capability ORDER BY name")
        ]

    def review(self, name: str) -> dict[str, Any]:
        """Read-only evidence for a human: tested bytes vs current lab/installation.

        Does not import code or assert test quality. A matching manifest says the
        files have not changed since the reported tests, not that they are correct.
        """

        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}")
        tested = capability.validated_manifest
        installed = capability.promoted_manifest

        def observed(directory: pathlib.Path, expected: dict[str, str] | None) -> dict[str, Any]:
            try:
                actual = file_manifest(directory) if directory.exists() else None
                error = None
            except (CapabilityError, OSError) as exc:
                actual = None
                error = str(exc)
            return {"expected": expected, "expected_sha256": manifest_digest(expected)
                    if expected else None, "observed": actual,
                    "observed_sha256": manifest_digest(actual) if actual else None,
                    "unchanged": actual == expected if expected is not None else False,
                    "error": error}

        lab = observed(self.lab / name, tested)
        promoted = observed(self.promoted_dir / name, installed)
        return {
            "capability": capability.as_dict(), "lab": lab, "promoted": promoted,
            "eligible_for_human_review": capability.status == "validated" and lab["unchanged"],
            "note": "A passing test and matching hash prove only that these exact files "
                    "passed those tests. A named human must review what they test and "
                    "the Guardian must permit promotion; arbitrary Python is not sandboxed.",
        }

    def approved_modules(self) -> dict[str, dict[str, str]]:
        """The DB's active, evidence-bound installation allowlist for the loader.

        A file dropped in promoted/ or a legacy row without a recorded manifest is
        not authority to execute code. The loader compares the actual files against
        these recorded digests before it imports anything.
        """

        return {c.name: c.promoted_manifest for c in self.all()
                if c.status in ("active", "maintained") and c.promoted_manifest}

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
        if capability is None or capability.status not in ("active", "maintained"):
            return None
        path = pathlib.Path(capability.implementation or "")
        if not capability.promoted_manifest or path.parent != self.promoted_dir / name:
            self.store.append_event(
                "capability_metadata_refresh_failed",
                {"name": name, "reason": "no evidence-bound promoted package is recorded"},
            )
            return None
        try:
            if file_manifest(path.parent) != capability.promoted_manifest:
                raise CapabilityError("the promoted files no longer match the approved manifest")
        except CapabilityError as exc:
            self.store.append_event("capability_metadata_refresh_failed",
                                    {"name": name, "reason": str(exc)})
            return None
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
                "limitations=excluded.limitations, evidence=excluded.evidence, "
                "implementation=NULL, tests=NULL, tools='[]', "
                "validated_manifest=NULL, promoted_manifest=NULL, "
                "promoted_at=NULL, promoted_by=NULL, version=capability.version+1, "
                "updated_at=excluded.updated_at",
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

    def scaffold(self, name: str, *, task_id: str | None = None) -> dict[str, pathlib.Path]:
        """Create the lab directory with an implementation and test skeleton.

        The skeleton is scaffolding for *this* capability, which the agent then
        has to fill in; it is not a stub pretending to be a feature. Status stays
        'experimental' until tests actually pass.
        """

        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}; propose it first")
        if capability.status not in ("proposed", "experimental"):
            raise CapabilityError(f"cannot scaffold {name!r} while it is {capability.status!r}")
        directory = self.lab / name
        if directory.is_symlink():
            raise CapabilityError(f"lab directory is a symlink: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        module_path = directory / f"{name}.py"
        test_path = directory / f"test_{name}.py"
        init_path = directory / "__init__.py"
        for candidate in (module_path, test_path, init_path):
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
                raise CapabilityError(f"scaffold path is not a regular lab file: {candidate}")
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
        init_path.touch(exist_ok=True)
        self._set_status(name, "experimental", implementation=str(module_path),
                         tests=str(test_path), lines=_count_lines(module_path),
                         validated_manifest="")
        self.store.append_event(
            "capability_scaffolded", {"name": name, "dir": str(directory)}, task_id=task_id
        )
        return {"module": module_path, "tests": test_path, "dir": directory}

    def run_tests(self, name: str, *, timeout: float = 120.0,
                  task_id: str | None = None) -> dict[str, Any]:
        """Execute the candidate's real tests, then bind evidence to all its files.

        A passing test suite is evidence for the exact files it exercised, *not* a
        promise about arbitrary later changes. It makes the candidate eligible for a
        human review; the reviewer still has to decide whether the assertions cover
        the requirement. Tests run under Runner's actual isolation report; the code
        does not call this a filesystem sandbox where none exists.
        """

        from .execution import Runner

        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}")
        if capability.status not in ("proposed", "experimental", "validated"):
            raise CapabilityError(f"cannot test {name!r} while it is {capability.status!r}")
        directory = self.lab / name
        if not directory.is_dir() or directory.is_symlink():
            raise CapabilityError(
                f"{directory} does not exist (or is a symlink): there is nothing to test. "
                "Scaffold or implement the capability first."
            )
        test_files = sorted(directory.glob("test_*.py"))
        if not test_files:
            raise CapabilityError(f"no test_*.py in {directory}: nothing would be verified")
        module_path = directory / f"{name}.py"
        if not module_path.is_file() or module_path.is_symlink():
            raise CapabilityError(f"implementation missing or a symlink: {module_path}")
        if capability.status == "proposed":
            # Writing real code directly to the lab need not require a placeholder
            # scaffold. It still passes through Experimental before validation.
            self._set_status(name, "experimental", implementation=str(module_path),
                             tests=", ".join(str(p) for p in test_files))

        def refused(detail: str) -> dict[str, Any]:
            self._set_status(
                name, "experimental", implementation=str(module_path),
                tests=", ".join(str(p) for p in test_files),
                lines=_count_lines(module_path),
                verification="NOT validated: " + detail[:200], evidence=detail,
                validated_manifest="",
            )
            self.store.append_event(
                "capability_test_refused", {"name": name, "reason": detail[:500]},
                task_id=task_id,
            )
            return {"name": name, "exit_code": None, "status": "experimental",
                    "passed": False, "duration_ms": 0, "stdout": "", "stderr": detail,
                    "evidence": detail, "manifest_sha256": None}

        try:
            before = file_manifest(directory)
        except CapabilityError as exc:
            return refused(str(exc))
        markers = placeholder_markers(module_path)
        if markers:
            return refused(
                f"the implementation at {module_path} still contains scaffold placeholders "
                f"({', '.join(sorted(set(markers)))}). Tests cannot validate an "
                "unimplemented module. Replace the placeholders and test actual behaviour."
            )

        runner = Runner(max_seconds=timeout, max_output_bytes=40_000)
        argv = [sys.executable, "-m", "unittest", "discover", "-s", str(directory),
                "-p", "test_*.py", "-v"]
        result = runner.run(argv, cwd=directory)
        try:
            after = file_manifest(directory)
        except CapabilityError as exc:
            return refused(f"tests altered the lab into an unsafe state: {exc}")
        # A test may create a non-executable fixture beside itself. Record that
        # fixture in the manifest reviewed for promotion. But an existing file
        # altered during testing, or a NEW executable module, makes the result
        # ambiguous: the tested code and the reviewed code may differ.
        new_files = sorted(after.keys() - before.keys())
        changed = any(after.get(file) != digest for file, digest in before.items()) or any(
            file.endswith((".py", ".pyi", ".so", ".pyd")) for file in new_files
        )
        passed = result.exit_code == 0 and not changed
        interface_detail = "not checked (tests failed)"
        declaration: dict[str, Any] = {}
        if passed:
            passed, interface_detail, declaration = inspect_capability_module(
                module_path, expected_name=name
            )
        manifest = after if passed else None
        evidence = (
            f"argv={argv}\n"
            f"exit_code={result.exit_code} duration_ms={result.duration_ms} "
            f"network_isolated={result.network_isolated} "
            f"filesystem_confined={runner.isolation.filesystem_confined}\n"
            f"lab_changed_during_test={changed} created_files={new_files} interface={interface_detail}\n"
            f"manifest_sha256={manifest_digest(manifest) if manifest is not None else 'not validated'}\n"
            f"--- stderr (test runner output) ---\n{result.stderr[-6000:]}"
        )
        status = "validated" if passed else "experimental"
        self._set_status(
            name, status, implementation=str(module_path),
            tests=", ".join(str(p) for p in test_files),
            lines=_count_lines(module_path),
            verification=("tests passed; source and test digests recorded" if passed
                          else "tests or interface FAILED; not validated"),
            evidence=evidence,
            validated_manifest=json.dumps(manifest, sort_keys=True) if manifest is not None else "",
            description=declaration.get("description") or capability.description,
            tools=json.dumps(declaration.get("tools") or []) if passed else "[]",
            required_permissions=json.dumps(declaration.get("permissions") or []) if passed else "[]",
            dependencies=json.dumps(declaration.get("dependencies") or []) if passed else "[]",
        )
        self.store.append_event(
            "capability_tested",
            {"name": name, "exit_code": result.exit_code, "status": status,
             "duration_ms": result.duration_ms,
             "network_isolated": result.network_isolated,
             "filesystem_confined": runner.isolation.filesystem_confined,
             "manifest_sha256": manifest_digest(manifest) if manifest is not None else None,
             "lab_changed_during_test": changed, "interface": interface_detail},
            task_id=task_id,
        )
        return {
            "name": name, "exit_code": result.exit_code, "status": status,
            "passed": passed, "duration_ms": result.duration_ms,
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-8000:],
            "evidence": evidence,
            "manifest_sha256": manifest_digest(manifest) if manifest is not None else None,
            "files": manifest if manifest is not None else None,
        }

    def promote(self, name: str, *, approved_by: str, guardian_check) -> Capability:
        """Human promotion of the exact implementation *and tests* that passed.

        The whole lab package is installed, not just its entrypoint: relative
        imports and non-Python resources therefore work after promotion. The
        installed snapshot is hashed before it can become Active. This gate checks
        attributed review and evidence, not a model's confidence; it is NOT an OS
        security boundary against arbitrary Python running as the same user.
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
        if not capability.validated_manifest:
            raise CapabilityError(
                "the previous test run did not record file digests; re-run capability test "
                "before promotion, so the reviewed code is the tested code"
            )
        check = guardian_check()
        if not check.get("ok"):
            raise CapabilityError(
                f"guardian integrity check failed, so promotion is blocked: {check.get('detail')}"
            )
        source_dir = self.lab / name
        source = source_dir / f"{name}.py"
        current = file_manifest(source_dir)
        if current != capability.validated_manifest:
            raise CapabilityError(
                "the lab changed since its tests passed; re-run capability test and review "
                "the new manifest before promoting it"
            )
        changed, detail, declaration = inspect_capability_module(source, expected_name=name)
        if not changed:
            raise CapabilityError(f"capability module failed validation: {detail}")
        # The import-time probe can itself change lab contents. Check again.
        if file_manifest(source_dir) != current:
            raise CapabilityError("the lab changed during the interface check; re-test it")

        if self.promoted_dir.is_symlink():
            raise CapabilityError("the promoted directory is a symlink; promotion refused")
        self.promoted_dir.mkdir(parents=True, exist_ok=True)
        destination_dir = self.promoted_dir / name
        flat_legacy = self.promoted_dir / f"{name}.py"
        if destination_dir.exists() or destination_dir.is_symlink() or flat_legacy.exists():
            raise CapabilityError(
                f"a promoted copy of {name!r} already exists; rollback and inspect it first"
            )
        # Copy into a temporary sibling; an incomplete package cannot be loaded.
        with tempfile.TemporaryDirectory(prefix=".livium-promote-", dir=self.promoted_dir) as tmp:
            staged = pathlib.Path(tmp) / name
            shutil.copytree(source_dir, staged, ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "*.pyc"))
            if file_manifest(staged) != current:
                raise CapabilityError(
                    "the copied package differs from the tested files; promotion refused"
                )
            os.replace(staged, destination_dir)
        digest = manifest_digest(current)
        destination = destination_dir / f"{name}.py"
        # Record what the module actually declares, not a speculative proposal.
        tools = [t for t in (declaration.get("tools") or []) if isinstance(t, dict)]
        self._set_status(
            name, "active", implementation=str(destination), promoted_by=approved_by.strip(),
            verification=(capability.verification or "") + f"; promoted manifest SHA-256 {digest}",
            description=declaration.get("description") or capability.description,
            tools=json.dumps(tools),
            dependencies=json.dumps(list(declaration.get("dependencies") or [])),
            required_permissions=json.dumps(list(declaration.get("permissions") or [])),
            limitations=declaration.get("limitations") or capability.limitations,
            implementation_ref=str(destination),
            promoted_manifest=json.dumps(current, sort_keys=True),
        )
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE capability SET promoted_at=?, promoted_by=? WHERE name=?",
                (now(), approved_by.strip(), name),
            )
        self.store.append_event(
            "capability_promoted",
            {"name": name, "approved_by": approved_by.strip(), "path": str(destination),
             "sha256": digest, "files": current},
        )
        promoted = self.get(name)
        assert promoted is not None
        return promoted

    def rollback(self, name: str, *, reason: str) -> Capability:
        """Withdraw an approved package; the loader also rejects its DB status."""

        capability = self.get(name)
        if capability is None:
            raise CapabilityError(f"unknown capability {name!r}")
        target = self.promoted_dir / name
        flat_legacy = self.promoted_dir / f"{name}.py"
        removed = False
        if target.is_symlink():
            target.unlink()
            removed = True
        elif target.is_dir():
            shutil.rmtree(target)
            removed = True
        if flat_legacy.exists():
            flat_legacy.unlink()
            removed = True
        self._set_status(name, "rollback", verification=f"rolled back: {reason}",
                         promoted_manifest="")
        self.store.append_event(
            "capability_rollback", {"name": name, "reason": reason, "file_removed": removed}
        )
        rolled = self.get(name)
        assert rolled is not None
        return rolled

    def deprecate(self, name: str, *, approved_by: str, reason: str) -> Capability:
        """A named human retires an Active/Maintained tool without erasing history."""

        if not approved_by or approved_by.strip().lower() in UNATTRIBUTED_NAMES:
            raise CapabilityError("deprecation requires a named human, not the agent")
        if not reason.strip():
            raise CapabilityError("deprecation needs a reason")
        capability = self.get(name)
        if capability is None or capability.status not in ("active", "maintained"):
            raise CapabilityError(f"{name!r} is not an active/maintained capability")
        target = self.promoted_dir / name
        legacy = self.promoted_dir / f"{name}.py"
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        if legacy.exists():
            legacy.unlink()
        self._set_status(name, "deprecated", verification=f"deprecated by {approved_by}: {reason}",
                         promoted_manifest="")
        self.store.append_event(
            "capability_deprecated", {"name": name, "by": approved_by.strip(),
                                      "reason": reason, "former_status": capability.status,
                                      "former_manifest_sha256": manifest_digest(
                                          capability.promoted_manifest or {})},
        )
        record = self.get(name)
        assert record is not None
        return record

    def mark_used(self, name: str, *, tool: str, task_id: str | None = None) -> None:
        """The first successful call makes an Active capability Maintained."""

        capability = self.get(name)
        if capability is None or capability.status not in ("active", "maintained"):
            raise CapabilityError(f"{name!r} is not an active capability")
        if capability.status == "active":
            self._set_status(name, "maintained")
            self.store.append_event(
                "capability_maintained", {"name": name, "tool": tool,
                                          "reason": "first successful invocation"},
                task_id=task_id,
            )

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

    Imported through Runner with a scrubbed environment, resource limits and the
    network boundary the host actually supports. It is *not* filesystem confined:
    imported Python still has this user's filesystem privileges. The result is
    only an interface probe, never a safety certification.
    """

    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('candidate', sys.argv[1], "
        "submodule_search_locations=[str(__import__('pathlib').Path(sys.argv[1]).parent)])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['candidate'] = module\n"
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
        "       'schemas': bool(tools) and all(isinstance(t, dict) and "
        "isinstance(t.get('name'), str) and bool(t.get('description')) and "
        "isinstance(t.get('parameters'), dict) and "
        "t['parameters'].get('type') == 'object' for t in tools)}\n"
        "print(json.dumps(out))\n"
    )
    from .execution import Runner

    runner = Runner(max_seconds=15, max_output_bytes=150_000)
    completed = runner.run([sys.executable, "-c", probe, str(path)],
                           cwd=path.parent, timeout=15)
    if completed.exit_code != 0:
        return False, (
            f"candidate raised while importing (exit {completed.exit_code}, "
            f"network_isolated={completed.network_isolated}): "
            f"{completed.stderr[-1500:]}"
        ), {}
    try:
        info = json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"candidate import produced no parseable report: {completed.stdout[:300]!r}", {}

    declaration = dict(info.get("declaration") or {})
    if not info["cap"] or not info["handler"]:
        return False, "module must define both CAPABILITY and a callable handle()", declaration
    if not info["has_requirement"]:
        return False, "CAPABILITY must state the requirement it addresses", declaration
    if not info["schemas"]:
        return False, "declare at least one tool with a name, description and object schema", declaration
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
        validated_manifest=_json_manifest(row["validated_manifest"])
        if "validated_manifest" in keys else None,
        promoted_manifest=_json_manifest(row["promoted_manifest"])
        if "promoted_manifest" in keys else None,
    )
