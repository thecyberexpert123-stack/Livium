"""Tests for the memory_recent capability.

These run against a real SQLite database through the real Memory API -- no mocks --
so a pass means the tool actually enumerates what the store holds.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import memory_recent  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from livium.memory import Memory  # noqa: E402
from livium.store import Store  # noqa: E402


class FakeContext:
    def __init__(self, memory):
        self.services = {"memory": memory}


class TestMemoryRecent(unittest.TestCase):
    def setUp(self):
        self.db = pathlib.Path("/tmp/livium-recent-test.db")
        self.db.unlink(missing_ok=True)
        self.store = Store(self.db)
        self.memory = Memory(self.store)
        self.ctx = FakeContext(self.memory)

    def tearDown(self):
        self.store.close()
        self.db.unlink(missing_ok=True)

    def call(self, **args):
        return memory_recent.handle("memory_recent", args, self.ctx)

    def test_empty_memory_is_reported_honestly(self):
        result = self.call()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)
        self.assertIn("No memory records", result.output)

    def test_returns_records_newest_first(self):
        self.memory.remember("experience", "the first thing learned", key="one")
        self.memory.remember("decision", "the second thing decided", key="two")
        result = self.call()
        self.assertTrue(result.ok)
        keys = [row["key"] for row in result.data["records"]]
        self.assertEqual(keys[:2], ["two", "one"])

    def test_kind_filter_is_applied(self):
        self.memory.remember("experience", "an experience record", key="exp")
        self.memory.remember("decision", "a decision record", key="dec")
        result = self.call(kinds=["decision"])
        self.assertEqual([row["kind"] for row in result.data["records"]], ["decision"])

    def test_limit_is_respected_and_clamped(self):
        for index in range(8):
            self.memory.remember("experience", f"record number {index}", key=f"r{index}")
        self.assertEqual(len(self.call(limit=3).data["records"]), 3)
        self.assertEqual(len(self.call(limit=0).data["records"]), 1)      # clamped up
        self.assertEqual(len(self.call(limit=999).data["records"]), 8)    # clamped down
        self.assertLessEqual(len(self.call(limit=999).data["records"]), memory_recent.MAX_LIMIT)

    def test_retired_records_are_not_listed_as_current_knowledge(self):
        memory_id, _ = self.memory.remember("failure", "an obsolete failure note", key="old")
        self.memory.forget(memory_id, reason="no longer relevant")
        result = self.call()
        self.assertNotIn(memory_id, [row["id"] for row in result.data["records"]])

    def test_content_change_moves_a_record_to_the_front(self):
        """The ordering must follow real updates, not insertion order."""

        self.memory.remember("experience", "older record", key="older")
        newer_id, _ = self.memory.remember("experience", "newer record", key="newer")
        self.memory.update(newer_id, content="newer record, revised after a real observation")
        result = self.call()
        self.assertEqual(result.data["records"][0]["id"], newer_id)

    def test_trust_information_travels_with_each_record(self):
        self.memory.remember("failure", "a hypothesis worth re-checking", key="h",
                             verification="unverified", confidence=0.3)
        row = self.call().data["records"][0]
        self.assertEqual(row["verification"], "unverified")
        self.assertAlmostEqual(row["confidence"], 0.3)
        self.assertIn("verification=unverified", self.call().output)

    def test_bad_arguments_are_reported(self):
        self.assertFalse(self.call(limit="many").ok)
        self.assertFalse(self.call(kinds="decision").ok)

    def test_missing_memory_service_is_an_error_not_an_empty_list(self):
        result = memory_recent.handle("memory_recent", {}, type("C", (), {"services": {}})())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_memory_service")

    def test_unknown_tool_name_is_refused(self):
        result = memory_recent.handle("something_else", {}, self.ctx)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
