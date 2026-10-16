import concurrent.futures
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import Store, Conflict
from pgw import dispatch, tools_list

APP = Path(__file__).resolve().parents[1] / "pgw.py"

class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.s = Store(self.root)
        self.s.init("独立验证项目", "验证持久状态与约束")
        self.receipt = self.root / "receipt.txt"
        self.receipt.write_text("actual test receipt\n")
        self.file = self.root / "output.txt"
        self.file.write_text("version1")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, name, **a):
        if name not in {"graph_get", "current_frontier", "project_status", "graph_neighbors", "impact_analysis", "timeline", "graph_search"}:
            a.setdefault("request_id", str(uuid.uuid4()))
        a.setdefault("actor", "test-agent") if name not in {"graph_get", "current_frontier", "project_status", "graph_neighbors", "impact_analysis", "timeline", "graph_search"} else None
        return self.s.call(name, a)

    def task(self, id="a", deps=None):
        return self.call("task_create", id=id, title=id, body="范围与验收", depends_on=deps or [])

    def read(self, id):
        return self.call("graph_get", id=id)["node"]

    def mutate(self, op, id, **a):
        return self.call(op, id=id, expected_version=self.read(id)["version"], **a)

    def validate(self, id, result="PASS"):
        return self.mutate("validation_record", id, check="technical", result=result, body="实际测试记录", source={"uri": str(self.receipt), "locator": "line 1"}, files=[{"uri": str(self.file), "locator": "whole file"}])

    def finish(self, id):
        self.mutate("task_claim", id)
        self.validate(id)
        return self.mutate("task_complete", id)

    def bucket(self, id):
        return next(n["bucket"] for n in self.call("current_frontier")["tasks"] if n["id"] == id)

    def test_evidence_influence_uses_real_relation_direction_and_preserves_gates(self):
        self.task(); self.task("untouched")
        e = self.call("evidence_add", id="source", title="来源", source={"uri": str(self.file), "locator": "whole"})
        c = self.call("node_create", id="claim", kind="Claim", title="限定判断")
        self.call("relation_add", source_id=e["id"], relation="contradicts", target_id=c["id"], basis="真实反证方向")
        self.call("relation_add", source_id="a", relation="reads", target_id=c["id"], basis="任务读取判断")
        artifact = self.call("artifact_register", id="artifact", title="产物", task_id="a", source={"uri": str(self.file), "locator": "whole"})
        commit = self.call("commit_link", id="commit", title="提交", path=str(self.root), sha="a" * 40)
        self.call("relation_add", source_id=artifact["id"], relation="committed_as", target_id=commit["id"], basis="实际提交关联")
        self.call("human_gate_request", id="gate", title="最终人工", task_id="a", body="必须用户验收")
        before = self.s.snapshot()
        q = self.call("impact_analysis", id=e["id"], depth=4, limit=2)
        self.assertEqual(q["total"], 5)
        self.assertEqual(q["bounds_hit"], [])
        self.assertTrue(q["truncated"])
        full = self.call("impact_analysis", id=e["id"], depth=4, limit=10)
        self.assertEqual({n["id"] for n in full["nodes"]}, {"source", "claim", "a", "artifact", "commit"})
        self.assertEqual([(x["relation"], x["influence_direction"]) for x in full["paths"]["commit"]], [("contradicts", "forward"), ("reads", "reverse"), ("produces", "forward"), ("committed_as", "forward")])
        self.assertEqual(self.s.snapshot(), before)
        self.assertEqual(self.bucket("a"), "Waiting Human")
        self.assertEqual(self.bucket("untouched"), "Ready")

    def test_influence_depth_and_cycle_have_explicit_unknown_boundary(self):
        self.task(); self.task("b", ["a"]); self.task("c", ["b"])
        q = self.call("impact_analysis", id="a", depth=1)
        self.assertEqual({n["id"] for n in q["nodes"]}, {"a", "b"})
        self.assertEqual(q["bounds_hit"], ["depth_limit"])
        self.assertFalse(q["total_known_within_depth"])
        with self.assertRaises(Conflict): self.call("impact_analysis", id="a", relations=["made_up"])
        self.finish("a")
        validation=next(n["id"] for n in self.s.snapshot()["nodes"] if n["kind"]=="Validation")
        q=self.call("impact_analysis", id=validation, depth=4)
        self.assertEqual({n["id"] for n in q["nodes"]}, {validation,"a","b","c"})
        self.assertEqual(q["paths"]["a"][0]["influence_direction"], "forward")
        q=self.call("impact_analysis", id="a", depth=1)
        self.assertEqual(q["paths"][validation][0]["influence_direction"], "reverse")
        self.assertEqual(q["relation_directions"]["validates"], "both")
        x=self.call("idea_capture", id="x", title="x")
        y=self.call("idea_capture", id="y", title="y")
        self.call("relation_add", source_id="x", relation="derived_from", target_id="y", basis="循环知识")
        self.call("relation_add", source_id="y", relation="derived_from", target_id="x", basis="循环知识")
        q=self.call("impact_analysis", id="x", depth=6)
        self.assertEqual(q["total"], 2)
        self.assertEqual(q["bounds_hit"], [])
        self.assertEqual(len(q["paths"]["y"]), 1)

    def test_influence_admission_limits_cannot_claim_unseen_total(self):
        snap=self.s.snapshot(); nodes={n["id"]: n for n in snap["nodes"]}
        nodes["source"]={"id":"source","kind":"Evidence"}
        edges={}
        for i in range(1002):
            key=f"c{i:04d}"; nodes[key]={"id":key,"kind":"Claim"}
            edges[key]={"id":key,"source":"source","target":key,"relation":"supports","basis":"限定候选","method":"declared"}
        q=self.s._impact({"id":"source","depth":6,"limit":500},nodes,edges,1)
        self.assertEqual(q["total_observed"],500)
        self.assertEqual(q["bounds_hit"],["node_limit","relation_limit"])
        self.assertFalse(q["total_known_within_depth"])
        self.assertLessEqual(len(q["path_nodes"]),500)
        unrelated={f"u{i}":{"id":f"u{i}","source":"project","target":"source","relation":"contains"} for i in range(6001)}
        q=self.s._impact({"id":"source"},nodes,unrelated,1)
        self.assertEqual(q["edge_scan_count"],6000)
        self.assertEqual(q["total_observed"],1)
        self.assertEqual(q["bounds_hit"],["edge_scan_limit"])
        self.assertFalse(q["total_known_within_depth"])

    def test_qualified_inputs_dirty_only_actual_downstream_and_recover(self):
        self.task(); self.task("b", ["a"]); self.task("untouched"); self.task("gated")
        self.call("human_gate_request", id="gate", task_id="gated", title="人工验收", body="仍待用户")
        self.finish("untouched")
        evidence=self.call("evidence_add", id="e", title="来源", source={"uri": str(self.file), "locator": "whole"})
        claim=self.call("node_create", id="c", kind="Claim", title="限定判断")
        self.call("relation_add", source_id="e", relation="supports", target_id="c", basis="明确已用来源")
        self.call("relation_add", source_id="a", relation="reads", target_id="c", basis="本任务输入")
        self.mutate("task_claim", "a")
        def record():
            return self.mutate("validation_record", "a", check="technical", result="PASS", body="限定验证", source={"uri": str(self.receipt),"locator":"line1"}, inputs=[{"id":"c","expected_version":self.read("c")["version"]}])
        validation=record(); self.assertEqual(validation["data"]["input_binding"]["revisions"], {"c":1,"e":1})
        self.assertEqual(validation["data"]["files"][0]["uri"], str(self.file))
        self.mutate("task_complete", "a"); self.finish("b")
        self.mutate("node_update", "e", body="该来源出现受影响限定")
        seq=self.s.snapshot()["sequence"]
        self.assertEqual(self.bucket("a"), "Pending Validation")
        self.assertEqual(self.bucket("b"), "Blocked")
        self.assertEqual(self.bucket("untouched"), "Completed")
        self.assertEqual(self.bucket("gated"), "Waiting Human")
        self.assertEqual(self.s.snapshot()["sequence"], seq)
        record(); self.assertEqual(self.bucket("a"), "Completed"); self.assertEqual(self.bucket("b"), "Completed")
        counter=self.call("evidence_add", id="counter", title="新增反证", source={"uri":str(self.file),"locator":"whole"})
        self.call("relation_add", source_id=counter["id"],relation="contradicts",target_id="c",basis="新反证必须重新核对")
        self.assertEqual(self.bucket("a"), "Pending Validation")
        record(); self.assertEqual(self.bucket("a"), "Completed")
        self.file.write_text("actual changed input bytes")
        self.assertEqual(self.bucket("a"), "Pending Validation")
        with self.assertRaises(Conflict): record()
        self.mutate("node_update","e",source={"uri":str(self.file),"locator":"whole"})
        self.mutate("node_update","counter",source={"uri":str(self.file),"locator":"whole"})
        record(); self.assertEqual(self.bucket("a"), "Completed")
        self.assertEqual(self.read("gate")["status"], "pending")

    def test_input_bindings_reject_stale_duplicate_invalid_and_oversize_atomically(self):
        schema=next(t["inputSchema"] for t in tools_list() if t["name"]=="validation_record")
        self.assertEqual(schema["properties"]["inputs"]["maxItems"],32)
        self.task(); self.mutate("task_claim", "a")
        e=self.call("evidence_add", id="e", title="来源", source={"uri": str(self.file),"locator":"whole"})
        def record(inputs):
            return self.mutate("validation_record","a",check="technical",result="PASS",body="限定",source={"uri":str(self.receipt),"locator":"line1"},inputs=inputs)
        bad=[[],[{"id":"e","expected_version":0}],[{"id":"e","expected_version":1,"extra":True}],[{"id":"a","expected_version":self.read("a")["version"]}],[{"id":"e","expected_version":1}]*2,[{"id":"e","expected_version":1}]*33]
        for inputs in bad:
            before=self.s.snapshot()
            with self.assertRaises(Conflict): record(inputs)
            self.assertEqual(self.s.snapshot(),before)
        self.file.write_bytes(b"x"*512001)
        self.mutate("node_update","e",source={"uri":str(self.file),"locator":"whole"})
        before=self.s.snapshot()
        with self.assertRaises(Conflict): record([{"id":"e","expected_version":2}])
        self.assertEqual(self.s.snapshot(),before)
        self.assertEqual(self.validate("a")["data"].get("input_binding"),None)

    def test_bound_check_cannot_silently_drop_inputs_on_subsequent_receipt(self):
        self.task(); self.mutate("task_claim","a")
        e=self.call("evidence_add",id="e",title="实际输入",source={"uri":str(self.file),"locator":"whole"})
        self.mutate("validation_record","a",check="technical",result="PASS",body="已核输入",source={"uri":str(self.receipt),"locator":"line1"},inputs=[{"id":"e","expected_version":e["version"]}])
        for result in ("PASS","UNKNOWN","FAIL"):
            before=self.s.snapshot()
            with self.assertRaises(Conflict): self.validate("a",result)
            self.assertEqual(self.s.snapshot(),before)
        self.mutate("validation_record","a",check="technical",result="UNKNOWN",body="保留准确输入的未知",source={"uri":str(self.receipt),"locator":"line1"},inputs=[{"id":"e","expected_version":e["version"]}])
        self.assertEqual(self.bucket("a"),"In Progress")

    def test_input_observation_boundary_cannot_be_promoted_to_pass(self):
        snap=self.s.snapshot(); nodes={n["id"]:n for n in snap["nodes"]};nodes["c"]={"id":"c","kind":"Claim","work_revision":1}
        edges={}
        for i in range(140):
            key=f"e{i:04d}";nodes[key]={"id":key,"kind":"Evidence","work_revision":1};edges[key]={"id":key,"source":key,"target":"c","relation":"supports"}
        q=self.s._input_observation(["c"],nodes,edges)
        self.assertEqual(len(q["revisions"]),128);self.assertEqual(q["bounds_hit"],["node_limit"])
        unrelated={f"u{i}":{"id":f"u{i}","source":"project","target":"c","relation":"contains"} for i in range(6001)}
        self.assertEqual(self.s._input_observation(["c"],nodes,unrelated)["bounds_hit"],["edge_scan_limit"])

    def test_dependency_and_reopen_propagation(self):
        self.task(); self.task("b", ["a"])
        with self.assertRaises(Conflict): self.mutate("task_claim", "b")
        self.finish("a"); self.assertEqual(self.bucket("b"), "Ready")
        self.finish("b"); self.assertEqual(self.bucket("b"), "Completed")
        self.mutate("task_progress", "a", status="todo", body="出现新事实需要重验")
        self.assertEqual(self.bucket("b"), "Blocked")

    def test_dependency_cycle_and_dangling_are_atomic(self):
        self.task(); self.task("b", ["a"])
        before = self.s.snapshot()["sequence"]
        with self.assertRaises(Conflict): self.call("relation_add", source_id="a", relation="depends_on", target_id="b", basis="bad", expected_version=1)
        with self.assertRaises(Conflict): self.task("c", ["missing"])
        self.assertEqual(self.s.snapshot()["sequence"], before)
        self.assertEqual(self.read("a")["version"], 1)
        with self.assertRaises(Conflict): self.read("c")

    def test_knowledge_cycles_allowed_and_evidence_direction_checked(self):
        x=self.call("idea_capture", title="想法 A")
        y=self.call("hypothesis_create", title="假设 B", body="待验证")
        self.call("relation_add", source_id=x["id"], relation="related_to", target_id=y["id"], basis="相关")
        self.call("relation_add", source_id=y["id"], relation="related_to", target_id=x["id"], basis="反向相关")
        with self.assertRaises(Conflict): self.call("relation_add", source_id=x["id"], relation="supports", target_id=y["id"], basis="不能用想法冒充证据")

    def test_claim_requires_real_evidence_node(self):
        c=self.call("node_create", kind="Claim", title="判断")
        with self.assertRaises(Conflict): self.mutate("claim_update", c["id"], status="supported")
        e=self.call("evidence_add", title="原件", source={"uri": str(self.file), "locator":"line 1"})
        self.call("relation_add", source_id=e["id"], relation="supports", target_id=c["id"], basis="指定条件内支持")
        self.assertEqual(self.mutate("claim_update", c["id"], status="supported")["status"], "supported")

    def test_stale_version_and_owner_rejected(self):
        self.task(); self.mutate("task_claim", "a")
        with self.assertRaises(Conflict): self.call("task_progress", id="a", expected_version=1, body="stale")
        with self.assertRaises(Conflict): self.call("task_progress", id="a", expected_version=2, body="takeover", actor="another")

    def test_parallel_process_claims_one_winner(self):
        self.task()
        def claim(actor):
            args={"id":"a","expected_version":1,"actor":actor,"request_id":actor}
            return subprocess.run([sys.executable,str(APP),"--root",str(self.root),"call","task_claim",json.dumps(args)],capture_output=True,text=True)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(claim,["worker1","worker2"]))
        self.assertEqual(sorted(r.returncode for r in results), [0,1])
        self.assertIn(self.read("a")["data"]["owner"], ["worker1","worker2"])

    def test_current_validation_required_and_source_change_invalidates(self):
        self.task(); self.mutate("task_claim", "a")
        with self.assertRaises(Conflict): self.mutate("task_complete", "a")
        self.validate("a", "UNKNOWN")
        with self.assertRaises(Conflict): self.mutate("task_complete", "a")
        self.validate("a"); self.mutate("task_progress", "a", body="changed", status="pending_validation")
        with self.assertRaises(Conflict): self.mutate("task_complete", "a")
        self.validate("a"); self.mutate("task_complete", "a")
        self.file.write_text("version2")
        self.assertEqual(self.bucket("a"), "Pending Validation")

    def test_later_fail_supersedes_pass(self):
        self.task(); self.mutate("task_claim", "a"); self.validate("a"); self.validate("a", "FAIL")
        with self.assertRaises(Conflict): self.mutate("task_complete", "a")

    def test_human_gate_cannot_be_resolved_by_mcp_or_generic_edit(self):
        self.task()
        gate=self.call("human_gate_request", title="真实停止点", task_id="a", body="必须用户判断")
        self.assertEqual(self.bucket("a"), "Waiting Human")
        with self.assertRaises(Conflict): self.mutate("task_claim", "a")
        with self.assertRaises(Conflict): self.mutate("human_gate_resolve", gate["id"], decision="approved", body="model says yes")
        with self.assertRaises(Conflict): self.mutate("node_update", gate["id"], status="approved")
        self.assertNotIn("human_gate_resolve", [t["name"] for t in tools_list()])
        self.s.call("human_gate_resolve", {"id":gate["id"],"expected_version":1,"decision":"approved","body":"测试用户明确操作","request_id":"ui-approval"},channel="ui")
        self.assertEqual(self.bucket("a"), "Ready")

    def test_idempotency_does_not_duplicate_and_rejects_changed_args(self):
        a={"title":"长期想法","request_id":"stable"}
        first=self.s.call("idea_capture",a)
        self.assertEqual(self.s.call("idea_capture",a), first)
        with self.assertRaises(Conflict): self.s.call("idea_capture",{**a,"title":"other"})
        self.assertEqual(self.s.snapshot()["sequence"],2)

    def test_fsync_event_survives_projection_failure(self):
        original=self.s._apply
        def fail(*args): raise OSError("simulated disk error after append")
        self.s._apply=fail
        with self.assertRaises(OSError): self.s.call("idea_capture",{"title":"survive","request_id":"crash"})
        self.s._apply=original
        result=self.s.call("idea_capture",{"title":"survive","request_id":"crash"})
        self.assertEqual(result["title"],"survive")
        self.assertEqual(self.s.snapshot()["sequence"],2)

    def test_fresh_process_and_full_replay_equal(self):
        self.task();self.finish("a")
        before=self.s.snapshot()
        self.s.db.unlink()  # isolated test projection only
        after=Store(self.root).snapshot()
        self.assertEqual(after["nodes"],before["nodes"]);self.assertEqual(after["edges"],before["edges"])
        result=subprocess.run([sys.executable,str(APP),"--root",str(self.root),"call","project_status"],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout)["frontier"][0]["bucket"],"Completed")

    def test_corrupt_or_partial_log_is_not_silently_repaired(self):
        with self.s.log.open("ab") as f:f.write(b'{"partial":')
        raw=self.s.log.read_bytes()
        with self.assertRaises(Conflict):self.s.snapshot()
        self.assertEqual(raw,self.s.log.read_bytes())

    def test_hash_tamper_detected_on_rebuild(self):
        raw=self.s.log.read_text();self.s.log.write_text(raw.replace('独立验证项目','篡改验证项目'))
        self.s.db.unlink()
        with self.assertRaises(Conflict):self.s.snapshot()

    def test_atomic_idea_promotion_and_original_retained(self):
        idea=self.call("idea_capture",title="原想法",body="原文")
        child=self.mutate("idea_promote",idea["id"],kind="Task",title="验证任务",body="实际验收")
        self.assertEqual(self.read(idea["id"])["body"],"原文")
        self.assertTrue(any(e["relation"]=='promoted_to' and e["target"]==child["id"] for e in self.s.snapshot()["edges"]))

    def test_query_limit_and_impact_direction(self):
        self.task();self.task("b",["a"]);self.task("c",["b"])
        r=self.call("impact_analysis",id="a",depth=1)
        self.assertEqual({n["id"] for n in r["nodes"]},{"a","b"})
        self.assertTrue(self.call("graph_get",limit=1)["truncated"])

    def test_thread_maps_and_keeps_unknown_runtime(self):
        self.task()
        t=self.call("thread_link",title="真实引用",thread_id="thread-actual",task_id="a")
        self.assertFalse(t["data"]["runtime_connected"])
        self.assertEqual(t["data"]["status"],"unknown")
        with self.assertRaises(Conflict):self.call("thread_link",title="duplicate",thread_id="thread-actual",task_id="a")

    def test_mcp_handshake_list_mutation_and_new_process_read(self):
        messages=[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"integration-test","version":"1"}}},{"jsonrpc":"2.0","method":"notifications/initialized"},{"jsonrpc":"2.0","id":2,"method":"tools/list"},{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"idea_capture","arguments":{"title":"来自 MCP","request_id":"mcp-write"}}}]
        r=subprocess.run([sys.executable,str(APP),"--root",str(self.root),"mcp"],input='\n'.join(map(json.dumps,messages))+'\n',capture_output=True,text=True,check=True)
        out=[json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(len(out),3)
        names={tool["name"] for tool in out[1]["result"]["tools"]}
        self.assertTrue({"idea_capture","history_import","project_status"} <= names)
        self.assertFalse(out[2]["result"]["isError"])
        self.assertTrue(any(n["title"]=='来自 MCP' for n in Store(self.root).snapshot()["nodes"]))

    def test_http_persistence_origin_and_path_controls(self):
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        p=subprocess.Popen([sys.executable,str(APP),"--root",str(self.root),"serve","--port",str(port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertIn('http://',p.stdout.readline())
            base=f'http://127.0.0.1:{port}'
            session=json.load(urllib.request.urlopen(base+'/api/session'))
            data=json.dumps({"project_id":self.s.call("project_open")["project"]["data"]["project_id"],"name":"idea_capture","arguments":{"title":"Web save","request_id":"web-write"}}).encode()
            req=urllib.request.Request(base+'/api/call',data=data,headers={'Content-Type':'application/json','X-PGW-CSRF':session['csrf']})
            self.assertEqual(json.load(urllib.request.urlopen(req))["result"]["title"],"Web save")
            req.add_header('Origin','https://example.com')
            with self.assertRaises(urllib.error.HTTPError) as err:urllib.request.urlopen(req)
            self.assertEqual(err.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(base+'/../../service.py')
            self.assertIn('Codex Project Manager',urllib.request.urlopen(base+'/').read().decode())
        finally:
            p.terminate();p.communicate(timeout=5)
        self.assertTrue(any(n["title"]=='Web save' for n in Store(self.root).snapshot()["nodes"]))

if __name__ == '__main__': unittest.main(verbosity=2)
