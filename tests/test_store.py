"""Durability tests: the store must survive real process termination."""

from __future__ import annotations

import json
import pathlib
import signal
import sqlite3
import subprocess
import sys
import textwrap
import unittest

from support import REPO_ROOT, LiviumTestCase

from livium.store import Store


class StoreDurabilityTests(LiviumTestCase):
    def test_records_survive_reopen(self) -> None:
        config = self.config()
        store = Store(config.paths.db)
        store.append_event("test_event", {"value": 1}, task_id="t1")
        store.set_meta("probe", {"hello": "world"})
        store.close()

        reopened = Store(config.paths.db)
        self.addCleanup(reopened.close)
        events = reopened.events(event_type="test_event")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["payload"])["value"], 1)
        self.assertEqual(reopened.get_meta("probe"), {"hello": "world"})

    def test_event_journal_is_append_only_at_the_database_level(self) -> None:
        store = self.store()
        event_id = store.append_event("immutable", {"a": 1})
        with self.assertRaises(sqlite3.IntegrityError):
            store.execute("UPDATE event SET type='changed' WHERE id=?", (event_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            store.execute("DELETE FROM event WHERE id=?", (event_id,))
        self.assertEqual(len(store.events()), 1)

    def test_transaction_rolls_back_completely_on_error(self) -> None:
        store = self.store()
        with self.assertRaises(RuntimeError):
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO task(id, objective, acceptance, status, created_at, updated_at) "
                    "VALUES('x','obj','[]','active','now','now')"
                )
                raise RuntimeError("simulated failure mid-transaction")
        self.assertIsNone(store.one("SELECT * FROM task WHERE id='x'"))

    def test_intent_write_ahead_log_round_trip(self) -> None:
        store = self.store()
        intent_id = store.record_intent(
            "write_file", {"path": "/tmp/x", "bytes": 10}, task_id="t9", target="/tmp/x"
        )
        pending = store.pending_intents("t9")
        self.assertEqual([row["id"] for row in pending], [intent_id])
        store.resolve_intent(intent_id, "done", {"sha256": "abc"})
        self.assertEqual(store.pending_intents("t9"), [])
        row = store.one("SELECT * FROM intent WHERE id=?", (intent_id,))
        self.assertEqual(row["status"], "done")
        self.assertEqual(json.loads(row["result"])["sha256"], "abc")

    def test_survives_an_actual_process_kill(self) -> None:
        """SIGKILL the writer mid-flight; committed rows must still be there.

        This is the durability claim the recovery demonstrations depend on, so it
        is tested against a real process rather than trusted because WAL is set.
        """

        config = self.config()
        script = textwrap.dedent(
            f"""
            import os, signal, sys, time
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from livium.store import Store
            store = Store({str(config.paths.db)!r})
            for i in range(25):
                store.append_event("kill_test", {{"i": i}})
                store.execute(
                    "INSERT INTO task(id, objective, acceptance, status, created_at, updated_at) "
                    "VALUES(?,?,?,'active','now','now')",
                    (f"row{{i}}", "objective", "[]"),
                )
            print("READY", flush=True)
            time.sleep(30)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        line = process.stdout.readline().decode().strip()
        self.assertEqual(line, "READY")
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()

        store = Store(config.paths.db)
        self.addCleanup(store.close)
        self.assertEqual(store.stats()["event"], 25)
        self.assertEqual(store.stats()["task"], 25)


class SchemaTests(LiviumTestCase):
    def test_schema_creation_is_idempotent(self) -> None:
        config = self.config()
        first = Store(config.paths.db)
        first.close()
        second = Store(config.paths.db)
        self.addCleanup(second.close)
        # Track the constant: the point of the test is that the version is stored and
        # read back as an int, not that it never changes.
        from livium.store import SCHEMA_VERSION

        self.assertEqual(second.get_meta("schema_version"), int(SCHEMA_VERSION))

    def test_stats_reports_real_counts(self) -> None:
        store = self.store()
        store.append_event("a")
        store.append_event("b")
        counts = store.stats()
        self.assertEqual(counts["event"], 2)
        for table in ("memory", "task", "intent", "capability", "verification"):
            self.assertIn(table, counts)
            self.assertEqual(counts[table], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
