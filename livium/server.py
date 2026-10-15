"""HTTP API and viewer.

Deliberately read-mostly. The API exposes state, memory, the journal, context
reports, capability status and a way to start a task. It deliberately does *not*
expose the guardian baseline or capability promotion: those are trusted,
human-attributed operations reachable only from the CLI, and putting them behind
an HTTP endpoint would quietly undo the control that makes them meaningful.

Everything the API returns is read from the database or from a real probe. There
are no canned responses; if the provider is unreachable, `/api/status` says so
with the reason.
"""

from __future__ import annotations

import json
import pathlib
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .agent import LiviumAgent
from .capabilities import CapabilityRegistry
from .config import Config

WEB_ROOT = pathlib.Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


class ApiState:
    """One agent, one lock. Task runs are serialised so two runs cannot interleave
    their intents and confuse recovery."""

    def __init__(self, config: Config):
        self.config = config
        self.agent = LiviumAgent(config)
        self.lock = threading.Lock()
        self.startup = self.agent.start()

    def close(self) -> None:
        self.agent.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "Livium/1.0"
    state: ApiState  # injected on the class by serve()

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The viewer is embedded in a preview iframe under a different origin.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, default=str, indent=2).encode("utf-8"),
                   CONTENT_TYPES[".json"])

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        try:
            origin_host = urllib.parse.urlsplit(origin).netloc
        except ValueError:
            return False
        # Same-origin, or proxied under the same host as the request itself.
        return origin_host == host or host in origin

    # -- routing ----------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        agent = self.state.agent

        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route.startswith("/static/"):
            return self._static(route[len("/static/"):])

        if route == "/api/status":
            return self._json(200, {
                "startup": self.state.startup,
                "status": agent.status(),
                "isolation": agent.runner.isolation.as_dict(),
                "provider": agent.probe_provider(),
            })
        if route == "/api/guardian":
            guardian = agent.guardian
            return self._json(200, {
                "integrity": guardian.integrity().as_dict(),
                "frozen": guardian.freeze_status(),
                "protected_files": [str(p) for p in guardian.protected_files()],
                "consequences": [c.as_dict() for c in guardian.consequences(None)],
                "baseline_recorded": len(guardian.load_baseline()),
            })
        if route == "/api/memory":
            query_text = (query.get("q") or [""])[0]
            limit = int((query.get("limit") or ["20"])[0])
            kinds = query.get("kinds") or None
            memory = agent.memory
            records = (memory.search(query_text, kinds=kinds, limit=limit) if query_text
                       else memory.recent(kinds=kinds, limit=limit))
            return self._json(200, {
                "query": query_text,
                "records": [r.as_dict() for r in records],
                "duplicates": memory.find_duplicates()[:5],
                "stats": {"active": len(memory.recent(limit=1000))},
            })
        if route == "/api/events":
            rows = agent.store.events(
                task_id=(query.get("task_id") or [None])[0],
                event_type=(query.get("type") or [None])[0],
                limit=int((query.get("limit") or ["60"])[0]),
            )
            return self._json(200, {
                "events": [
                    {"id": r["id"], "ts": r["ts"], "task_id": r["task_id"], "type": r["type"],
                     "payload": json.loads(r["payload"] or "{}")}
                    for r in rows
                ]
            })
        if route == "/api/tasks":
            rows = agent.store.query(
                "SELECT * FROM task ORDER BY updated_at DESC LIMIT ?",
                (int((query.get("limit") or ["25"])[0]),),
            )
            return self._json(200, {"tasks": [dict(r) for r in rows]})
        if route.startswith("/api/tasks/"):
            task_id = route.rsplit("/", 1)[-1]
            row = agent.store.one("SELECT * FROM task WHERE id=?", (task_id,))
            if row is None:
                return self._json(404, {"error": f"no task {task_id}"})
            steps = agent.store.query(
                "SELECT * FROM task_step WHERE task_id=? ORDER BY idx", (task_id,)
            )
            checks = agent.store.query(
                "SELECT * FROM verification WHERE task_id=? ORDER BY id", (task_id,)
            )
            reports = agent.store.query(
                "SELECT * FROM context_report WHERE task_id=? ORDER BY step", (task_id,)
            )
            return self._json(200, {
                "task": dict(row),
                "steps": [dict(s) for s in steps],
                "verification": [dict(c) for c in checks],
                "context_reports": [dict(r) for r in reports],
            })
        if route == "/api/capabilities":
            registry = CapabilityRegistry(
                agent.store, lab=agent.config.paths.capability_lab
            )
            return self._json(200, {
                "capabilities": [c.as_dict() for c in registry.all()],
                "loaded_capability_tools": sorted(agent.loaded_capabilities),
                "load_failures": agent.capability_load_failures,
            })
        if route == "/api/context":
            rows = agent.store.query(
                "SELECT * FROM context_report ORDER BY id DESC LIMIT ?",
                (int((query.get("limit") or ["20"])[0]),),
            )
            return self._json(200, {"reports": [dict(r) for r in rows]})
        if route == "/api/permissions":
            return self._json(200, agent.engine.describe())
        if route == "/api/recovery":
            report = agent.recovery.inspect((query.get("task_id") or [None])[0])
            return self._json(200, report.as_dict())
        if route == "/api/tools":
            return self._json(200, {"tools": agent.tools.specs()})
        return self._json(404, {"error": f"no route {route}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_ok():
            return self._json(403, {"error": "cross-origin POST refused"})
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return self._json(400, {"error": f"invalid JSON body: {exc}"})
        agent = self.state.agent

        if route == "/api/memory":
            try:
                memory_id, action = agent.memory.remember(
                    str(body.get("kind", "experience")),
                    str(body.get("content", "")),
                    key=body.get("key") or None,
                    tags=body.get("tags") or (),
                    confidence=float(body.get("confidence", 0.7)),
                    verification=str(body.get("verification", "claimed")),
                    evidence=body.get("evidence") or "recorded by an operator through the viewer",
                    source="operator:viewer",
                )
            except (ValueError, PermissionError) as exc:
                return self._json(400, {"error": str(exc)})
            record = agent.memory.get(memory_id)
            return self._json(200, {"action": action, "record": record.as_dict() if record else None})

        if route == "/api/run":
            objective = str(body.get("objective", "")).strip()
            if not objective:
                return self._json(400, {"error": "objective is required"})
            if not self.state.lock.acquire(blocking=False):
                return self._json(409, {"error": "a task is already running"})
            try:
                probe = agent.probe_provider()
                if not probe.get("ok") and not body.get("force"):
                    return self._json(200, {
                        "status": "blocked",
                        "error": (
                            "the model provider is not usable, so no task was started. "
                            + str(probe.get("detail"))
                        ),
                        "provider": probe,
                    })
                run = agent.run_task(objective, body.get("criteria") or [])
                payload = run.as_dict()
                payload["provider"] = probe
                return self._json(200, payload)
            finally:
                self.state.lock.release()

        if route == "/api/capabilities/test":
            name = str(body.get("name", ""))
            registry = CapabilityRegistry(agent.store, lab=agent.config.paths.capability_lab)
            try:
                result = registry.run_tests(name)
            except Exception as exc:
                return self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, result)

        if route == "/api/memory/consolidate":
            return self._json(200, agent.memory.consolidate_duplicates())

        return self._json(404, {"error": f"no route {route} (promotion and re-baselining are "
                                         f"CLI-only operations by design)"})

    def _static(self, relative: str) -> None:
        target = (WEB_ROOT / relative).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.is_file():
            return self._json(404, {"error": f"no such asset {relative}"})
        body = target.read_bytes()
        self._send(200, body, CONTENT_TYPES.get(target.suffix, "application/octet-stream"))


def serve(config: Config, *, host: str = "0.0.0.0", port: int = 8787, verbose: bool = False) -> int:
    state = ApiState(config)
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    probe = state.startup["provider_probe"]
    print(f"Livium viewer on http://{host}:{port}")
    print(f"  provider: {config.model_label()} "
          f"({'verified' if probe.get('ok') else 'NOT VERIFIED: ' + str(probe.get('detail'))[:160]})")
    print(f"  database: {config.paths.db}")
    print(f"  isolation: {state.agent.runner.isolation.mechanism} "
          f"(network_isolated={state.agent.runner.isolation.network_isolated})")
    print("  promotion and re-baselining are CLI-only and are not exposed here")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        state.close()
    return 0
