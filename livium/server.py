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

import dataclasses
import json
import pathlib
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .agent import LiviumAgent
from .capabilities import CapabilityError
from .config import Config
from .discovery import DiscoveryQueue
from .integrity import Integrity
from .ledger import Ledger

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

    #: How long a provider probe stays usable for the viewer. A page that polls is not a
    #: reason to make a network request, so the answer is stamped and reused; asking for
    #: a fresh one is an explicit act, not a side effect of a timer.
    PROBE_TTL_SECONDS = 300.0

    def __init__(self, config: Config):
        self.config = config
        self.agent = LiviumAgent(config)
        self.lock = threading.Lock()
        self.startup = self.agent.start()
        self._probe: dict[str, Any] | None = None
        self._probe_at = 0.0
        # The startup probe already answered the question once; reuse it rather than
        # asking a second time the moment the first page is opened.
        started = self.startup.get("provider_probe")
        if isinstance(started, dict):
            self._probe = started
            self._probe_at = time.time()

    def close(self) -> None:
        self.agent.close()

    def provider_snapshot(self, fresh: bool = False) -> dict[str, Any]:
        """The provider's state, probed when it is stale and reused when it is not.

        Every probe is a real request to the provider and a real row in the journal, so a
        viewer that polls must not produce one per poll: that would measure the view's
        timer instead of the provider. The age travels with the answer, and `probe=1`
        asks for a fresh one when the operator wants to know *now*.
        """

        age = time.time() - self._probe_at if self._probe else None
        if fresh or self._probe is None or age is None or age > self.PROBE_TTL_SECONDS:
            self._probe = self.agent.probe_provider()
            self._probe_at = time.time()
            age = 0.0
            fresh_probe = True
        else:
            fresh_probe = False
        payload = dict(self._probe)
        payload["age_seconds"] = round(age, 1)
        payload["fresh"] = fresh_probe
        return payload


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
                # Cached by default, because this route is polled: see provider_snapshot.
                "provider": self.state.provider_snapshot(
                    fresh=(query.get("probe") or ["0"])[0] == "1"),
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
        if route == "/api/ledger":
            ledger = Ledger(agent.store)
            limit = int((query.get("limit") or ["20"])[0])
            return self._json(200, {
                "rows": [dataclasses.asdict(row) for row in ledger.rows(limit=limit)],
                "summary": ledger.summary(),
                "note": ("prompt_tokens_actual and completion_tokens_actual are NULL when the "
                         "provider did not report usage; tokens_estimated is a separate "
                         "measurement and is never folded into them"),
            })
        if route == "/api/capabilities":
            return self._json(200, {
                "capabilities": [c.as_dict() for c in agent.capabilities.all()],
                "loaded_capability_tools": sorted(agent.loaded_capabilities),
                "load_failures": agent.capability_load_failures,
            })
        if route.startswith("/api/capabilities/") and route.endswith("/review"):
            name = route[len("/api/capabilities/"):-len("/review")].strip("/")
            try:
                review = agent.capabilities.review(name)
            except CapabilityError as exc:
                return self._json(404, {"error": str(exc)})
            review["guardian_gate"] = agent.guardian.promotion_gate(name)
            return self._json(200, review)
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
        if route == "/api/discovery":
            queue = DiscoveryQueue(agent.store,
                                   max_depth=agent.config.limits.followup_max_depth)
            limit = int((query.get("limit") or ["25"])[0])
            cycles = [
                {"task_id": row["task_id"], "ts": row["ts"],
                 "payload": json.loads(row["payload"] or "{}")}
                for row in agent.store.events(event_type="discovery_cycle_complete", limit=20)
            ]
            stopped = [
                {"task_id": row["task_id"], "ts": row["ts"],
                 "payload": json.loads(row["payload"] or "{}")}
                for row in agent.store.events(event_type="discovery_cycle_stopped", limit=20)
            ]
            return self._json(200, {
                "counts": queue.counts(),
                # Every record, newest first, and separately the open ones with the score
                # and the reasons behind their rank -- the ranking is a judgement, so the
                # API shows its terms rather than only its result.
                "records": [record.as_dict() for record in queue.all(limit=limit)],
                "review": [{"record": record.as_dict(), "score": score,
                            "reasons": reasons}
                           for record, score, reasons in queue.review(limit=limit)],
                "cycles": cycles,
                "stopped_cycles": stopped,
                "budget": agent.follow_up_budget().report(),
                "note": ("a discovery is an observation, not an instruction: nothing here "
                         "executes what a record says. Follow-up investigations run as their "
                         "own tasks on a budget charged before each call"),
            })
        if route.startswith("/api/discovery/"):
            raw = route[len("/api/discovery/"):]
            try:
                record_id = int(raw)
            except ValueError:
                return self._json(404, {"error": f"no discovery {raw}"})
            queue = DiscoveryQueue(agent.store,
                                   max_depth=agent.config.limits.followup_max_depth)
            record = queue.get(record_id)
            if record is None:
                return self._json(404, {"error": f"no discovery {record_id}"})
            return self._json(200, {"record": record.as_dict()})
        if route == "/api/integrity":
            guardian = agent.guardian
            # The service, not the summary: guardian.integrity() is a digest comparison
            # for one moment, and this route reports the record, the policy and the
            # enforcement history as well.
            integrity = Integrity(agent.config, agent.store, agent.engine)
            try:
                # record=False on purpose: the viewer polls every few seconds, and an
                # append-only security record filled with polling noise is a record
                # nobody reads. Detections are recorded where the system acts on them.
                inspection = integrity.inspect(detection="api", record=False)
            except Exception as exc:  # an unreadable home is reported, not hidden
                inspection = {"ok": False, "status": "unavailable",
                              "error": f"{type(exc).__name__}: {exc}"}
            snapshot_row = agent.store.one(
                "SELECT COUNT(*) AS n FROM integrity_snapshot") if agent.store else None
            return self._json(200, {
                "components": [
                    {"path": c.path.name, "tier": c.tier, "group": getattr(c, "group", ""),
                     "why": getattr(c, "why", "")}
                    for c in integrity.components()
                ],
                "inspection": inspection,
                "events": integrity.events(limit=int((query.get("limit") or ["25"])[0])),
                "incidents": integrity.incidents(limit=10),
                "enforcement": integrity.enforcement_summary(),
                "snapshots": int((snapshot_row or {"n": 0})["n"]),
                "frozen": guardian.freeze_status(),
                "note": ("a detection is not a prevention: the layer notices, attributes, "
                         "records and freezes. Recovery is offered as restore-and-verify, and "
                         "only a named human can authorise it"),
            })
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
            try:
                result = agent.capabilities.run_tests(name)
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
