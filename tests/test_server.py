"""The viewer and the API must agree about the shape of the data.

`livium/web/app.js` renders whatever `/api/*` returns; it has no fallback values. A
field that the front end reads but the server does not send used to render as
nothing at all, which looks like a quiet dashboard rather than a broken contract —
that is exactly how ``status.permissions`` went wrong once. This module starts the
real server on an ephemeral port and checks the real responses for every path the
front end depends on.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from support import REPO_ROOT, LiviumTestCase

from livium.server import ApiState, Handler

#: (endpoint, dotted path) pairs that `livium/web/app.js` reads. Keep this list next
#: to the front-end code it describes: it is the contract, and it fails loudly when
#: either side moves without the other.
FRONT_END_FIELDS: tuple[tuple[str, str], ...] = (
    ("/api/status", "status.config.model.provider"),
    ("/api/status", "status.config.model.model"),
    ("/api/status", "status.config.model.base_url"),
    ("/api/status", "status.config.model.api_key_present"),
    ("/api/status", "status.stats.memory"),
    ("/api/status", "status.stats.event"),
    ("/api/status", "status.guardian.integrity.ok"),
    ("/api/status", "status.guardian.integrity.detail"),
    ("/api/status", "status.guardian.freeze.frozen"),
    ("/api/status", "status.guardian.consequences"),
    ("/api/status", "startup.guardian_integrity.checked"),
    ("/api/status", "startup.context.available_for_prompt"),
    ("/api/status", "startup.context.reserve"),
    ("/api/status", "startup.context.estimate_calibration"),
    ("/api/status", "startup.capabilities_loaded.loaded_tools"),
    ("/api/status", "startup.capabilities_loaded.failures"),
    ("/api/status", "provider.model"),
    ("/api/status", "provider.ok"),
    ("/api/status", "provider.detail"),
    ("/api/status", "provider.context_window"),
    ("/api/status", "provider.context_window_source"),
    ("/api/status", "provider.is_test_double"),
    ("/api/status", "provider.age_seconds"),
    ("/api/status", "provider.fresh"),
    ("/api/status", "isolation.mechanism"),
    ("/api/status", "isolation.network_isolated"),
    ("/api/status", "isolation.filesystem_confined"),
    ("/api/status", "isolation.detail"),
    ("/api/permissions", "read_roots"),
    ("/api/permissions", "write_roots"),
    ("/api/permissions", "network_allowlist"),
    ("/api/permissions", "denied_binaries"),
    ("/api/permissions", "allowed_binaries"),
    ("/api/permissions", "git_refused"),
    ("/api/guardian", "integrity"),
    ("/api/guardian", "frozen"),
    ("/api/guardian", "protected_files"),
    ("/api/guardian", "consequences"),
    ("/api/capabilities", "capabilities"),
    ("/api/capabilities", "loaded_capability_tools"),
    ("/api/capabilities", "load_failures"),
    ("/api/context", "reports"),
    ("/api/events", "events"),
    ("/api/tasks", "tasks"),
    ("/api/ledger", "rows"),
    ("/api/tools", "tools"),
    ("/api/recovery", "findings"),
    ("/api/recovery", "next_action"),
    ("/api/recovery", "safe_to_resume"),
    # Deferred work and integrity: the two panels this mission adds.
    ("/api/discovery", "counts"),
    ("/api/discovery", "records"),
    ("/api/discovery", "review"),
    ("/api/discovery", "cycles"),
    ("/api/discovery", "stopped_cycles"),
    ("/api/discovery", "budget"),
    ("/api/discovery", "note"),
    ("/api/integrity", "components"),
    ("/api/integrity", "inspection"),
    ("/api/integrity", "events"),
    ("/api/integrity", "incidents"),
    ("/api/integrity", "enforcement"),
    ("/api/integrity", "snapshots"),
    ("/api/integrity", "frozen"),
    ("/api/integrity", "note"),
    ("/api/memory", "records"),
    ("/api/memory", "duplicates"),
    ("/api/memory", "stats"),
)


def _dig(payload, dotted: str):
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted)
        current = current[part]
    return current


class ViewerContractTests(LiviumTestCase):
    @classmethod
    def setUpClass(cls) -> None:  # pragma: no cover - exercised by every test below
        pass

    def setUp(self) -> None:
        super().setUp()
        self.config = self.config()
        self.config.ensure_dirs()
        self.state = ApiState(self.config)
        handler = type("TestHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.verbose = False  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.state.close()

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=15) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    # -- the contract ------------------------------------------------------
    def test_every_field_the_viewer_reads_is_present(self) -> None:
        cache: dict[str, dict] = {}
        problems: list[str] = []
        for endpoint, dotted in FRONT_END_FIELDS:
            if endpoint not in cache:
                cache[endpoint] = self.get(endpoint)
            try:
                value = _dig(cache[endpoint], dotted)
            except KeyError:
                problems.append(f"{endpoint} is missing {dotted}")
                continue
            if value is None:
                problems.append(f"{endpoint}.{dotted} is null")
        self.assertEqual(problems, [], "front end and API have drifted apart:\n  "
                                       + "\n  ".join(problems))

    def test_polling_the_status_route_does_not_probe_the_provider_every_time(self) -> None:
        """A page that refreshes must not become a request per refresh.

        Every probe is a real network call and a real `provider_probe` row. A viewer that
        polls on a timer would otherwise fill the journal with probes and measure its own
        timer rather than the provider -- and the badge would claim a freshness it did not
        have. The explicit `probe=1` is how a fresh answer is asked for.
        """

        store = self.state.agent.store
        before = len(store.query("SELECT id FROM event WHERE type='provider_probe'"))
        for _ in range(3):
            self.get("/api/status")
        after_polls = len(store.query("SELECT id FROM event WHERE type='provider_probe'"))
        self.assertEqual(after_polls, before,
                         "three polls of /api/status wrote provider_probe rows")

        payload = self.get("/api/status?probe=1")
        self.assertTrue(payload["provider"]["fresh"])
        self.assertEqual(payload["provider"]["age_seconds"], 0.0)
        after_fresh = len(store.query("SELECT id FROM event WHERE type='provider_probe'"))
        self.assertEqual(after_fresh, before + 1,
                         "an explicit probe did not record exactly one probe")

        # ...and a poll after a fresh probe reports how old the answer is, rather than
        # pretending it was just taken.
        cached = self.get("/api/status")
        self.assertFalse(cached["provider"]["fresh"])
        self.assertGreaterEqual(cached["provider"]["age_seconds"], 0.0)
        self.assertEqual(len(store.query("SELECT id FROM event WHERE type='provider_probe'")),
                         after_fresh)

    def test_the_declared_contract_covers_every_data_read_in_the_front_end(self) -> None:
        """Every payload path app.js reads must appear in the declared contract.

        This is the check that would have caught the original defect: `status.permissions`
        was read by the front end and never sent by the server, and no declared path
        covered it. A read is covered when it equals a declared path or is a prefix of
        one (containers such as `status.config` are covered by their declared children).
        """

        import re

        source = (REPO_ROOT / "livium" / "web" / "app.js").read_text(encoding="utf-8")
        # local variable in app.js -> prefix in the contract
        prefixes = {
            "status": "status",
            "startup": "startup",
            "provider": "provider",
            "isolation": "isolation",
            "perms": "",
            "guardian": "status.guardian",
            "data": "",
            "item": "",  # loop variable over capability records, not an API payload
        }
        declared = [dotted for _, dotted in FRONT_END_FIELDS]
        uncovered: list[str] = []
        for variable, prefix in prefixes.items():
            for field in sorted(set(re.findall(rf"\b{variable}\.([a-z_][a-z_0-9]*)", source))):
                path = f"{prefix}.{field}" if prefix else field
                if not any(d == path or d.startswith(path + ".") for d in declared):
                    uncovered.append(f"{variable}.{field}")
        self.assertEqual(
            uncovered, [],
            "app.js reads payload paths that the declared contract does not cover: "
            f"{sorted(set(uncovered))} — add them to FRONT_END_FIELDS and make sure the "
            "server actually sends them",
        )

    # -- the page itself ---------------------------------------------------
    def test_the_page_and_its_assets_are_served(self) -> None:
        for path, needle in (("/", "<html"), ("/static/app.js", "driftNote"),
                             ("/static/styles.css", "{")):
            with urllib.request.urlopen(self.base + path, timeout=15) as response:
                self.assertEqual(response.status, 200)
                body = response.read().decode("utf-8")
            self.assertIn(needle, body, f"{path} did not look like the expected asset")

    def test_an_unknown_route_is_404_not_a_canned_page(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/nope", timeout=15)
        self.assertEqual(caught.exception.code, 404)

    def test_capability_review_reads_live_lab_hashes_but_cannot_promote(self) -> None:
        registry = self.state.agent.capabilities
        registry.propose(name="line_count", requirement="count lines",
                         reuse_analysis="existing tools cannot count non-blank lines")
        registry.scaffold("line_count")
        review = self.get("/api/capabilities/line_count/review")
        self.assertEqual(review["capability"]["status"], "experimental")
        self.assertIn("line_count.py", review["lab"]["observed"])
        self.assertIsNone(review["lab"]["expected"])
        self.assertFalse(review["eligible_for_human_review"])
        self.assertFalse(review["guardian_gate"]["ok"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/capabilities/not_registered/review")
        self.assertEqual(caught.exception.code, 404)

    def test_the_api_does_not_expose_promotion_or_rebaselining(self) -> None:
        """Those are deliberate CLI-only, human-attributed controls."""

        source = (REPO_ROOT / "livium" / "server.py").read_text(encoding="utf-8")
        for forbidden in ("/api/promote", "/api/guardian/baseline", "/api/rollback"):
            self.assertNotIn(forbidden, source)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/api/promote", timeout=15)

    # -- deferred work and integrity, from real state ----------------------
    def test_deferred_work_endpoint_reads_the_queue_not_a_stub(self) -> None:
        from livium.discovery import DiscoveryQueue

        payload = self.get("/api/discovery")
        self.assertEqual(payload["records"], [], "an empty queue is reported as empty")
        queue = DiscoveryQueue(self.state.agent.store, max_depth=2)
        record, action = queue.capture(title="the viewer should see this",
                                      observation="a real record, written by the real queue",
                                      confidence=0.7)
        self.assertEqual(action, "created")
        payload = self.get("/api/discovery")
        self.assertEqual(payload["counts"].get("pending", 0)
                         + payload["counts"].get("ready", 0), 1)
        self.assertEqual([r["id"] for r in payload["records"]], [record.id])
        self.assertEqual(payload["records"][0]["provenance"], "agent_observation")
        # The ranking ships with its reasons: a score without its terms is a number
        # nobody can argue with.
        self.assertEqual([entry["record"]["id"] for entry in payload["review"]], [record.id])
        self.assertTrue(payload["review"][0]["reasons"])
        self.assertTrue(payload["note"])
        detail = self.get(f"/api/discovery/{record.id}")
        self.assertEqual(detail["record"]["title"], "the viewer should see this")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/discovery/999999", timeout=15)
        self.assertEqual(caught.exception.code, 404)

    def test_integrity_endpoint_reports_the_watched_set_and_the_record(self) -> None:
        payload = self.get("/api/integrity")
        self.assertEqual(len(payload["components"]), 12,
                         "the twelve components the policy names")
        tiers = {component["tier"] for component in payload["components"]}
        self.assertEqual(tiers, {"governance", "enforcement", "instructions"},
                         "the policy's own entries; 'operational' is the tier a file gets "
                         "when configuration protects it and the policy does not name it")
        self.assertIn(payload["inspection"]["status"], ("ok", "changed", "unbaselined",
                                                        "missing", "tampered"))
        self.assertEqual(payload["incidents"], [])
        self.assertEqual(payload["enforcement"]["unresolved_detections"], [])
        # Reading the panel is not a security event. On a home with no baseline the
        # inspection reports `unbaselined`, and a route that recorded that on every poll
        # would fill the append-only table with the viewer's own refresh timer.
        first = len(payload["events"])
        self.assertEqual(len(self.get("/api/integrity")["events"]), first)
        self.assertFalse(payload["frozen"]["frozen"])
        self.assertTrue(payload["note"])
        # A run of the real service writes a record, and the route reports it.
        from livium.integrity import Integrity

        integrity = Integrity(self.state.agent.config, self.state.agent.store,
                              self.state.agent.engine)
        integrity.inspect(detection="test")
        payload = self.get("/api/integrity")
        self.assertTrue(payload["events"], "the detection is in the record the API returns")

    def test_a_file_protected_only_by_configuration_is_watched_and_says_so(self) -> None:
        victim = self.home.dir / "deployment-rules.py"
        victim.write_text("RULE = 1\n", encoding="utf-8")
        # self.home.config, not self.config: setUp assigns the *result* of config() to
        # self.config, so the method is shadowed by the object it returned.
        config = self.home.config(LIVIUM_EXTRA_PROTECTED_PATHS=str(victim))
        from livium.integrity import Integrity
        from livium.store import Store

        store = Store(config.paths.db)
        self.addCleanup(store.close)
        integrity = Integrity(config, store)
        entry = [c for c in integrity.components() if c.path == victim.resolve()]
        self.assertTrue(entry, "a configured path is watched even with no policy entry")
        self.assertEqual(entry[0].tier, "operational")
        self.assertIn("configur", entry[0].why.lower(),
                      "and the record says why it is protected: configuration, not policy")

    def test_the_new_routes_are_read_only(self) -> None:
        """Transitions, approvals and recovery are CLI-only, human-attributed controls."""

        source = (REPO_ROOT / "livium" / "server.py").read_text(encoding="utf-8")
        post_body = source[source.index("def do_POST"):]
        for forbidden in ("/api/discovery", "/api/integrity"):
            self.assertNotIn(forbidden, post_body,
                             f"{forbidden} must not be reachable by POST")

    def test_recovery_endpoint_reports_an_honest_empty_state(self) -> None:
        payload = self.get("/api/recovery")
        self.assertIsNone(payload["task"])
        self.assertEqual(payload["findings"], [])
        self.assertFalse(payload["safe_to_resume"])
        self.assertIn("No unfinished task", payload["next_action"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
