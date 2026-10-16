"""Local project state. JSONL is authoritative; SQLite is a replayable read model."""
from __future__ import annotations
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

KINDS = "PlanItem Goal Requirement Phase Task Subtask Idea Question Hypothesis Evidence Claim Decision File Dataset Artifact Concept Blocker Validation HumanGate Agent Thread Run Worktree Commit CodeSymbol Module".split()
# Bounds apply before full JSON loading or write copies; originals are never truncated.
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_STATE_NODES = 20000
MAX_STATE_EDGES = 40000
MAX_EVENT_BYTES = 1024 * 1024

TASKS = {"Task", "Subtask", "PlanItem"}
READS = {"project_open", "project_status", "graph_get", "graph_search", "graph_neighbors", "current_frontier", "impact_analysis", "timeline"}
CREATORS = {"task_create": "Task", "idea_capture": "Idea", "hypothesis_create": "Hypothesis", "evidence_add": "Evidence", "decision_record": "Decision", "artifact_register": "Artifact", "thread_link": "Thread", "worktree_link": "Worktree", "commit_link": "Commit", "human_gate_request": "HumanGate"}
RELATIONS = set("contains depends_on requires_validation executed_by produces derived_from reads writes supports contradicts insufficient_for validates supersedes compares_with promoted_to worktree modifies committed_as related_to".split())

class Conflict(ValueError):
    pass

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()

def file_hash(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise Conflict(f"原件不存在：{p}")
    if p.stat().st_size > 64 * 1024 * 1024:
        raise Conflict("大文件须先按重负载规则处理；当前轻量服务最多核对 64 MiB")
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()

def need(a, *keys):
    for k in keys:
        if k not in a or a[k] is None or a[k] == "":
            raise Conflict(f"缺少字段：{k}")

class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.folder = self.root / ".project-graph"
        self.log = self.folder / "events.jsonl"
        self.db = self.folder / "project.db"

    def init(self, name, goal):
        if not self.root.is_dir():
            raise Conflict("项目目录必须先存在")
        self.folder.mkdir(mode=0o700, exist_ok=True)
        with self.locked(require=False) as db:
            if self._nodes(db):
                raise Conflict("项目已初始化，不覆盖现有状态")
            project = {"id": "project", "kind": "Project", "title": name, "body": goal,
                       "status": "active", "version": 1, "work_revision": 1,
                       "created_at": now(), "updated_at": now(),
                       "data": {"project_id": str(uuid.uuid4()), "root": str(self.root), "schema_version": 1}}
            self._append(db, "project_init", {"name": name, "goal": goal}, "init", "local-user", [project], [], [], project)
            self._manifest(project)
        return project

    def _manifest(self, project):
        dest = self.folder / "project.json"
        if not dest.exists():
            temp = dest.with_suffix(".tmp")
            temp.write_text(json.dumps({**project["data"], "name": project["title"], "state": "events.jsonl", "projection": "project.db"}, ensure_ascii=False, indent=2))
            os.replace(temp, dest)

    @contextmanager
    def locked(self, require=True):
        if not self.folder.is_dir():
            raise Conflict("项目尚未接入；先在已授权的目录执行 init")
        with (self.folder / "write.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db = sqlite3.connect(self.db, timeout=20)
            try:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, request_id TEXT UNIQUE, args_hash TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                """)
                self._replay(db)
                if require and not db.execute("SELECT 1 FROM nodes WHERE id='project'").fetchone():
                    raise Conflict("缺少项目初始化事件")
                yield db
            finally:
                db.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _replay(self, db):
        row = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        offset = int(row[0]) if row else 0
        last = db.execute("SELECT data FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = json.loads(last[0]) if last else None
        if not self.log.exists():
            if offset:
                raise Conflict("权威日志丢失，拒绝从孤立数据库继续")
            return
        if self.log.stat().st_size < offset:
            raise Conflict("日志被截短；保留原件并恢复，不继续写入")
        with self.log.open("rb") as f:
            f.seek(offset)
            while True:
                line = f.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT_BYTES:
                    raise Conflict("单日志事件超过容量上限；原件保留，请在 Windows 核对，不自动截断")
                if not line.endswith(b"\n"):
                    raise Conflict("日志尾部不完整；请保留备份后人工恢复，未自动删除")
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise Conflict("事件日志损坏") from exc
                clean = {k: v for k, v in event.items() if k != "hash"}
                if event.get("hash") != digest(clean) or event.get("prev") != (previous["hash"] if previous else "") or event.get("seq") != (previous["seq"] + 1 if previous else 1):
                    raise Conflict("事件序号或哈希链不匹配")
                offset += len(line)
                self._apply(db, event, offset)
                previous = event
        if require_project := db.execute("SELECT data FROM nodes WHERE id='project'").fetchone():
            self._manifest(json.loads(require_project[0]))

    def _apply(self, db, e, offset):
        self._guard_projection(db, e["nodes"], e["edges"], e.get("remove_edges", []))
        with db:
            for n in e["nodes"]:
                db.execute("INSERT OR REPLACE INTO nodes VALUES(?,?)", (n["id"], encode(n)))
            for edge_id in e.get("remove_edges", []):
                db.execute("DELETE FROM edges WHERE id=?", (edge_id,))
            for edge in e["edges"]:
                db.execute("INSERT OR REPLACE INTO edges VALUES(?,?)", (edge["id"], encode(edge)))
            db.execute("INSERT INTO events VALUES(?,?,?,?)", (e["seq"], e["request_id"], e["args_hash"], encode(e)))
            db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(offset),))

    def _append(self, db, op, args, request_id, actor, nodes, edges, remove_edges, result):
        self._guard_projection(db, nodes, edges, remove_edges)
        last = db.execute("SELECT data FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = json.loads(last[0]) if last else None
        e = {"seq": previous["seq"] + 1 if previous else 1, "prev": previous["hash"] if previous else "",
             "request_id": request_id, "args_hash": digest({"op": op, "args": args}), "at": now(),
             "actor": actor, "op": op, "nodes": nodes, "edges": edges, "remove_edges": remove_edges, "result": result}
        e["hash"] = digest(e)
        record = (encode(e) + "\n").encode()
        if len(record) > MAX_EVENT_BYTES:
            raise Conflict("单事件超过 1 MiB，请缩小输入并引用原件")
        with self.log.open("ab") as f:
            f.write(record)
            f.flush()
            os.fsync(f.fileno())
            offset = f.tell()
        # If projection fails after fsync, the next request replays this event.
        self._apply(db, e, offset)
        return result

    def _guard_projection(self, db, nodes=(), edges=(), removed=()):
        """Measure stored UTF8 bytes in SQLite before JSON decoding or copying.

        Prospective changed rows are measured before authoritative append/replay.
        This is a fail-closed working-set limit, never a claim about all RAM use.
        """
        counts, size = {}, 0
        for table, changes in (("nodes", nodes), ("edges", edges)):
            count, raw_bytes = db.execute(
                f"SELECT count(*),coalesce(sum(length(CAST(data AS BLOB))),0) FROM {table}"
            ).fetchone()
            for ident, value in {n["id"]: n for n in changes}.items():
                old = db.execute(f"SELECT length(CAST(data AS BLOB)) FROM {table} WHERE id=?", (ident,)).fetchone()
                raw_bytes += len(encode(value).encode("utf-8")) - (old[0] if old else 0)
                count += 0 if old else 1
            if table == "edges":
                for ident in set(removed):
                    old = db.execute("SELECT length(CAST(data AS BLOB)) FROM edges WHERE id=?", (ident,)).fetchone()
                    if old:
                        raw_bytes -= old[0]
                        count -= 1
            counts[table] = count
            size += raw_bytes
        if size > MAX_STATE_BYTES or counts["nodes"] > MAX_STATE_NODES or counts["edges"] > MAX_STATE_EDGES:
            raise Conflict(
                f"任务图容量超限：{size} UTF8字节/{counts['nodes']}节点/{counts['edges']}边；"
                f"上限{MAX_STATE_BYTES}字节/{MAX_STATE_NODES}节点/{MAX_STATE_EDGES}边。"
                "本次拒绝当前操作且未追加新事件，原记录保留；请在 Windows 核对并制定有界迁移，不自动删减。"
            )

    def _nodes(self, db):
        self._guard_projection(db)
        return {r[0]: json.loads(r[1]) for r in db.execute("SELECT id,data FROM nodes ORDER BY json_extract(data,'$.created_at'),id")}

    def _edges(self, db):
        self._guard_projection(db)
        return {r[0]: json.loads(r[1]) for r in db.execute("SELECT id,data FROM edges")}

    def _node(self, nodes, id):
        if id not in nodes:
            raise Conflict(f"节点不存在：{id}")
        return nodes[id]

    def _task(self, nodes, id):
        n = self._node(nodes, id)
        if n["kind"] not in TASKS:
            raise Conflict("目标不是任务")
        return n

    def _version(self, node, args):
        need(args, "expected_version")
        if type(args["expected_version"]) is not int or node["version"] != args["expected_version"]:
            raise Conflict(f"版本已变化：当前 {node['version']}；请重读后处理，不覆盖")

    def _touch(self, n, work=False):
        n["version"] += 1
        n["updated_at"] = now()
        if work:
            n["work_revision"] += 1

    def _source(self, source):
        if not isinstance(source, dict):
            raise Conflict("source 必须是包含 uri 与 locator 的对象")
        need(source, "uri", "locator")
        s = copy.deepcopy(source)
        uri = s["uri"]
        if not isinstance(uri, str):
            raise Conflict("source.uri 必须是字符串")
        if uri.startswith("/"):
            s["uri"] = str(Path(uri).resolve())
            s["sha256"] = file_hash(uri)
        elif not uri.startswith(("https://", "http://", "codex://", "chatgpt-conversation://")):
            raise Conflict("来源须为绝对文件路径或可定位 URL")
        s["observed_at"] = s.get("observed_at") or now()
        return s

    def _edge(self, nodes, edges, src, rel, dst, basis, method="declared"):
        if rel not in RELATIONS:
            raise Conflict("未知关系类型")
        self._node(nodes, src); self._node(nodes, dst)
        if not basis:
            raise Conflict("关系需要依据 basis")
        if rel == "depends_on":
            if nodes[src]["kind"] not in TASKS | {"HumanGate"} or nodes[dst]["kind"] not in TASKS | {"HumanGate"}:
                raise Conflict("依赖端点须为任务、计划项或人工门禁")
            frontier, seen = [dst], set()
            while frontier:
                v = frontier.pop()
                if v == src:
                    raise Conflict("依赖会产生循环")
                if v in seen:
                    continue
                seen.add(v)
                frontier.extend(e["target"] for e in edges.values() if e["source"] == v and e["relation"] == rel)
        if rel in {"supports", "contradicts", "insufficient_for"}:
            if nodes[src]["kind"] != "Evidence" or nodes[dst]["kind"] not in {"Claim", "Hypothesis"}:
                raise Conflict("证据关系须 Evidence → Claim/Hypothesis")
        id = "edge-" + digest([src, rel, dst])[:20]
        if id in edges:
            raise Conflict("关系已存在；更改依据请先记录决定，再撤回旧关系")
        e = {"id": id, "source": src, "relation": rel, "target": dst, "basis": basis, "method": method, "created_at": now()}
        edges[id] = e
        return e

    def _input_observation(self, roots, nodes, edges):
        allowed = {"Evidence", "Claim", "Hypothesis", "Decision", "File", "Dataset", "Artifact", "Concept", "Goal", "Requirement", "Idea", "Question", "Module", "CodeSymbol"}
        roots = sorted(roots)
        for ident in roots:
            if self._node(nodes, ident)["kind"] not in allowed:
                raise Conflict("验证输入须为知识或来源对象；任务、门禁和回执使用原生命周期")
        pool, bounds = [], set()
        for count, edge in enumerate(sorted(edges.values(), key=lambda e: e["id"])):
            if count >= 6000:
                bounds.add("edge_scan_limit"); break
            if edge["relation"] in {"reads", "derived_from", "supports", "contradicts", "insufficient_for"}:
                if len(pool) >= 1000:
                    bounds.add("relation_limit"); continue
                pool.append(edge)
        def arc(e):
            return (e["source"], e["target"]) if e["relation"] in {"reads", "derived_from"} else (e["target"], e["source"])
        visited, frontier = set(roots), set(roots)
        for _ in range(6):
            new = set()
            for e in pool:
                source, target = arc(e)
                if source in frontier and target not in visited and target not in new and nodes[target]["kind"] in allowed:
                    if len(visited) + len(new) >= 128:
                        bounds.add("node_limit"); continue
                    new.add(target)
            visited |= new; frontier = new
            if not frontier:
                break
        if any(source in frontier and target not in visited and nodes[target]["kind"] in allowed for source, target in map(arc, pool)):
            bounds.add("depth_limit")
        relations = [{k: e.get(k) for k in ("id", "source", "relation", "target", "basis", "method")} for e in pool if e["source"] in visited and e["target"] in visited]
        return {"schema": 1, "roots": roots, "revisions": {ident: nodes[ident]["work_revision"] for ident in sorted(visited)}, "relation_sha256": digest(relations), "bounds_hit": sorted(bounds), "scope": "显式已用输入及其登记来源/支持/反证，图外未知；不是语义资格或人工批准"}

    def _validation_reasons(self, n, nodes, edges):
        reasons = []
        for check in n["data"].get("checks", ["technical"]):
            candidates = [v for v in nodes.values() if v["kind"] == "Validation" and v["data"].get("task_id") == n["id"] and v["data"].get("check") == check]
            if not candidates:
                reasons.append(f"缺少验证：{check}"); continue
            v = max(candidates, key=lambda x: x["data"]["sequence"])["data"]
            if v["result"] != "PASS" or v["task_revision"] != n["work_revision"]:
                reasons.append(f"验证未通过或已过期：{check}"); continue
            if "input_binding" in v:
                binding = v["input_binding"]
                try:
                    observed = self._input_observation(binding["roots"], nodes, edges)
                    if binding != observed or observed["bounds_hit"]:
                        reasons.append(f"限定输入版本/来源关系已变或有界未知：{check}")
                except (Conflict, KeyError) as error:
                    reasons.append(f"限定输入待核对：{check} / {error}")
            current_deps = {e["target"]: nodes[e["target"]]["work_revision"] for e in edges.values() if e["source"] == n["id"] and e["relation"] == "depends_on"}
            if v.get("dependency_revisions") != current_deps:
                reasons.append(f"上游版本已变：{check}")
            for s in [v["source"], *v.get("files", [])]:
                if s["uri"].startswith("/"):
                    try:
                        if file_hash(s["uri"]) != s.get("sha256"):
                            reasons.append(f"被测文件已变：{s['uri']}")
                    except Conflict as e:
                        reasons.append(str(e))
        return reasons

    def frontier(self, nodes, edges):
        cache = {}
        def classify(n, visiting):
            id = n["id"]
            if id in cache:
                return cache[id]
            if id in visiting:
                return {"bucket": "Blocked", "reasons": ["依赖循环"]}
            visiting = visiting | {id}
            deps = [e["target"] for e in edges.values() if e["source"] == id and e["relation"] == "depends_on"]
            reasons = [f"等待 {d}" for d in deps if d not in nodes or classify(nodes[d], visiting)["bucket"] != "Completed"]
            gates = [g for g in nodes.values() if g["kind"] == "HumanGate" and (g["data"].get("task_id") == id or g["id"] in deps) and g["status"] != "approved"]
            if n["kind"] == "HumanGate":
                out = {"bucket": "Completed" if n["status"] == "approved" and not reasons else "Waiting Human", "reasons": reasons + ([] if n["status"] == "approved" else [n["title"]])}
            elif n["status"] in {"deferred", "cancelled"}:
                out = {"bucket": "Deferred", "reasons": [n["data"].get("blocker") or n["body"]]}
            elif gates:
                out = {"bucket": "Waiting Human", "reasons": [g["title"] for g in gates] + reasons}
            elif n["status"] == "blocked" or reasons:
                out = {"bucket": "Blocked", "reasons": ([n["data"].get("blocker", "手动阻塞")] if n["status"] == "blocked" else []) + reasons}
            elif n["status"] == "completed":
                stale = self._validation_reasons(n, nodes, edges)
                out = {"bucket": "Pending Validation" if stale else "Completed", "reasons": stale}
            else:
                out = {"bucket": {"in_progress": "In Progress", "pending_validation": "Pending Validation"}.get(n["status"], "Ready"), "reasons": []}
            cache[id] = out
            return out
        return [{"id": n["id"], "title": n["title"], "status": n["status"], "version": n["version"], "owner": n["data"].get("owner"), **classify(n, set())} for n in nodes.values() if n["kind"] in TASKS]

    def call(self, op, args=None, channel="mcp"):
        args = copy.deepcopy(args or {})
        if not isinstance(args, dict):
            raise Conflict("参数必须是对象")
        with self.locked() as db:
            nodes, edges = self._nodes(db), self._edges(db)
            if op in READS:
                return self._read(op, args, db, nodes, edges)
            request_id = args.get("request_id")
            need(args, "request_id")
            old = db.execute("SELECT args_hash,data FROM events WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old[0] != digest({"op": op, "args": args}):
                    raise Conflict("同一 request_id 不能用于不同参数")
                return json.loads(old[1])["result"]
            if op == "history_import":
                manifest = args.get("manifest")
                if isinstance(manifest, dict) and "expected_sequence" in manifest:
                    expected = manifest["expected_sequence"]
                    if type(expected) is not int or expected < 0:
                        raise Conflict("导入预览事件序号必须为非负整数")
                    already = any(n["data"].get("import_batch") == manifest.get("batch_id")
                                  for n in nodes.values())
                    current = db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
                    if not already and current != expected:
                        raise Conflict("预览后项目事件已变；重新核对，不采用旧预览")
            before_nodes, before_edges = copy.deepcopy(nodes), copy.deepcopy(edges)
            actor = args.get("actor") or ("local-user" if channel == "ui" else "codex")
            result = self._mutate(op, args, nodes, edges, actor, channel)
            changed = [n for k, n in nodes.items() if before_nodes.get(k) != n]
            changed_edges = [e for k, e in edges.items() if before_edges.get(k) != e]
            removed = [k for k in before_edges if k not in edges]
            if op == "history_import" and not changed and not changed_edges and not removed:
                return result
            return self._append(db, op, args, request_id, actor, changed, changed_edges, removed, result)

    def _impact(self, a, nodes, edges, seq):
        need(a, "id"); self._node(nodes, a["id"])
        depth = min(max(int(a.get("depth", 2)), 1), 6)
        limit = min(max(int(a.get("limit", 200)), 1), 500)
        offset = max(int(a.get("offset", 0)), 0)
        directions = {"depends_on": "reverse", "derived_from": "reverse", "reads": "reverse", "requires_validation": "reverse", "validates": "both", "supports": "forward", "contradicts": "forward", "insufficient_for": "forward", "produces": "forward", "writes": "forward", "committed_as": "forward"}
        relevant = set(a.get("relations") or directions)
        if relevant - RELATIONS:
            raise Conflict("未知影响关系")
        bounds, pool, scanned = set(), [], 0
        for e in edges.values():
            if scanned >= 6000:
                bounds.add("edge_scan_limit"); break
            scanned += 1
            if e["relation"] in relevant:
                if len(pool) >= 1000:
                    bounds.add("relation_limit"); break
                pool.append(e)
        pool.sort(key=lambda e: e["id"])
        visited, frontier, parents = {a["id"]}, {a["id"]}, {}
        def arcs(e):
            direction = directions.get(e["relation"], "reverse")
            forward, reverse = (e["source"], e["target"]), (e["target"], e["source"])
            return [forward, reverse] if direction == "both" else [forward if direction == "forward" else reverse]
        for _ in range(depth):
            new = set()
            for e in pool:
                for source, target in arcs(e):
                    if source in frontier and target not in visited and target not in new:
                        if len(visited) + len(new) >= 500:
                            bounds.add("node_limit"); continue
                        new.add(target); parents[target] = (source, e)
            visited |= new; frontier = new
            if not frontier:
                break
        if any(source in frontier and target not in visited for e in pool for source, target in arcs(e)):
            bounds.add("depth_limit")
        observed = [n for n in nodes.values() if n["id"] in visited]
        selected = observed[offset:offset + limit]
        ids = {n["id"] for n in selected}
        paths, path_ids = {}, {a["id"]}
        for n in selected:
            trail, target = [], n["id"]
            while target in parents:
                source, e = parents[target]
                trail.append({**e, "from": source, "to": target, "influence_direction": "forward" if source == e["source"] else "reverse"})
                path_ids.update((source, target)); target = source
            paths[n["id"]] = list(reversed(trail))
        return {"nodes": selected, "edges": [e for e in pool if e["source"] in ids and e["target"] in ids], "total": len(observed), "total_observed": len(observed), "total_known_within_depth": not bounds, "offset": offset, "next_offset": offset + limit if offset + limit < len(observed) else None, "truncated": bool(bounds) or offset > 0 or offset + limit < len(observed), "sequence": seq, "project_id": nodes["project"]["data"]["project_id"], "depth": depth, "bounds_hit": sorted(bounds), "edge_scan_count": scanned, "relation_directions": {r: directions.get(r, "reverse") for r in sorted(relevant)}, "legacy_reverse_relations": sorted(relevant - directions.keys()), "paths": paths, "path_nodes": [n for n in nodes.values() if n["id"] in path_ids], "scope": "已登记来源/论据/依赖/产物关系的有界影响候选；方向按关系语义，单次访问路径不是原件资格或状态批准；图外关系未知"}

    def _read(self, op, a, db, nodes, edges):
        seq = db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
        if op in {"project_open", "project_status"}:
            counts = {}
            for n in nodes.values():
                counts[n["kind"]] = counts.get(n["kind"], 0) + 1
            return {"project": nodes["project"], "sequence": seq, "counts": counts, "frontier": self.frontier(nodes, edges), "storage": str(self.folder), "thread_status": "reported observations; not live runtime"}
        if op == "current_frontier":
            return {"tasks": self.frontier(nodes, edges), "sequence": seq}
        if op == "timeline":
            limit = min(max(int(a.get("limit", 50)), 1), 200)
            before = int(a.get("before", seq + 1))
            events = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE seq < ? ORDER BY seq DESC LIMIT ?", (before, limit))]
            return {"events": [{k: e[k] for k in ("seq", "at", "actor", "op", "nodes", "edges", "remove_edges")} for e in events], "next_before": events[-1]["seq"] if events else None}
        if op == "graph_get" and a.get("id"):
            n = self._node(nodes, a["id"])
            return {"node": n, "edges": [e for e in edges.values() if n["id"] in (e["source"], e["target"])], "sequence": seq}
        if op == "impact_analysis":
            return self._impact(a, nodes, edges, seq)
        limit = min(max(int(a.get("limit", 200)), 1), 500)
        selected = list(nodes.values())
        if op in {"graph_get", "graph_search"}:
            if a.get("kind"):
                selected = [n for n in selected if n["kind"] == a["kind"]]
            if a.get("query"):
                q = a["query"].casefold()
                selected = [n for n in selected if q in encode(n).casefold()]
        elif op in {"graph_neighbors", "impact_analysis"}:
            need(a, "id"); self._node(nodes, a["id"])
            depth = min(max(int(a.get("depth", 2)), 1), 6)
            visited, frontier = {a["id"]}, {a["id"]}
            relevant = set(a.get("relations") or (["depends_on", "derived_from", "reads"] if op == "impact_analysis" else RELATIONS))
            for _ in range(depth):
                new = set()
                for e in edges.values():
                    if e["relation"] not in relevant:
                        continue
                    if e["target"] in frontier:
                        new.add(e["source"])
                    if op != "impact_analysis" and e["source"] in frontier:
                        new.add(e["target"])
                frontier = new - visited
                visited |= frontier
            selected = [n for n in nodes.values() if n["id"] in visited]
        else:
            raise Conflict("未知查询")
        total = len(selected)
        offset = max(int(a.get("offset", 0)), 0)
        selected = selected[offset:offset + limit]
        ids = {n["id"] for n in selected}
        return {"nodes": selected, "edges": [e for e in edges.values() if e["source"] in ids and e["target"] in ids], "total": total, "offset": offset, "next_offset": offset + limit if offset + limit < total else None, "truncated": offset + limit < total or offset > 0, "sequence": seq, "scope": "已登记关系；邻域/影响为指定深度内的候选，不保证图外关系完整"}

    def _create(self, kind, a, nodes, edges, actor):
        if kind not in KINDS or kind == "Validation":
            raise Conflict("未知或保留节点类型")
        need(a, "title")
        if not isinstance(a["title"], str) or not a["title"].strip() or len(a["title"]) > 300:
            raise Conflict("标题须为 1–300 字符")
        id = a.get("id") or kind.lower() + "-" + uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", id) or id in nodes:
            raise Conflict("ID 无效或已存在")
        data = copy.deepcopy(a.get("data") or {})
        if not isinstance(data, dict):
            raise Conflict("data 必须是对象")
        status = "open"
        if kind in TASKS:
            need(a, "body")
            data = {"spec": a["body"], "checks": a.get("checks") or ["technical"], "owner": None, "blocker": None}
            if not isinstance(data["checks"], list) or not all(isinstance(c, str) and c for c in data["checks"]):
                raise Conflict("checks 必须是检查名称数组")
            status = "todo"
        if kind in {"Evidence", "Artifact", "File", "Dataset"}:
            data["source"] = self._source(a.get("source"))
            data["epistemic_status"] = a.get("epistemic_status", "recorded")
        if kind == "Decision":
            need(a, "body", "alternatives", "evidence_ids")
            if not a["evidence_ids"]:
                raise Conflict("决定需至少一个依据节点")
            for ref in a["evidence_ids"]:
                self._node(nodes, ref)
            data.update({"alternatives": a["alternatives"], "evidence_ids": a["evidence_ids"], "revisit_when": a.get("revisit_when", "新证据或范围变化")})
        if kind == "Thread":
            need(a, "thread_id", "task_id")
            self._task(nodes, a["task_id"])
            existing = [n for n in nodes.values() if n["kind"] == "Thread" and n["data"].get("thread_id") == a["thread_id"]]
            if existing:
                raise Conflict("聊天已登记；使用 relation_add 关联其他任务，node_update 更新观察")
            data.update({"thread_id": a["thread_id"], "project_id": nodes["project"]["data"]["project_id"], "task_id": a["task_id"], "goal": a.get("body", ""), "agent": a.get("agent", "unknown"), "status": a.get("status", "unknown"), "started_at": a.get("started_at"), "ended_at": None, "worktree": None, "files_read": [], "files_changed": [], "artifacts": [], "evidence_added": [], "decisions": [], "summary": a.get("body", ""), "observed_at": now(), "observation_source": a.get("observation_source", "reported"), "runtime_connected": False})
        if kind == "Worktree":
            need(a, "path"); data["path"] = str(Path(a["path"]).expanduser().resolve())
            if not Path(data["path"]).is_dir():
                raise Conflict("工作树路径不存在；本工具不创建工作树")
        if kind == "Commit":
            need(a, "sha", "path"); data.update({"sha": a["sha"], "path": a["path"], "verification": "reported; verify with git"})
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", a["sha"]):
                raise Conflict("Commit 需要完整 SHA")
        if kind == "HumanGate":
            need(a, "task_id", "body"); self._task(nodes, a["task_id"])
            data = {"task_id": a["task_id"], "request_basis": a["body"]}; status = "pending"
        n = {"id": id, "kind": kind, "title": a["title"].strip(), "body": a.get("body", ""), "status": status, "version": 1, "work_revision": 1, "created_at": now(), "updated_at": now(), "data": data}
        nodes[id] = n
        self._edge(nodes, edges, "project", "contains", id, "登记到当前项目")
        for dep in a.get("depends_on", []):
            self._edge(nodes, edges, id, "depends_on", dep, "创建任务时声明的前置")
        if a.get("task_id"):
            task = self._task(nodes, a["task_id"])
            relation = "executed_by" if kind == "Thread" else "produces" if kind in {"Artifact", "Evidence"} else "related_to"
            self._edge(nodes, edges, task["id"], relation, id, "创建对象时关联任务")
        for ref in data.get("evidence_ids", []):
            self._edge(nodes, edges, id, "derived_from", ref, "决定的可定位依据")
        return n

    def _mutate(self, op, a, nodes, edges, actor, channel):
        if op == "history_import":
            return self._import_history(a, nodes, edges)
        if op in CREATORS or op == "node_create":
            kind = CREATORS.get(op) or a.get("kind")
            if kind in {"HumanGate", "Thread", "Commit", "Worktree"} and op == "node_create":
                raise Conflict("请使用对应专用工具")
            return self._create(kind, a, nodes, edges, actor)
        if op in {"relation_add", "relation_remove"}:
            need(a, "basis")
            if op == "relation_remove":
                need(a, "id")
                if a["id"] not in edges:
                    raise Conflict("关系不存在")
                edge = edges[a["id"]]
            else:
                need(a, "source_id", "target_id", "relation")
                edge = {"source": a["source_id"], "target": a["target_id"], "relation": a["relation"]}
            src = self._node(nodes, edge["source"])
            if edge["relation"] == "depends_on":
                self._version(src, a)
                if src["status"] == "in_progress" and src["data"].get("owner") != actor:
                    raise Conflict("只有领取者可修改正在执行任务的依赖")
                self._touch(src, work=True)
            if op == "relation_remove":
                return edges.pop(a["id"])
            return self._edge(nodes, edges, a["source_id"], a["relation"], a["target_id"], a["basis"], a.get("method", "declared"))
        need(a, "id")
        n = self._node(nodes, a["id"])
        self._version(n, a)
        if op == "idea_promote":
            if n["kind"] not in {"Idea", "Hypothesis", "Question"} or a.get("kind") not in {"Task", "Hypothesis"}:
                raise Conflict("仅想法/假设/问题可转为任务或假设")
            child_args = {k: v for k, v in a.items() if k not in {"id", "expected_version"}}
            child_args.setdefault("title", n["title"])
            child_args.setdefault("body", n["body"])
            child = self._create(a["kind"], child_args, nodes, edges, actor)
            self._edge(nodes, edges, n["id"], "promoted_to", child["id"], a.get("body") or "保留原想法并创建后续工作")
            n["status"] = "promoted"; self._touch(n)
            return child
        if op == "human_gate_resolve":
            if channel != "ui" or n["kind"] != "HumanGate":
                raise Conflict("仅用户工作区界面可登记人工裁决；MCP/CLI 不可批准")
            need(a, "decision", "body")
            if a["decision"] not in {"approved", "rejected"}:
                raise Conflict("裁决须 approved/rejected")
            n["status"] = a["decision"]; n["data"].update({"resolution": a["body"], "resolved_at": now(), "resolved_by": "local-ui-user"})
            self._touch(n); return n
        if op in {"node_update", "claim_update"}:
            if n["kind"] in TASKS | {"Project", "HumanGate", "Validation"}:
                raise Conflict("此对象必须使用专用生命周期工具")
            if op == "claim_update" and n["kind"] not in {"Claim", "Hypothesis"}:
                raise Conflict("目标不是 Claim/Hypothesis")
            for k in ("title", "body"):
                if k in a:
                    n[k] = a[k]
            if "data" in a:
                if n["kind"] == "Thread" and set(a["data"]) - {"status", "ended_at", "worktree", "files_read", "files_changed", "artifacts", "evidence_added", "decisions", "summary", "observed_at", "observation_source"}:
                    raise Conflict("聊天身份字段不可更改")
                n["data"].update(a["data"])
            if "source" in a:
                n["data"]["source"] = self._source(a["source"])
            if "status" in a:
                if n["kind"] in {"Claim", "Hypothesis"} and a["status"] in {"supported", "contradicted"}:
                    rel = "supports" if a["status"] == "supported" else "contradicts"
                    if not any(e["target"] == n["id"] and e["relation"] == rel for e in edges.values()):
                        raise Conflict("结论状态必须关联相应证据关系")
                n["status"] = a["status"]
            self._touch(n, work=True); return n
        self._task(nodes, n["id"])
        bucket = next(t for t in self.frontier(nodes, edges) if t["id"] == n["id"])
        if op == "task_claim":
            if bucket["bucket"] != "Ready":
                raise Conflict("此任务当前不可领取：" + bucket["bucket"] + " / ".join(bucket["reasons"]))
            n["status"] = "in_progress"; n["data"]["owner"] = actor; self._touch(n); return n
        if op in {"task_progress", "task_block", "task_complete", "validation_record"}:
            if n["data"].get("owner") not in {None, actor} and channel != "ui":
                raise Conflict("任务由其他执行者领取；请由其更新或在用户界面处理")
        if op == "task_progress":
            need(a, "body")
            status = a.get("status", "in_progress")
            if status not in {"todo", "in_progress", "pending_validation", "deferred", "cancelled"}:
                raise Conflict("进度不能绕过完成或阻塞工具")
            if status == "in_progress" and bucket["bucket"] in {"Blocked", "Waiting Human", "Deferred"}:
                raise Conflict("先解除阻塞/人工门禁，恢复为 todo 后重新领取")
            n["body"] = a["body"]; n["status"] = status
            n["data"]["blocker"] = None
            n["data"]["owner"] = actor if status == "in_progress" else None
            self._touch(n, work=True); return n
        if op == "task_block":
            need(a, "body"); n["status"] = "blocked"; n["data"]["blocker"] = a["body"]
            self._touch(n, work=True); return n
        if op == "validation_record":
            need(a, "result", "check", "body", "source")
            if a["result"] not in {"PASS", "FAIL", "UNKNOWN"}:
                raise Conflict("验证结果须 PASS/FAIL/UNKNOWN")
            sources = [self._source(s) for s in a.get("files", [])]
            source = self._source(a["source"])
            if a["result"] == "PASS" and not source["uri"].startswith("/"):
                raise Conflict("首版 PASS 需本地可回读的回执；远端来源可登记 UNKNOWN")
            previous_checks = [v for v in nodes.values() if v["kind"] == "Validation" and v["data"].get("task_id") == n["id"] and v["data"].get("check") == a["check"]]
            previous = max(previous_checks, key=lambda v: v["data"]["sequence"])["data"] if previous_checks else {}
            if "input_binding" in previous and "inputs" not in a:
                raise Conflict("本检查已有准确输入绑定；后续不能省略inputs静默降级，先核准确输入/版本")
            binding = None
            if "inputs" in a:
                inputs = a["inputs"]
                if not isinstance(inputs, list) or not 1 <= len(inputs) <= 32:
                    raise Conflict("inputs须为1–32个准确id/expected_version对象")
                roots = []
                for item in inputs:
                    if not isinstance(item, dict) or set(item) != {"id", "expected_version"} or not isinstance(item["id"], str):
                        raise Conflict("每个input仅接受id/expected_version")
                    self._version(self._node(nodes, item["id"]), item)
                    if item["id"] in roots:
                        raise Conflict("验证输入不可重复")
                    roots.append(item["id"])
                binding = self._input_observation(roots, nodes, edges)
                if binding["bounds_hit"] and a["result"] == "PASS":
                    raise Conflict("输入关系超出有界核对；不能记录PASS")
                total_bytes, seen = 0, {source["uri"], *(row["uri"] for row in sources)}
                for ident in binding["revisions"]:
                    original = nodes[ident]["data"].get("source")
                    if not isinstance(original, dict) or not isinstance(original.get("uri"), str) or not original["uri"].startswith("/"):
                        continue
                    path = Path(original["uri"])
                    if not path.is_file():
                        raise Conflict("限定输入原件不存在；先核原件")
                    total_bytes += path.stat().st_size
                    if path.stat().st_size > 512000 or total_bytes > 2097152 or len(seen) >= 33:
                        raise Conflict("输入原件超出轻量有界核对；Windows核定后使用准确分段原件")
                    qualified = self._source(original)
                    if qualified.get("sha256") != original.get("sha256"):
                        raise Conflict("登记输入原件已变；先回源核定并追加源版本")
                    if qualified["uri"] not in seen:
                        sources.append(qualified); seen.add(qualified["uri"])
            validations = [v for v in nodes.values() if v["kind"] == "Validation"]
            id = "validation-" + uuid.uuid4().hex[:12]
            v = {"id": id, "kind": "Validation", "title": a["check"] + " · " + a["result"], "body": a["body"], "status": a["result"], "version": 1, "work_revision": 1, "created_at": now(), "updated_at": now(), "data": {"task_id": n["id"], "task_revision": n["work_revision"], "check": a["check"], "result": a["result"], "source": source, "files": sources, "sequence": max([v["data"]["sequence"] for v in validations] or [0]) + 1, "dependency_revisions": {e["target"]: nodes[e["target"]]["work_revision"] for e in edges.values() if e["source"] == n["id"] and e["relation"] == "depends_on"}, "verification_mode": "recorded check receipt; not independently rerun"}}
            if binding is not None:
                v["data"]["input_binding"] = binding
            nodes[id] = v
            self._edge(nodes, edges, id, "validates", n["id"], a["body"])
            return v
        if op == "task_complete":
            if n["status"] not in {"in_progress", "pending_validation"}:
                raise Conflict("仅进行中或待验证任务可完成")
            reasons = bucket["reasons"] + self._validation_reasons(n, nodes, edges)
            if reasons:
                raise Conflict("不能完成：" + "; ".join(reasons))
            n["status"] = "completed"; n["data"]["completed_at"] = now()
            self._touch(n); return n
        raise Conflict("未知写入工具")

    def _import_history(self, a, nodes, edges):
        """A whole vetted batch is one durable event, never a sequence of partial writes."""
        m = a.get("manifest", {})
        if m.get("project_root") != str(self.root) or not m.get("batch_id"):
            raise Conflict("导入身份无效")
        existing = [n for n in nodes.values() if n["data"].get("import_batch") == m["batch_id"]]
        fingerprint = digest(m)
        if existing:
            if any(n["data"].get("import_fingerprint") != fingerprint for n in existing):
                raise Conflict("批次内容变化；须生成新的冲突预览，不能覆盖")
            return {"batch_id": m["batch_id"], "imported": 0, "existing": len(existing)}
        for source in m.get("sources", []):
            if file_hash(source["uri"]) != source["sha256"]:
                raise Conflict("预览原件已变：" + source["uri"])
        allowed = set(KINDS) - {"Validation", "Project", "Run"}
        for item in m.get("nodes", []):
            ident, kind = item["id"], item["kind"]
            if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", ident) or ident in nodes or kind not in allowed:
                raise Conflict("导入 ID 冲突或类型无效：" + ident)
            source = self._source(item["source"])
            data = copy.deepcopy(item.get("data", {}))
            if any(k in data for k in ("owner", "checks", "import_batch", "import_fingerprint")):
                raise Conflict("导入包含受控字段")
            data.update({"source": source, "import_batch": m["batch_id"], "import_fingerprint": fingerprint, "user_acceptance": "NOT_CONFIRMED"})
            status = "open"
            if kind in TASKS:
                # Original state remains verbatim. No historical assistant claim creates PASS.
                status = "todo" if item.get("state") == "todo" else "pending_validation"
                data.update({"spec": item.get("body", ""), "owner": None, "checks": ["imported_scope_verification"]})
            if kind == "Idea" and data.get("disposition") == "incorporated":
                status = "promoted"
            if data.get("import_disposition") in {"defer", "exclude"}:
                status = "deferred" if data["import_disposition"] == "defer" else "cancelled"
            if kind == "HumanGate":
                status = "pending"
                data["approval_qualification"] = "historical_source_only; not a current human decision"
            if kind == "Thread":
                if not re.fullmatch(r"[0-9a-f-]{36}", data.get("thread_id", "")):
                    raise Conflict("聊天身份无效")
                if any(n["kind"] == "Thread" and n["data"].get("thread_id") == data["thread_id"] for n in nodes.values()):
                    raise Conflict("真实聊天已存在，请关联而不是重复导入")
                data.update({"runtime_connected": False, "project_id": nodes["project"]["data"]["project_id"]})
            nodes[ident] = {"id": ident, "kind": kind, "title": item["title"], "body": item.get("body", ""), "status": status, "version": 1, "work_revision": 1, "created_at": now(), "updated_at": now(), "data": data}
            self._edge(nodes, edges, "project", "contains", ident, "历史导入批次 " + m["batch_id"])
        # Imported labels and source roles are historical material, not a decision.
        # Keep already-existing gates untouched; only the current human channel
        # can resolve a newly imported pending gate after reviewing its source.
        for e in m.get("edges", []):
            self._edge(nodes, edges, e["source"], e["relation"], e["target"], e["basis"], "source-import")
        # Recheck originals after candidate construction, still under Store lock.
        # Abort the in-memory mutation if a writer changed an original mid-read.
        for source in m.get("sources", []):
            if file_hash(source["uri"]) != source["sha256"]:
                raise Conflict("导入过程中原件已变；未追加事件：" + source["uri"])
        return {"batch_id": m["batch_id"], "imported": len(m["nodes"]), "existing": 0}

    def snapshot(self):
        with self.locked() as db:
            nodes, edges = self._nodes(db), self._edges(db)
            seq = db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
            ordered = list(nodes.values())
            # Keep the human overview bounded by bytes as well as object count.
            # The graph_get/query tools remain the route to full individual records.
            visible, visible_bytes = [], 0
            for n in ordered[:1500]:
                size = len(encode(n).encode("utf-8"))
                if visible and visible_bytes + size > 4 * 1024 * 1024:
                    break
                visible.append(n)
                visible_bytes += size
            ids = {n["id"] for n in visible}
            return {"project": nodes["project"], "nodes": visible,
                    "edges": [e for e in edges.values() if e["source"] in ids and e["target"] in ids],
                    "frontier": self.frontier(nodes, edges), "sequence": seq,
                    "truncated": len(visible) < len(ordered), "total": len(ordered), "at": now()}
