"""Promoted capabilities (loaded at runtime).

This source package includes ``memory_recent.py`` as an historical example of a
human-promoted capability. It is NOT automatically active in a new runtime home.
Approved capability packages are installed in ``$LIVIUM_HOME/promoted/NAME/``
after real lab tests and a human CLI promotion; the loader accepts only packages
whose on-disk bytes match an Active/Maintained database manifest. A file in the
source tree or dropped by hand into the promoted directory grants no tool.

Contract (documented in each promoted module and enforced by
``livium.capabilities.validate_capability_module``):

``CAPABILITY``
    dict with ``name``, ``requirement``, ``description`` and ``tools`` -- each
    tool being ``{"name", "description", "parameters"}``.
``handle(tool_name, args, ctx)``
    returns a ``livium.tools.ToolResult``.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any, Iterator


class _LoadReport(list):
    """List of loaded (spec, handler, capability) triples that also carries the
    failures. Keeps the public function a plain iterator while making the reasons
    for exclusion impossible to lose."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: list[tuple[pathlib.Path, str]] = []


def load_promoted(directory: str | pathlib.Path, *,
                  approved: dict[str, dict[str, str]] | None = None) -> Iterator[Any]:
    """Load only approved, unchanged packages, BEFORE running their import code.

    ``approved`` maps capability name to the manifest recorded at the human
    promotion. The default is deny-all: simply dropping a Python file in this
    directory cannot make it a tool. The caller journals ``last_failures`` so a
    corrupt or unapproved installation is visible, not silently skipped.
    """

    from ..capabilities import CapabilityError, file_manifest
    import hashlib
    import sys

    report = _LoadReport()
    load_promoted.last_failures = report.failures
    root = pathlib.Path(directory)
    if not root.exists():
        return iter(report)
    approved = approved or {}
    seen: set[str] = set()
    candidates = sorted(p for p in root.iterdir() if not p.name.startswith(("_", "."))
                        and (p.is_dir() or p.suffix == ".py" or p.is_symlink()))
    for candidate in candidates:
        name = candidate.stem if candidate.suffix == ".py" else candidate.name
        if name in seen:
            report.failures.append((candidate, "duplicate promoted entry for the same capability"))
            continue
        seen.add(name)
        if name not in approved:
            report.failures.append((candidate, "no active, human-promoted manifest in the registry"))
            continue
        if candidate.is_symlink():
            report.failures.append((candidate, "promoted entry is a symlink"))
            continue
        try:
            if candidate.is_dir():
                actual = file_manifest(candidate)
                module_path = candidate / f"{name}.py"
            else:  # legacy single-file installation (only if explicitly approved)
                with candidate.open("rb") as handle:
                    actual = {candidate.name: hashlib.file_digest(handle, "sha256").hexdigest()}
                module_path = candidate
        except (CapabilityError, OSError) as exc:
            report.failures.append((candidate, str(exc)))
            continue
        if actual != approved[name]:
            report.failures.append((candidate, "promoted files differ from the approved manifest"))
            continue
        if not module_path.is_file() or module_path.is_symlink():
            report.failures.append((candidate, f"missing or symlinked entrypoint: {module_path}"))
            continue
        if candidate.is_dir() and any(candidate.rglob("*.pyc")):
            report.failures.append((candidate, "bytecode cache present; refusing unsigned executable code"))
            continue
        module_name = f"livium_promoted_{name}"
        original_bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, module_path,
                submodule_search_locations=[str(candidate)] if candidate.is_dir() else None,
            )
            if spec is None or spec.loader is None:
                report.failures.append((module_path, "no import spec available"))
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module  # package-relative helper imports work
            spec.loader.exec_module(module)
        except BaseException as exc:
            for loaded in list(sys.modules):
                if loaded == module_name or loaded.startswith(module_name + "."):
                    sys.modules.pop(loaded, None)
            report.failures.append((module_path, f"{type(exc).__name__}: {exc}"))
            continue
        finally:
            sys.dont_write_bytecode = original_bytecode_setting

        try:
            after_import = file_manifest(candidate) if candidate.is_dir() else {
                candidate.name: hashlib.sha256(candidate.read_bytes()).hexdigest()}
        except (CapabilityError, OSError) as exc:
            report.failures.append((candidate, f"changed during import: {exc}"))
            continue
        if after_import != actual:
            report.failures.append((candidate, "promoted files changed during import; refusing tools"))
            continue
        declaration = getattr(module, "CAPABILITY", None)
        handler = getattr(module, "handle", None)
        if not isinstance(declaration, dict) or not callable(handler):
            report.failures.append((module_path, "missing CAPABILITY dict or callable handle()"))
            continue
        if declaration.get("name") != name:
            report.failures.append((module_path, "declaration name differs from approved entry"))
            continue
        tools = declaration.get("tools") or []
        if not isinstance(tools, list) or not tools:
            report.failures.append((module_path, "CAPABILITY['tools'] must be a nonempty list"))
            continue
        invalid = [t for t in tools if not isinstance(t, dict)
                   or not t.get("name") or not t.get("description")
                   or not isinstance(t.get("parameters"), dict)
                   or t["parameters"].get("type") != "object"]
        if invalid or len({t["name"] for t in tools}) != len(tools):
            report.failures.append((module_path, "malformed or duplicate tool declarations"))
            continue
        for tool in tools:
            report.append((tool, handler, name))

    for missing in approved.keys() - seen:
        report.failures.append((root / missing, "approved capability has no installed files"))
    return iter(report)
