"""Promoted capabilities (loaded at runtime).

Files here are copied in by ``livium capability promote``, after the capability
passed its tests in the capability lab and a human approved the promotion. This
package is otherwise empty: there is no speculative placeholder module waiting
for a future feature, and the loader below has no entries to load until a real
capability is promoted.

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


def load_promoted(directory: str | pathlib.Path) -> Iterator[Any]:
    """Import every ``*.py`` in ``directory`` and yield its tools.

    Failures are collected in ``last_failures`` (and recorded as journal events by
    the caller) rather than being skipped quietly: a capability that cannot load is
    a real defect and the agent must not be told it is available.
    """

    report = _LoadReport()
    load_promoted.last_failures = report.failures

    path = pathlib.Path(directory)
    if not path.exists():
        return iter(())

    for module_path in sorted(path.glob("*.py")):
        if module_path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"livium_promoted_{module_path.stem}", module_path
            )
            if spec is None or spec.loader is None:
                report.failures.append((module_path, "no import spec available"))
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            report.failures.append((module_path, f"{type(exc).__name__}: {exc}"))
            continue

        declaration = getattr(module, "CAPABILITY", None)
        handler = getattr(module, "handle", None)
        if not isinstance(declaration, dict) or not callable(handler):
            report.failures.append(
                (module_path, "missing CAPABILITY dict or callable handle()")
            )
            continue
        name = str(declaration.get("name", module_path.stem))
        tools = declaration.get("tools") or []
        if not isinstance(tools, list):
            report.failures.append((module_path, "CAPABILITY['tools'] must be a list"))
            continue
        for tool in tools:
            if not isinstance(tool, dict) or "name" not in tool:
                report.failures.append((module_path, f"malformed tool entry: {tool!r}"))
                continue
            report.append((tool, handler, name))

    return iter(report)
