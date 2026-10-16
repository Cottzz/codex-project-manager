import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import ManagedRuntime, RuntimeError, rebuild_runtime_projection
from service import Store
from test_runtime import FakeConnection


class RuntimeJournalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.init("journal", "synthetic only")
        self.store.call("task_create", {"id": "T", "title": "test", "body": "synthetic only",
                                         "request_id": "task"})
        self.runtime = ManagedRuntime()
        self.runtime.connection = FakeConnection()

    def action(self, action, rid, **args):
        return self.runtime.action(self.store, {"action": action, "request_id": rid,
            "task_id": "T", **args})

    def test_rotation_preserves_old_request_dedupe_and_terminal_state(self):
        with patch("runtime.JOURNAL_SEGMENT_BYTES", 800):
            created = self.action("create", "old-create")
            self.action("continue", "old-turn", thread_id="managed-thread", text="test")
            self.runtime._on_message({"method": "turn/completed", "params": {
                "threadId": "managed-thread", "turn": {"id": "turn-1", "status": "completed"}}})
            for n in range(140):
                self.action("reconcile", f"read-{n}", thread_id="managed-thread")
        archives = list((self.store.folder / "runtime-archive").glob("*.jsonl"))
        self.assertGreater(len(archives), 0)
        self.assertLessEqual(len(self.runtime._project(self.store)[1]["operations"]), 128)
        restored = ManagedRuntime()
        restored.connection = FakeConnection()
        self.assertEqual(restored.action(self.store, {
            "action": "create", "request_id": "old-create", "task_id": "T"}), created)
        self.assertEqual(restored.connection.calls, [])
        self.assertEqual(restored.status(self.store)["threads"][0]["thread_id"], "managed-thread")
        self.assertEqual(self.store.call("graph_get", {"id": "T"})["node"]["status"], "todo")

    def test_missing_projection_fails_closed_and_offline_replay_preserves_new_events(self):
        with patch("runtime.JOURNAL_SEGMENT_BYTES", 700):
            created = self.action("create", "old")
            self.action("continue", "new", thread_id="managed-thread", text="test")
            self.runtime._on_message({"method": "turn/completed", "params": {
                "threadId": "managed-thread", "turn": {"id": "turn-1", "status": "interrupted"}}})
        paths = sorted((self.store.folder / "runtime-archive").glob("*.jsonl"))
        paths.append(self.store.folder / "runtime-events.jsonl")
        original = {str(p): p.read_bytes() for p in paths}
        task_log = self.store.log.read_bytes()
        self.runtime._project(self.store)[1]["projection"].close()
        (self.store.folder / "runtime-projection.sqlite").unlink()
        with self.assertRaises(RuntimeError):
            ManagedRuntime().status(self.store)
        rebuild_runtime_projection(self.store.folder)
        restored = ManagedRuntime()
        restored.connection = FakeConnection()
        self.assertEqual(restored.status(self.store)["threads"][0]["status"], "interrupted")
        self.assertEqual(restored.action(self.store, {
            "action": "create", "request_id": "old", "task_id": "T"}), created)
        self.assertEqual(restored.connection.calls, [])
        self.assertEqual({str(p): p.read_bytes() for p in paths}, original)
        self.assertEqual(self.store.log.read_bytes(), task_log)

    def test_partial_tail_never_deleted_or_accepted(self):
        path = self.store.folder / "runtime-events.jsonl"
        path.write_bytes(b'{"type":"operation"')
        with self.assertRaises(RuntimeError):
            ManagedRuntime().status(self.store)
        self.assertEqual(path.read_bytes(), b'{"type":"operation"')

    def test_capacity_and_oversized_event_fail_before_dispatch_or_append(self):
        self.runtime.status(self.store)
        with patch("runtime.RUNTIME_THREAD_LIMIT", 0):
            with self.assertRaises(RuntimeError):
                self.action("create", "capacity")
        self.assertEqual(self.runtime.connection.calls, [])
        state = self.runtime._project(self.store)[1]
        with patch("runtime.JOURNAL_EVENT_BYTES", 32):
            with self.assertRaises(RuntimeError):
                self.runtime._append(state, {"type": "operation", "request_id": "large",
                                             "body": "x" * 100})
        self.assertFalse(state["path"].exists())


if __name__ == "__main__":
    unittest.main()
