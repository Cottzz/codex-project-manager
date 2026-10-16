import hashlib
import json
import os
import sys
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_index import CodeIndex
from service import Conflict, Store


class CodeIndexTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        self.store = Store(self.root)
        self.store.init("code test", "isolation")
        self.pid = self.store.call("project_open")["project"]["data"]["project_id"]
        self.cache = base / "cache"
        self.cache.mkdir()
        db = sqlite3.connect(self.cache / "test.sqlite")
        db.executescript("""
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE files(path TEXT PRIMARY KEY,language TEXT,sha256 TEXT,bytes INTEGER,status TEXT,errors TEXT);
          CREATE TABLE symbols(id TEXT PRIMARY KEY,path TEXT,kind TEXT,name TEXT,line INTEGER,end_line INTEGER);
          CREATE TABLE unresolved(path TEXT,kind TEXT,text TEXT,line INTEGER,reason TEXT);
          CREATE TABLE relations(source TEXT,target TEXT,kind TEXT,method TEXT,path TEXT,line INTEGER);
          CREATE INDEX relations_target ON relations(target);
        """)
        db.executemany("INSERT INTO meta VALUES (?,?)", [
            ("project_id", json.dumps(self.pid)), ("source_root", json.dumps(str(self.root))),
            ("built_at", json.dumps("2026-09-29T00:00:00Z")),
            ("parser", json.dumps("tree-sitter test"))])
        path = str(self.root / "main.py")
        db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", (path, "python", "sha", 40, "parsed", "[]"))
        db.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?)", [
            ("caller", path, "function", "caller", 1, 4),
            ("target", path, "function", "target", 6, 9)])
        db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                   ("caller", "target", "calls", "unique_same_file_symbol", path, 2))
        db.commit()
        db.close()
        (self.cache / "manifest.json").write_text(json.dumps({
            self.pid: {"filename": "test.sqlite", "sha256": "fixture-sha"}}))
        self.patch = patch("code_index.CACHE_ROOT", self.cache)
        self.patch.start()
        self.addCleanup(self.patch.stop)


    def test_refresh_observation_is_separate_bounded_and_project_scoped(self):
        index=CodeIndex(self.store)
        path=self.cache/("project-refresh-"+self.pid+".json")
        self.assertIsNone(index.status()["refresh"])
        path.write_text(json.dumps({"project_id":self.pid,"request_id":"r1","state":"RESULT_UNKNOWN","observed_at":"test"}))
        self.assertEqual(index.status()["refresh"]["state"],"RESULT_UNKNOWN")
        self.assertEqual(index.status()["version"],"fixture-sha")
        path.write_text("bad JSON")
        self.assertEqual(index.status()["refresh"]["state"],"UNKNOWN")
        path.write_text("x"*8193)
        self.assertEqual(index.status()["refresh"]["state"],"UNKNOWN")
        path.write_text(json.dumps({"project_id":"foreign"}))
        with self.assertRaisesRegex(Conflict,"项目身份不一致"):index.status()

    def test_language_semantics_are_metadata_scope_not_parse_or_runtime_success(self):
        index = CodeIndex(self.store)
        self.assertEqual(index.status()["semantic_capabilities"], {})
        self.assertIn("旧缓存", index.status()["semantic_scope"])
        capability = {"css": {"symbols": "selector rule sets", "calls": "none"}}
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO meta VALUES (?,?)", ("semantic_capabilities", json.dumps(capability)))
            db.execute("INSERT INTO meta VALUES (?,?)", ("symbol_extractor_version", "3"))
        status = index.status()
        self.assertEqual(status["semantic_capabilities"], capability)
        self.assertEqual(status["symbol_extractor_version"], 3)
        self.assertEqual(status["files"][0]["status"], "parsed")
        self.assertIn("parsed不等于关系完整", status["semantic_scope"])

    def test_scope_pagination_impact_context_and_git(self):
        index = CodeIndex(self.store)
        self.assertTrue(index.status()["available"])
        first = index.query("search", {"limit": 1})
        self.assertTrue(first["truncated"])
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(index.query("impact", {"id": "target"})["items"], ["caller"])
        self.assertEqual(index.query("context", {"query": "target"})["items"][0]["name"], "target")
        self.assertEqual(index.query("file", {"path": str(self.root / "main.py")})["file"]["status"], "parsed")
        inventory = index.query("inventory", {"limit": 1})
        self.assertEqual(inventory["project_id"], self.pid)
        self.assertEqual(inventory["items"][0]["path"], str(self.root / "main.py"))
        self.assertEqual(inventory["items"][0]["errors"], [])
        self.assertEqual(index.query("inventory", {"status": "parse_error"})["items"], [])
        with self.assertRaises(Conflict):
            index.query("inventory", {"status": "invented"})
        self.assertEqual(index.query("git", {})["state"], "nonGit")
        with self.assertRaises(Conflict):
            index.query("file", {"path": "/other/private.py"})


    def test_impact_readable_locations_and_visited_witness(self):
        path = str(self.root / "main.py")
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       (path, "caller", "imports", "fixture_import", path, 1))
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       ("caller", "unvisited", "calls", "must_not_be_witness", path, 99))
        result = CodeIndex(self.store).query("impact", {"id": "target", "depth": 2})
        self.assertEqual(result["project_id"], self.pid)
        self.assertEqual(result["version"], "fixture-sha")
        self.assertEqual([n["id"] for n in result["nodes"]], result["items"])
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertEqual(nodes["caller"]["path"], path)
        self.assertEqual(nodes["caller"]["name"], "caller")
        self.assertEqual(nodes["caller"]["line"], 1)
        self.assertEqual(nodes["caller"]["via"], {
            "target": "target", "kind": "calls", "method": "unique_same_file_symbol",
            "path": path, "line": 2})
        self.assertEqual(nodes[path]["kind"], "file")
        self.assertEqual(nodes[path]["name"], "main.py")
        self.assertEqual(nodes[path]["via"]["target"], "caller")
        limited = CodeIndex(self.store).query("impact", {"id": "target", "depth": 2, "limit": 1})
        self.assertEqual(len(limited["nodes"]), 1)
        self.assertEqual(limited["nodes"][0]["id"], limited["items"][0])
        self.assertIn("item_limit", limited["bounds_hit"])

    def test_impact_path_is_shortest_visited_chain_even_with_cycle(self):
        path = str(self.root / "main.py")
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            for row in [("outer", "caller", "calls", "outer_call", path, 11),
                        ("caller", "outer", "calls", "cycle_edge", path, 12),
                        ("outer", "unvisited", "calls", "must_not_show", path, 99)]:
                db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)", row)
        result = CodeIndex(self.store).query("impact", {"id":"target", "depth":3})
        caller = next(n for n in result["nodes"] if n["id"] == "caller")
        self.assertEqual(caller["via"]["target"],"target")
        node = next(n for n in result["nodes"] if n["id"] == "outer")
        self.assertEqual([(e["source"],e["target"]) for e in node["witness_path"]],
                         [("outer","caller"),("caller","target")])
        self.assertTrue(node["path_reaches_start"])
        locations = {n["id"]: n for n in result["path_nodes"]}
        self.assertEqual(locations["target"]["name"],"target")
        self.assertEqual(locations["caller"]["path"],path)
        self.assertEqual(locations["outer"]["kind"],"unresolved")
        self.assertEqual(node["via"]["method"],"outer_call")
        self.assertEqual(result["observed_candidate_count"],2)
        self.assertTrue(result["total_candidates_known"])

    def test_impact_paths_keep_intermediate_even_when_candidate_list_omits_it(self):
        path = str(self.root / "main.py")
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       ("a_outer","caller","calls","outer_call",path,11))
        result = CodeIndex(self.store).query("impact", {"id":"target","depth":2,"limit":1})
        self.assertEqual(result["items"],["a_outer"])
        self.assertEqual([e["target"] for e in result["nodes"][0]["witness_path"]],["caller","target"])
        self.assertEqual(result["omitted_observed_candidates"],1)
        self.assertIn("caller",[n["id"] for n in result["path_nodes"]])
        self.assertIn("item_limit",result["bounds_hit"])
        self.assertTrue(result["total_candidates_known"])

    def test_neighborhood_reports_indexed_omissions(self):
        path = str(self.root / "main.py")
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       ("outer","target","calls","outer_call",path,11))
        result = CodeIndex(self.store).query("neighborhood", {"id":"target","limit":1})
        self.assertEqual(result["project_id"],self.pid)
        self.assertEqual(result["version"],"fixture-sha")
        self.assertEqual((result["total_edges"],result["returned_edges"],result["omitted_edges"]),(2,1,1))
        self.assertTrue(result["truncated"])

    def test_impact_unknown_endpoint_remains_explicit(self):
        path = str(self.root / "main.py")
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       ("missing", "target", "calls", "fixture", path, 3))
        result = CodeIndex(self.store).query("impact", {"id": "target"})
        node = next(n for n in result["nodes"] if n["id"] == "missing")
        self.assertEqual(node["kind"], "unresolved")
        self.assertIsNone(node["path"])
        self.assertIsNone(node["line"])

    def test_neighborhood_order_is_stable_across_process_hash_seeds(self):
        path = str(self.root / "main.py")
        identities = ["node-a", "node-b", "node-c", "node-d", "node-e", "node-f"]
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?)", [
                (identity, path, "function", identity, 20+i, 20+i)
                for i,identity in enumerate(identities)])
            db.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?)", [
                (identity, "target", "calls", "fixture", path, 20+i)
                for i,identity in enumerate(identities)])
        script = ("import json,sys;from pathlib import Path;import code_index;from service import Store;"
                  "code_index.CACHE_ROOT=Path(sys.argv[2]);"
                  "print(json.dumps(code_index.CodeIndex(Store(sys.argv[1])).query('neighborhood', {'id':'target'})['nodes']))")
        observations = []
        for seed in ["2", "17"]:
            output = subprocess.check_output([sys.executable, "-c", script, str(self.root), str(self.cache)],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONHASHSEED": seed}, text=True, timeout=15)
            observations.append([node['id'] for node in json.loads(output)])
        expected = ["caller", *identities, "target"]
        self.assertEqual(observations, [expected, expected])

    def test_wrong_project_cache_rejected(self):
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("UPDATE meta SET value=? WHERE key='project_id'", (json.dumps("wrong"),))
        with self.assertRaises(Conflict):
            CodeIndex(self.store).status()

    def test_import_file_node_can_be_traversed_without_cross_project_paths(self):
        path = str(self.root / "main.py")
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       ("caller", path, "imports", "fixture_static_import", path, 1))
        index = CodeIndex(self.store)
        result = index.query("neighborhood", {"id": path})
        center = next(n for n in result['nodes'] if n['id'] == path)
        self.assertEqual(center['kind'], 'file')
        self.assertEqual(center['path'], path)
        self.assertEqual(result['edges'][0]['source'], 'caller')
        self.assertEqual(result['edges'][0]['target'], path)
        with self.assertRaises(Conflict):
            index.query("neighborhood", {"id": "/other/project/main.py"})

    def test_hidden_frontier_and_budget_cutoffs_are_reported(self):
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?)", [
                (f"upstream-{i:03}", "target", "calls", "fixture", "fixture.py", i)
                for i in range(105)])
            db.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?)", [
                (f"long-{i}", str(self.root / "main.py"), "function", "预算" * 80 + str(i), 10+i, 10+i)
                for i in range(105)])
        index = CodeIndex(self.store)
        impact = index.query("impact", {"id": "target", "depth": 1, "limit": 100})
        self.assertTrue(impact["truncated"])
        self.assertIn("edge_limit", impact["bounds_hit"])
        self.assertEqual(len(impact["items"]), 100)
        context = index.query("context", {"query": "预算", "budget_tokens": 100, "limit": 100})
        self.assertEqual(context["items"], [])
        self.assertTrue(context["truncated"])
        self.assertIn("budget_limit", context["bounds_hit"])
        self.assertIn("candidate_limit", context["bounds_hit"])
        self.assertLessEqual(context["estimated_tokens"], context["budget_tokens"])

    def test_git_staged_untracked_and_missing_commit_are_separate(self):
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        (self.root / "staged file.txt").write_text("staged")
        (self.root / "未追踪.txt").write_text("untracked")
        subprocess.run(["git", "-C", str(self.root), "add", "staged file.txt"], check=True)
        result = CodeIndex(self.store).git()
        self.assertEqual(result["state"], "observed")
        self.assertIsNone(result["commit"])
        self.assertIn("staged file.txt", result["staged_changes"])
        self.assertIn("未追踪.txt", result["untracked_paths"])
        self.assertEqual(result["unstaged_changes"], [])
        self.assertTrue(result["untracked_included"])
        self.assertFalse(result["truncated"])

    def test_impact_depth_reports_actual_omitted_ancestor_but_not_leaf(self):
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       ("grandparent", "caller", "calls", "fixture", "fixture.py", 3))
        limited = CodeIndex(self.store).query("impact", {"id": "target", "depth": 1})
        self.assertEqual(limited["items"], ["caller"])
        self.assertTrue(limited["truncated"])
        self.assertEqual(limited["bounds_hit"], ["depth_limit"])
        complete = CodeIndex(self.store).query("impact", {"id": "target", "depth": 2})
        self.assertEqual(complete["items"], ["caller", "grandparent"])
        self.assertFalse(complete["truncated"])
        self.assertEqual(complete["bounds_hit"], [])

    def test_impact_seen_cycle_does_not_claim_an_omitted_depth_layer(self):
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?)", [
                ("grandparent", "caller", "calls", "fixture", "fixture.py", 3),
                ("target", "grandparent", "calls", "fixture", "fixture.py", 4)])
        result = CodeIndex(self.store).query("impact", {"id": "target", "depth": 2})
        self.assertEqual(result["items"], ["caller", "grandparent"])
        self.assertFalse(result["truncated"])
        self.assertNotIn("target", result["items"])

    def test_impact_item_limit_is_distinct_from_depth_limit(self):
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                       ("sibling", "target", "calls", "fixture", "fixture.py", 3))
        result = CodeIndex(self.store).query("impact", {"id": "target", "depth": 1, "limit": 1})
        self.assertEqual(result["items"], ["caller"])
        self.assertEqual(result["limit"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["bounds_hit"], ["item_limit"])

    def test_impact_wide_frontier_does_not_expand_sql_parameter_payload(self):
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("DELETE FROM relations")
            db.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?)", [
                (f"parent-{i}", "target", "calls", "fixture", "fixture.py", i)
                for i in range(100)])
            db.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?)", [
                (f"ancestor-{i}-{j}", f"parent-{i}", "calls", "fixture", "fixture.py", j)
                for i in range(100) for j in range(100)])
        index = CodeIndex(self.store)
        original_connect = index.connect
        test = self

        class BoundedSQL:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, parameters=()):
                test.assertLessEqual(len(parameters), 512)
                return self.connection.execute(sql, parameters)

            def close(self):
                self.connection.close()

        with patch.object(index, "connect", side_effect=lambda: BoundedSQL(original_connect())):
            result = index.query("impact", {"id": "target", "depth": 2})
        self.assertTrue(result["truncated"])
        self.assertIn("frontier_limit", result["bounds_hit"])
        self.assertIn("item_limit", result["bounds_hit"])

    def original(self, text):
        path = self.root / "main.py"
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("UPDATE files SET sha256=?,bytes=? WHERE path=?",
                       (digest, path.stat().st_size, str(path)))
        return path, digest

    def test_source_context_reads_verified_original_and_rejects_stale_bytes(self):
        text = "\n".join(["# 前置约束", "def caller():", "    return target()", "# 调用边界",
                          "", "def target():", "    # 不自动批准", "    return '中文'", "# 结束"]) + "\n"
        path, digest = self.original(text)
        index = CodeIndex(self.store)
        before = self.store.call("project_status")["sequence"]
        result = index.query("context", {"query": "target", "include_source": "1"})
        self.assertEqual(result["items"][0]["text"], "\n".join(text.splitlines()[5:9]))
        self.assertEqual(result["items"][0]["sha256"], digest)
        self.assertTrue(result["items"][0]["source_verified"])
        self.assertEqual(result["used_bytes"], len(json.dumps(result["items"], ensure_ascii=False).encode()))
        self.assertLessEqual(result["used_bytes"], result["budget_bytes"])
        path.write_text(text.replace("不自动批准", "新修订尚未入索引"), encoding="utf-8")
        stale = index.query("context", {"query": "target", "include_source": True})
        self.assertEqual(stale["items"], [])
        self.assertEqual(stale["skipped"][0]["reason"], "source_sha_mismatch")
        self.assertEqual(self.store.call("project_status")["sequence"], before)

    def test_task_context_keeps_gate_counterevidence_and_exact_seed_neighbor(self):
        self.original("\n".join(["# independent expected source"] * 9))
        self.store.call("task_create", {"id": "TASK", "title": "保留停止点", "body": "不得自动执行", "request_id": "task"})
        self.store.call("human_gate_request", {"id": "GATE", "title": "待用户决定", "body": "不要代答", "task_id": "TASK", "request_id": "gate"})
        counter_file = self.root / "counter.txt"
        counter_file.write_text("反证原件；不改变人工许可", encoding="utf-8")
        self.store.call("node_create", {"id": "COUNTER", "kind": "Evidence", "title": "独立反证", "body": "反证未消失", "source": {"uri": str(counter_file), "locator": "全文"}, "request_id": "counter"})
        self.store.call("node_create", {"id": "CLAIM", "kind": "Claim", "title": "任务所读命题", "body": "不是已成立结论", "data": {"criterion": "必要条件尚未满足", "unknown": True}, "request_id": "claim"})
        self.store.call("relation_add", {"source_id": "TASK", "target_id": "CLAIM", "relation": "reads", "basis": "任务需要判断该命题", "request_id": "task-claim"})
        self.store.call("relation_add", {"source_id": "COUNTER", "target_id": "CLAIM", "relation": "contradicts", "basis": "图外原文反证", "request_id": "counter-edge"})
        before = self.store.call("project_status")["sequence"]
        result = CodeIndex(self.store).query("task_context", {"task_id": "TASK", "symbol_ids": ["target"], "budget_bytes": 16384})
        self.assertEqual(result["project_id"], self.pid)
        self.assertFalse(result["task_context"]["eligible"])
        self.assertEqual(result["task_context"]["human_gates"][0]["id"], "GATE")
        counter = next(x for x in result["required_evidence_refs"] if x["id"] == "COUNTER")
        self.assertEqual(counter["via_object"], "CLAIM")
        self.assertEqual(counter["relation"], "contradicts")
        self.assertEqual(counter["body"], "反证未消失")
        self.assertEqual(counter["data"]["epistemic_status"], "recorded")
        self.assertEqual(counter["edge"]["basis"], "图外原文反证")
        self.assertEqual(counter["edge"]["method"], "declared")
        self.assertIn("原件尚未", counter["reading_status"])
        claim = next(x for x in result["required_evidence_refs"] if x["id"] == "CLAIM")
        self.assertEqual(claim["body"], "不是已成立结论")
        self.assertEqual(claim["data"]["criterion"], "必要条件尚未满足")
        self.assertTrue(claim["data"]["unknown"])
        self.assertEqual(result["code"]["items"][0]["id"], "target")
        self.assertEqual({x["id"] for x in result["code"]["items"]}, {"target", "caller"})
        self.assertTrue(all(x["source_verified"] for x in result["code"]["items"]))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), result["budget_bytes"])
        self.assertEqual(self.store.call("project_status")["sequence"], before)
        with self.assertRaisesRegex(Conflict, "必要任务"):
            CodeIndex(self.store).query("task_context", {"task_id": "TASK", "symbol_ids": ["target"], "budget_bytes": 100})

    def test_task_context_cli_serialization_matches_near_boundary_budget(self):
        import contextlib
        import io
        import pgw
        self.store.call("task_create", {"id": "SERIAL", "title": "准确预算停止点", "body": "只读，不授执行许可", "request_id": "serial-task"})
        self.store.call("human_gate_request", {"id": "SERIAL-GATE", "title": "人工门禁不得删除", "body": "用户未作决定", "task_id": "SERIAL", "request_id": "serial-gate"})
        large = CodeIndex(self.store).query("task_context", {"task_id": "SERIAL", "symbol_ids": ["target"], "budget_bytes": 32768})
        budget = large["used_bytes"] + 16
        self.assertLess(budget, 32768)
        argv = ["pgw.py", "--root", str(self.root), "code", "task_context", json.dumps({"task_id": "SERIAL", "symbol_ids": ["target"], "budget_bytes": budget})]
        before = self.store.call("project_status")["sequence"]
        out = io.StringIO()
        with patch("sys.argv", argv), contextlib.redirect_stdout(out):
            pgw.main()
        raw = out.getvalue().rstrip("\n")
        result = json.loads(raw)
        actual_bytes = len(raw.encode("utf-8"))
        self.assertEqual(actual_bytes, result["used_bytes"], "actual CLI JSON includes formatting, metadata, Chinese and qualifiers")
        self.assertLessEqual(actual_bytes, budget)
        self.assertEqual(result["task_context"]["human_gates"][0]["id"], "SERIAL-GATE")
        self.assertEqual(result["task_context"]["human_gates"][0]["status"], "pending")
        self.assertFalse(result["task_context"]["eligible"])
        self.assertEqual(self.store.call("project_status")["sequence"], before)

    def test_task_context_does_not_drop_large_counterevidence_to_fit_budget(self):
        self.original("\n".join(["# code context"] * 9))
        self.store.call("task_create", {"id": "TASK", "title": "必要反证不能省略", "body": "仅预览", "request_id": "task"})
        self.store.call("node_create", {"id": "CLAIM", "kind": "Claim", "title": "有条件命题", "body": "不能因图关系就视为成立", "data": {"criterion": "条件未通过", "unknown": True}, "request_id": "claim"})
        counter_file = self.root / "counter.txt"
        counter_file.write_text("中文反证条件" * 6000, encoding="utf-8")
        self.store.call("node_create", {"id": "COUNTER", "kind": "Evidence", "source": {"uri": str(counter_file), "locator": "全文"}, "title": "具名必要反证", "body": "中文反证条件" * 6000, "epistemic_status": "unverified", "request_id": "counter"})
        self.store.call("relation_add", {"source_id": "TASK", "target_id": "CLAIM", "relation": "reads", "basis": "必须核该命题", "request_id": "reads"})
        self.store.call("relation_add", {"source_id": "COUNTER", "target_id": "CLAIM", "relation": "contradicts", "basis": "必要反证条件", "method": "declared", "request_id": "counter-edge"})
        before = self.store.call("project_status")["sequence"]
        with self.assertRaisesRegex(Conflict, "证据正文及限定超过预算"):
            CodeIndex(self.store).query("task_context", {"task_id": "TASK", "symbol_ids": ["target"], "budget_bytes": 32768})
        self.assertEqual(self.store.call("project_status")["sequence"], before)

    def test_optional_output_history_does_not_crowd_out_gate_and_counterevidence(self):
        from runtime import ManagedRuntime
        self.original("\n".join(["task code source"] * 9))
        self.store.call("task_create", {"id": "TASK", "title": "必要目标", "body": "不得自动执行", "request_id": "task"})
        self.store.call("task_create", {"id": "DEP", "title": "硬前置", "body": "原前置未通过", "request_id": "dep"})
        self.store.call("relation_add", {"source_id": "TASK", "target_id": "DEP", "relation": "depends_on", "basis": "原硬前置", "expected_version": 1, "request_id": "dep-edge"})
        self.store.call("human_gate_request", {"id": "GATE", "title": "准确人工停止点", "body": "不要代答", "task_id": "TASK", "request_id": "gate"})
        evidence = self.root / "evidence.txt"
        evidence.write_text("实际反证正文", encoding="utf-8")
        self.store.call("node_create", {"id": "CLAIM", "kind": "Claim", "title": "待判断命题", "body": "未知", "request_id": "claim"})
        self.store.call("node_create", {"id": "COUNTER", "kind": "Evidence", "title": "具名反证", "source": {"uri": str(evidence), "locator": "全文"}, "request_id": "counter"})
        self.store.call("relation_add", {"source_id": "TASK", "target_id": "CLAIM", "relation": "reads", "basis": "必要命题", "request_id": "reads"})
        self.store.call("relation_add", {"source_id": "COUNTER", "target_id": "CLAIM", "relation": "contradicts", "basis": "反证原件", "request_id": "counter-edge"})
        for i in range(20):
            self.store.call("artifact_register", {"id": "OUT" + str(i), "title": "可选历史产物", "source": {"uri": str(evidence), "locator": "原历史定位" * 200}, "task_id": "TASK", "request_id": "out" + str(i)})
        before = self.store.call("project_status")["sequence"]
        with self.assertRaisesRegex(Conflict, "16384"):
            ManagedRuntime.context(self.store, "TASK")
        result = CodeIndex(self.store).query("task_context", {"task_id": "TASK", "symbol_ids": ["target"], "budget_bytes": 32768})
        task = result["task_context"]
        self.assertEqual(task["task"]["spec"], "不得自动执行")
        self.assertEqual(task["dependencies"][0]["id"], "DEP")
        self.assertEqual(task["human_gates"][0]["id"], "GATE")
        self.assertEqual(task["source_refs"][0]["id"], "CLAIM")
        self.assertEqual(task["omitted_optional_refs"], 21)  # 20 outputs + gate duplicate locator; gate facts retained above
        self.assertIn("produces/related_to", task["reference_policy"])
        self.assertEqual({r["id"] for r in result["required_evidence_refs"]}, {"CLAIM", "COUNTER"})
        self.assertEqual(next(r for r in result["required_evidence_refs"] if r["id"] == "COUNTER")["relation"], "contradicts")
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 32768)
        self.assertEqual(self.store.call("project_status")["sequence"], before)
        with self.assertRaisesRegex(Conflict, "16384"):
            ManagedRuntime.context(self.store, "TASK")

    def test_task_context_rejects_foreign_seed_and_exposes_stale_source(self):
        path, _ = self.original("\n".join(["original source"] * 9))
        self.store.call("task_create", {"id": "TASK", "title": "读取任务", "body": "只读", "request_id": "task"})
        index = CodeIndex(self.store)
        with self.assertRaisesRegex(Conflict, "此项目缓存"):
            index.query("task_context", {"task_id": "TASK", "symbol_ids": ["foreign"]})
        with self.assertRaisesRegex(Conflict, "1–10"):
            index.query("task_context", {"task_id": "TASK", "symbol_ids": ["target", "target"]})
        path.write_text("changed source", encoding="utf-8")
        result = index.query("task_context", {"task_id": "TASK", "symbol_ids": ["target"]})
        self.assertEqual(result["code"]["items"], [])
        self.assertEqual(result["code"]["omitted_by_reason"]["source_sha_mismatch"], 2)

    def test_source_budget_bounds_chinese_serialized_items_and_exposes_omission(self):
        self.original("\n".join(["中文注释" * 30] * 9))
        r = CodeIndex(self.store).query("context", {"query": "target", "include_source": True,
                                                    "budget_bytes": 500})
        self.assertLessEqual(r["used_bytes"], 500)
        self.assertIn("budget_limit", r["bounds_hit"])
        self.assertTrue(r["truncated"])
        self.assertEqual(r["items"], [])
        self.assertEqual(r["skipped"][0]["reason"], "budget_limit")

    def test_source_context_refuses_symlink_escape_and_large_original(self):
        path, _ = self.original("\n".join(["original"] * 9))
        outside = self.root.parent / "other-project.py"
        outside.write_text("private", encoding="utf-8")
        path.unlink(); path.symlink_to(outside)
        escaped = CodeIndex(self.store).query("context", {"query": "target", "include_source": True})
        self.assertEqual(escaped["items"], [])
        self.assertEqual(escaped["source_read_bytes"], 0)
        self.assertEqual(escaped["skipped"][0]["reason"], "outside_source_root")
        path.unlink(); self.original("x" * 262145)
        large = CodeIndex(self.store).query("context", {"query": "target", "include_source": True})
        self.assertEqual(large["source_read_bytes"], 0)
        self.assertIn("source_read_limit", large["bounds_hit"])

    def test_source_context_file_and_line_caps_are_visible(self):
        with sqlite3.connect(self.cache / "test.sqlite") as db:
            db.execute("DELETE FROM symbols")
            for i in range(6):
                path = self.root / f"code-{i}.py"
                raw = ("中文\n" * 50).encode()
                path.write_bytes(raw)
                db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)",
                           (str(path), "python", hashlib.sha256(raw).hexdigest(), len(raw), "parsed", "[]"))
                db.execute("INSERT INTO symbols VALUES (?,?,?,?,?,?)",
                           (f"candidate{i}", str(path), "function", "candidate", 1, 50))
        r = CodeIndex(self.store).query("context", {"query": "candidate", "include_source": True,
                                                   "budget_bytes": 32768})
        self.assertEqual(len(r["items"]), 5)
        self.assertIn("source_file_limit", r["bounds_hit"])
        self.assertIn("source_line_limit", r["bounds_hit"])
        self.assertTrue(all(n["source_end_line"] == 40 and n["source_truncated"] for n in r["items"]))
        self.assertEqual(r["skipped"][0]["reason"], "source_file_limit")


if __name__ == "__main__":
    unittest.main()
