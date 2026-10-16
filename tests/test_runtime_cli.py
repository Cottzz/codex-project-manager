import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from unittest.mock import patch

from pgw import runtime_cli, cli_arguments, source_cli, local_source_preview, source_inventory, code_refresh_cli, thread_observation, threads_cli
from service import Store, Conflict


class RuntimeCLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.identity = self.store.init("CLI isolation", "No real execution")["data"]["project_id"]
        self.posts = []
        self.wrong_root = False
        self.drop_response = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                root = str(owner.root) + ("/foreign" if owner.wrong_root else "")
                if self.path == "/api/session":
                    self.reply({"root": root, "csrf": "test-token", "demo_mode": False})
                elif self.path == "/api/projects":
                    self.reply({"projects": [{"id": owner.identity, "root": root}]})
                elif self.path.startswith("/api/source-inventory?"):
                    q = parse_qs(urlparse(self.path).query)
                    self.reply(source_inventory(owner.store, int(q["offset"][0]), int(q["limit"][0])))
                elif self.path.startswith("/api/thread-observation?"):
                    q = parse_qs(urlparse(self.path).query)
                    self.reply(thread_observation(owner.store, q["id"][0], int(q["expected_version"][0])))
                elif self.path.startswith("/api/source?"):
                    q = parse_qs(urlparse(self.path).query)
                    n = owner.store.call("graph_get", {"id": q["id"][0]})["node"]
                    try:
                        self.reply(local_source_preview(n["data"]["source"]))
                    except Conflict:
                        self.send_error(400, "bounded preview refused")
                else:
                    self.reply({"threads": [], "pending_approvals": []})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.posts.append((body, self.headers.get("X-PGW-CSRF")))
                if owner.drop_response:
                    self.close_connection = True
                    return
                self.reply({"result": {"uncertain": True, "reason": "fixture; no dispatch"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)


    def test_code_refresh_rejects_missing_request_and_extra_transfer_fields(self):
        bad = [{}, {"request_id":"../x"}, {"request_id":"valid","action":"approve"},
               {"request_id":"valid","worker":"foreign-host"},
               {"request_id":"valid","source_root":"/private/business"}]
        with patch("subprocess.run") as run:
            for args in bad:
                with self.assertRaises(Conflict): code_refresh_cli(self.store,args)
            run.assert_not_called()

    def test_code_refresh_exact_result_identity_and_no_graph_write(self):
        original=self.store.log.read_bytes()
        def reply(command,stdout,stderr,**kwargs):
            response={"project_id":self.identity,"request_id":"stable","state":"RUNNING"}
            stdout.write(json.dumps(response).encode())
            return subprocess.CompletedProcess(command,0)
        with patch("shutil.which",return_value="node"),patch("subprocess.run",side_effect=reply) as run:
            value=code_refresh_cli(self.store,{"action":"start","request_id":"stable"})
            self.assertEqual(value["refresh"]["state"],"RUNNING")
            run.assert_called_once()
        self.assertEqual(self.store.log.read_bytes(),original)
        def foreign(command,stdout,stderr,**kwargs):
            stdout.write(json.dumps({"project_id":"foreign","request_id":"stable"}).encode())
            return subprocess.CompletedProcess(command,0)
        with patch("shutil.which",return_value="node"),patch("subprocess.run",side_effect=foreign) as run:
            with self.assertRaisesRegex(Conflict,"身份不匹配"): code_refresh_cli(self.store,{"request_id":"stable"})
            run.assert_called_once()
        self.assertEqual(self.store.log.read_bytes(),original)

    def test_code_refresh_timeout_unknown_has_no_second_dispatch(self):
        original=self.store.log.read_bytes()
        with patch("shutil.which",return_value="node"),patch("subprocess.run",side_effect=subprocess.TimeoutExpired("node",180)) as run:
            with self.assertRaisesRegex(Conflict,"结果待核对"): code_refresh_cli(self.store,{"action":"start","request_id":"stable"})
            run.assert_called_once()
        self.assertEqual(self.store.log.read_bytes(),original)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_context_is_read_only_and_requires_exact_task_identity(self):
        with patch("runtime.subprocess.Popen") as spawn:
            value = runtime_cli(self.store, "context", {"task_id": "TASK-1"}, self.server.server_port)
            spawn.assert_not_called()
        self.assertIn("context", value)
        self.assertEqual(self.posts, [])
        for args in [{}, {"task_id": "TASK-1", "request_id": "unnecessary"}]:
            with self.assertRaises(Conflict):
                runtime_cli(self.store, "context", args, self.server.server_port)

    def test_status_and_mutation_use_existing_server_and_explicit_identity(self):
        with patch("runtime.subprocess.Popen") as spawn:
            status = runtime_cli(self.store, "status", {}, self.server.server_port)
            result = runtime_cli(self.store, "reconcile", {
                "thread_id": "fixture", "request_id": "stable-read"}, self.server.server_port)
            spawn.assert_not_called()
        self.assertEqual(status["project_id"], self.identity)
        self.assertTrue(result["result"]["uncertain"])
        body, token = self.posts[0]
        self.assertEqual(body["project_id"], self.identity)
        self.assertEqual(body["request_id"], "stable-read")
        self.assertEqual(token, "test-token")

    def test_identity_mismatch_and_approval_never_send_post(self):
        self.wrong_root = True
        with self.assertRaises(Conflict):
            runtime_cli(self.store, "continue", {"request_id": "foreign"}, self.server.server_port)
        with self.assertRaises(Conflict):
            runtime_cli(self.store, "approval", {"request_id": "approval"}, self.server.server_port)
        self.assertEqual(self.posts, [])

    def test_missing_request_id_and_identity_override_rejected(self):
        for args in ({}, {"request_id": "x", "project_id": self.identity}):
            with self.assertRaises(Conflict):
                runtime_cli(self.store, "continue", args, self.server.server_port)
        self.assertEqual(self.posts, [])

    def test_lost_response_not_retried(self):
        self.drop_response = True
        with self.assertRaises(Conflict):
            runtime_cli(self.store, "interrupt", {"request_id": "one-attempt"},
                        self.server.server_port)
        self.assertEqual(len(self.posts), 1)

    def test_actual_cli_status_and_args_file(self):
        app = Path(__file__).resolve().parents[1] / "pgw.py"
        arguments = self.root / "arguments.json"
        arguments.write_text(json.dumps({"id": "project"}))
        query = subprocess.run([sys.executable, str(app), "--root", str(self.root),
            "call", "graph_get", "--args-file", str(arguments)], capture_output=True, text=True)
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertEqual(json.loads(query.stdout)["node"]["data"]["project_id"], self.identity)
        status = subprocess.run([sys.executable, str(app), "--root", str(self.root),
            "runtime", "status", "--port", str(self.server.server_port)],
            capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["project_id"], self.identity)


if __name__ == "__main__":
    unittest.main()


class SourceCLITest(RuntimeCLITest):
    def setUp(self):
        super().setUp()
        self.original = self.root / "original.txt"
        self.original.write_text("原始证据\n", encoding="utf-8")
        self.evidence = self.store.call("evidence_add", {"id": "E-SOURCE", "title": "本地原件",
            "source": {"uri": str(self.original), "locator": "whole file"}, "request_id": "source-fixture"})

    def preview(self, **extra):
        return source_cli(self.store, "preview", {"id": self.evidence["id"], "expected_version": self.evidence["version"], **extra}, self.server.server_port)

    def test_threads_cli_uses_actual_get_and_explicit_identity_without_post(self):
        self.store.call("task_create", {"id":"T","title":"isolated task","body":"no business execution","request_id":"fixture-task"})
        n=self.store.call("thread_link", {"id":"LINK","title":"same name","thread_id":"exact-thread","task_id":"T","agent":"unknown","status":"unknown","observation_source":"fixture","request_id":"thread-observe-fixture"})
        original=self.store.log.read_bytes()
        with patch("pgw.thread_metadata",return_value={"id":"exact-thread","name":"same name","status":{"type":"notLoaded"}}):
            result=threads_cli(self.store,"observe",{"id":n["id"],"expected_version":1},self.server.server_port)
            self.assertEqual(result["observation"]["thread"]["id"],"exact-thread")
            app=Path(__file__).resolve().parents[1]/"pgw.py"; args=self.root/"observe.json"
            args.write_text(json.dumps({"id":"LINK","expected_version":1}))
            real=subprocess.run([sys.executable,str(app),"--root",str(self.root),"threads","observe","--args-file",str(args),"--port",str(self.server.server_port)],capture_output=True,text=True,timeout=20)
            self.assertEqual(real.returncode,0,real.stderr)
            self.assertEqual(json.loads(real.stdout)["observation"]["thread"]["id"],"exact-thread")
        self.assertEqual(self.store.log.read_bytes(),original); self.assertEqual(self.posts,[])
        for args in ({},{"id":"LINK","expected_version":True},{"id":"LINK","expected_version":2},{"id":"E-SOURCE","expected_version":1},{"id":"LINK","expected_version":1,"project_id":"foreign"}):
            with self.assertRaises(Conflict): threads_cli(self.store,"observe",args,self.server.server_port)
        self.wrong_root=True
        with self.assertRaises(Conflict): threads_cli(self.store,"observe",{"id":"LINK","expected_version":1},self.server.server_port)
        self.assertEqual(self.posts,[])

    def test_source_original_hash_changed_no_adoption_and_no_event(self):
        before = self.store.snapshot()
        before_log = self.store.log.read_bytes()
        first = self.preview()
        self.assertEqual(first["source"]["freshness"], "match")
        self.assertEqual(first["project_id"], self.identity)
        self.assertEqual(first["node_version"], self.evidence["version"])
        self.original.write_text("新正文\n", encoding="utf-8")
        changed = self.preview()["source"]
        self.assertEqual(changed["freshness"], "changed")
        self.assertNotEqual(changed["registered_sha256"], changed["current_sha256"])
        after = self.store.snapshot()
        self.assertEqual(before_log, self.store.log.read_bytes())
        # Observation timestamps advance during a read; persistent content must not.
        self.assertEqual({k: v for k, v in before.items() if k != "at"},
                         {k: v for k, v in after.items() if k != "at"})
        self.assertEqual(self.posts, [])

    def test_source_stale_identity_and_arbitrary_path_refused(self):
        for args in ({"id": "E-SOURCE", "expected_version": 99},
                     {"uri": str(self.original)}, {"id": "E-SOURCE", "expected_version": True},
                     {"id": "E-SOURCE", "expected_version": 1, "version": "anything"}):
            with self.assertRaises(Conflict):
                source_cli(self.store, "preview", args, self.server.server_port)
        self.wrong_root = True
        with self.assertRaises(Conflict): self.preview()
        self.assertEqual(self.posts, [])

    def test_source_inventory_real_pagination_no_qualification(self):
        for i in range(3):
            path = self.root / (str(i) + ".txt")
            path.write_text(str(i))
            self.store.call("evidence_add", {"title": str(i), "source": {"uri": str(path), "locator": "whole"}, "request_id": "page" + str(i)})
        r = source_cli(self.store, "inventory", {"limit": 1}, self.server.server_port)
        self.assertEqual(len(r["source"]["files"]), 1)
        self.assertEqual(r["source"]["next_offset"], 1)
        n = source_cli(self.store, "inventory", {"offset": 1, "limit": 1}, self.server.server_port)
        self.assertNotEqual(r["source"]["files"][0]["uri"], n["source"]["files"][0]["uri"])
        self.assertIn("不自动采纳", r["qualification"])
        for args in ({"limit": 11}, {"limit": True}, {"offset": -1}, {"project_id": "foreign"}, {"request_id": "write"}):
            with self.assertRaises(Conflict): source_cli(self.store, "inventory", args, self.server.server_port)

    def test_source_large_file_server_refuses_without_write(self):
        before = self.store.snapshot()
        before_log = self.store.log.read_bytes()
        self.original.write_bytes(b"a" * 512001)
        with self.assertRaisesRegex(Conflict, "HTTP 400"): self.preview()
        after = self.store.snapshot()
        self.assertEqual(before_log, self.store.log.read_bytes())
        # Observation timestamps advance during a read; persistent content must not.
        self.assertEqual({k: v for k, v in before.items() if k != "at"},
                         {k: v for k, v in after.items() if k != "at"})

    def test_actual_source_cli_uses_args_file_and_explicit_root(self):
        app = Path(__file__).resolve().parents[1] / "pgw.py"
        args = self.root / "source-args.json"
        args.write_text(json.dumps({"id": "E-SOURCE", "expected_version": 1}))
        r = subprocess.run([sys.executable, str(app), "--root", str(self.root), "source", "preview", "--args-file", str(args), "--port", str(self.server.server_port)], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        value = json.loads(r.stdout)
        self.assertEqual(value["source"]["text"], "原始证据\n")
        self.assertEqual(value["node_id"], "E-SOURCE")
        r = subprocess.run([sys.executable, str(app), "source", "inventory", "--port", str(self.server.server_port)], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("显式--root", r.stderr)


class ThreadObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name); self.store.init("observation fixture","no real chat or business execution")
        self.store.call("task_create", {"id":"T","title":"isolated task","body":"no business execution","request_id":"fixture-task"})
        self.node=self.store.call("thread_link", {"id":"LINK","title":"same name","thread_id":"exact-thread","task_id":"T","agent":"unknown","status":"unknown","observation_source":"fixture","request_id":"thread-fixture"})

    def test_exact_metadata_only_read_and_historical_state_preserved(self):
        original=self.store.log.read_bytes()
        with patch("pgw.thread_metadata",return_value={"id":"exact-thread","name":"same name","status":{"type":"notLoaded"},"updatedAt":1,"turns":[{"text":"must not return"}]}) as read:
            result=thread_observation(self.store,"LINK",1)
            read.assert_called_once_with(self.store,"exact-thread")
        self.assertEqual(result["thread"]["id"],"exact-thread")
        self.assertEqual(result["thread"]["status"],"notLoaded")
        self.assertFalse(result["control_verified"])
        self.assertNotIn("turns",result); self.assertNotIn("must not return",json.dumps(result))
        self.assertIn("桌面空闲",result["scope"])
        self.assertEqual(self.store.log.read_bytes(),original)

    def test_stale_foreign_and_mid_read_versions_never_accepted(self):
        original=self.store.log.read_bytes()
        with patch("pgw.thread_metadata") as read:
            for args in (("LINK",True),("LINK",2),("project",1)):
                with self.assertRaises(Conflict): thread_observation(self.store,*args)
            read.assert_not_called()
        self.assertEqual(self.store.log.read_bytes(),original)
        with patch("pgw.thread_metadata",return_value={"id":"foreign"}):
            with self.assertRaisesRegex(Conflict,"身份不同"): thread_observation(self.store,"LINK",1)
        self.assertEqual(self.store.log.read_bytes(),original)
        def changed(store,tid):
            store.call("node_update", {"id":"LINK","expected_version":1,"title":"same name updated","actor":"fixture","request_id":"fixture-concurrent"})
            return {"id":tid}
        with patch("pgw.thread_metadata",side_effect=changed):
            with self.assertRaisesRegex(Conflict,"观察期间"): thread_observation(self.store,"LINK",1)

    def test_metadata_uses_official_no_turns_contract_and_exact_cwd(self):
        from codex_bridge import metadata,BridgeError
        with patch("codex_bridge.request",return_value={"thread":{"id":"exact-thread","cwd":str(self.store.root)}}) as request:
            result=metadata(self.store,"exact-thread")
            request.assert_called_once_with("thread/read",{"threadId":"exact-thread","includeTurns":False})
        with patch("codex_bridge.request",return_value={"thread":{"id":"exact-thread","cwd":"/foreign"}}):
            with self.assertRaises(BridgeError): metadata(self.store,"exact-thread")
