import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

from pgw import BoundedHTTPServer
from service import Store, Conflict
from unittest.mock import patch


class MemoryGuardTest(unittest.TestCase):
    def test_state_cap_rejects_before_json_loading_and_preserves_latest_events(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); store.init("test", "synthetic")
            store.call("idea_capture", {"id": "later", "title": "keep", "request_id": "later"})
            original = store.log.read_bytes()
            with store.locked() as db:
                with patch("service.MAX_STATE_NODES", 1), patch("service.json.loads", side_effect=AssertionError("must reject before decoding")):
                    with self.assertRaisesRegex(Conflict, "任务图容量超限"):
                        store._nodes(db)
            self.assertEqual(store.log.read_bytes(), original)
            self.assertEqual(store.call("graph_get", {"id": "later"})["node"]["title"], "keep")

    def test_prospective_capacity_failure_is_atomic_and_counts_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); store.init("test", "synthetic")
            original = store.log.read_bytes(); before = store.snapshot()
            with patch("service.MAX_STATE_NODES", 1):
                with self.assertRaisesRegex(Conflict, "任务图容量超限"):
                    store.call("idea_capture", {"id": "too-many", "title": "refuse", "request_id": "too-many"})
            with store.locked() as db:
                size = sum(db.execute(f"SELECT coalesce(sum(length(CAST(data AS BLOB))),0) FROM {table}").fetchone()[0] for table in ("nodes", "edges"))
            with patch("service.MAX_STATE_BYTES", size + 64):
                with self.assertRaisesRegex(Conflict, "任务图容量超限"):
                    store.call("idea_capture", {"id": "too-large", "title": "refuse", "body": "汉" * 1000, "request_id": "too-large"})
            self.assertEqual(store.log.read_bytes(), original)
            self.assertEqual(store.snapshot()["nodes"], before["nodes"])
            self.assertEqual(store.snapshot()["sequence"], before["sequence"])

    def test_oversized_replay_line_is_bounded_without_truncating_original(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); store.init("test", "synthetic")
            with store.log.open("ab") as file:
                file.write(b"x" * 513 + b"\n")
            original = store.log.read_bytes()
            with patch("service.MAX_EVENT_BYTES", 512):
                with self.assertRaisesRegex(Conflict, "单日志事件超过容量上限"):
                    store.snapshot()
            self.assertEqual(store.log.read_bytes(), original)

    def test_replay_capacity_rejection_preserves_later_state_for_bounded_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); store.init("test", "synthetic")
            store.call("idea_capture", {"id": "later", "title": "not old snapshot", "request_id": "later"})
            before = store.snapshot(); original = store.log.read_bytes(); store.db.unlink()
            with patch("service.MAX_STATE_NODES", 1):
                with self.assertRaisesRegex(Conflict, "任务图容量超限"):
                    Store(root).snapshot()
            self.assertEqual(store.log.read_bytes(), original)
            restored = Store(root).snapshot()
            self.assertEqual(restored["nodes"], before["nodes"])
            self.assertEqual(restored["edges"], before["edges"])
            self.assertEqual(restored["sequence"], before["sequence"])

    def test_http_fifth_simultaneous_request_is_rejected_and_capacity_recovers(self):
        entered = threading.Event()
        release = threading.Event()
        count = [0]
        count_lock = threading.Lock()

        class SlowHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with count_lock:
                    count[0] += 1
                    if count[0] == 4:
                        entered.set()
                release.wait(3)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

        server = BoundedHTTPServer(("127.0.0.1", 0), SlowHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        url = f"http://127.0.0.1:{server.server_port}/"
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(lambda: urllib.request.urlopen(url, timeout=3).status)
                           for _ in range(4)]
                self.assertTrue(entered.wait(2))
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url, timeout=2)
                self.assertEqual(caught.exception.code, 503)
                release.set()
                self.assertEqual([f.result(timeout=3) for f in futures], [200] * 4)
            self.assertEqual(urllib.request.urlopen(url, timeout=2).status, 200)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)

    def test_large_body_overview_is_bounded_and_records_remain_queryable(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.init("Synthetic overview", "No business data")
            for batch in range(10):
                items = [{"id": f"large-{batch}-{index}", "kind": "Idea",
                          "title": f"Synthetic {batch}-{index}", "body": "x" * 20000,
                          "source": {"uri": "https://example.invalid/large",
                                     "locator": f"{batch}/{index}"}}
                         for index in range(25)]
                store.call("history_import", {"manifest": {"project_root": root,
                           "batch_id": f"large-{batch}", "sources": [],
                           "nodes": items, "edges": []},
                           "request_id": f"large-{batch}"})
            overview = store.snapshot()
            self.assertEqual(overview["total"], 251)
            self.assertTrue(overview["truncated"])
            self.assertLess(len(overview["nodes"]), 251)
            self.assertLessEqual(sum(len(str(n).encode()) for n in overview["nodes"]),
                                 5 * 1024 * 1024)
            self.assertEqual(store.call("graph_get", {"id": "large-9-24"})["node"]["body"],
                             "x" * 20000)
            before = overview["sequence"]
            store.db.unlink()
            replay = Store(root).snapshot()
            self.assertEqual(replay["sequence"], before)
            self.assertEqual(replay["total"], 251)


if __name__ == "__main__":
    unittest.main()
