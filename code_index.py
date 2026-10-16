"""Read-only access to Windows-built code snapshots; graph events remain authoritative."""

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from service import Conflict

CACHE_ROOT = Path(__file__).resolve().parent / ".project-graph" / "code-index"


def number(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class CodeIndex:
    def __init__(self, store):
        self.store = store
        self.project_id = store.call("project_open")["project"]["data"]["project_id"]
        pointer = CACHE_ROOT / "manifest.json"
        self.entry = (json.loads(pointer.read_text()).get(self.project_id)
                      if pointer.exists() else None)
        self.path = None
        if self.entry:
            name = self.entry.get("filename", "")
            candidate = (CACHE_ROOT / name).resolve()
            if candidate.parent == CACHE_ROOT.resolve() and candidate.suffix == ".sqlite" and candidate.is_file():
                self.path = candidate

    def connect(self):
        if not self.path:
            raise Conflict("此项目尚无 Windows 构建的代码索引；旧任务图仍可使用")
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        identity = json.loads(db.execute("SELECT value FROM meta WHERE key='project_id'").fetchone()[0])
        if identity != self.project_id:
            db.close()
            raise Conflict("代码缓存的项目身份不匹配")
        return db

    def refresh_observation(self):
        path = CACHE_ROOT / ("project-refresh-"+self.project_id+".json")
        if not path.exists():
            return None
        with path.open("rb") as handle:
            raw = handle.read(8193)
        if len(raw) > 8192:
            return {"state": "UNKNOWN", "reason": "刷新观察超过轻量上限"}
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return {"state": "UNKNOWN", "reason": "刷新观察不可读；旧缓存仍单独核验"}
        if not isinstance(value, dict):
            return {"state": "UNKNOWN", "reason": "刷新观察结构无效"}
        if value.get("project_id") != self.project_id:
            raise Conflict("刷新观察项目身份不一致")
        return value

    def status(self):
        if not self.path:
            return {"project_id": self.project_id, "available": False,
                    "reason": "Windows 索引尚未切换；不能在 Mac 上自动批量解析"}
        with closing(self.connect()) as db:
            meta = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key,value FROM meta")}
            files = [dict(r) for r in db.execute(
                "SELECT language,status,count(*) AS count FROM files GROUP BY language,status ORDER BY language,status")]
            relation_count = db.execute("SELECT count(*) FROM relations").fetchone()[0]
            return {"project_id": self.project_id, "available": True, "version": self.entry.get("sha256"),
                    "built_at": meta.get("built_at"), "source_root": meta.get("source_root"),
                    "parser": meta.get("parser"), "files": files, "relation_count": relation_count,
                    "symbol_extractor_version": meta.get("symbol_extractor_version"),
                    "resolver_version": meta.get("resolver_version"),
                    "semantic_capabilities": meta.get("semantic_capabilities", {}),
                    "semantic_scope": "静态提取范围；parsed不等于关系完整；旧缓存无范围声明时保留未知",
                    "refresh": self.refresh_observation(),
                    "freshness": "Windows来源快照；刷新状态及观察时间独立列出，失败保留旧缓存。当前源仍须按版本核对"}

    def query(self, operation, args):
        if operation == "task_context":
            return self.task_context(args)
        if operation == "status":
            return self.status()
        if operation == "git":
            return self.git()
        with closing(self.connect()) as db:
            limit = number(args.get("limit"), 30, 1, 100)
            offset = number(args.get("offset"), 0, 0, 100000)
            if operation == "search":
                query = str(args.get("query", ""))[:160].strip()
                if not query:
                    rows = db.execute("SELECT id,path,kind,name,line,end_line FROM symbols ORDER BY path,line LIMIT ? OFFSET ?",
                                      (limit + 1, offset))
                else:
                    like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
                    rows = db.execute("SELECT id,path,kind,name,line,end_line FROM symbols "
                                      "WHERE name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
                                      "ORDER BY path,line LIMIT ? OFFSET ?",
                                      (like, like, limit + 1, offset))
                data = [dict(r) for r in rows]
                return {"project_id": self.project_id, "scope": "Windows 索引中的语法符号",
                        "query": query, "offset": offset, "limit": limit,
                        "truncated": len(data) > limit, "items": data[:limit],
                        "version": self.entry.get("sha256")}
            if operation == "inventory":
                status = str(args.get("status", "")).strip()
                if status and status not in {"parsed", "parse_error", "parser_unavailable",
                                             "skipped_size_or_binary", "unsupported_extension"}:
                    raise Conflict("未知源码解析状态")
                if status:
                    rows = db.execute("SELECT path,language,sha256,bytes,status,errors "
                                      "FROM files WHERE status=? ORDER BY path LIMIT ? OFFSET ?",
                                      (status, limit + 1, offset))
                else:
                    rows = db.execute("SELECT path,language,sha256,bytes,status,errors "
                                      "FROM files ORDER BY path LIMIT ? OFFSET ?",
                                      (limit + 1, offset))
                data = [dict(r) for r in rows]
                for item in data:
                    item["errors"] = json.loads(item["errors"])
                return {"project_id": self.project_id, "scope": "当前 Windows 快照逐文件覆盖；仅已登记的维护源码清单",
                        "status_filter": status or None, "offset": offset, "limit": limit,
                        "truncated": len(data) > limit, "items": data[:limit],
                        "version": self.entry.get("sha256")}
            if operation == "file":
                path = str(args.get("path", ""))
                file = db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
                if not file:
                    raise Conflict("文件不在当前项目索引中")
                symbols = [dict(r) for r in db.execute(
                    "SELECT id,kind,name,line,end_line FROM symbols WHERE path=? ORDER BY line LIMIT ? OFFSET ?",
                    (path, limit + 1, offset))]
                unresolved = [dict(r) for r in db.execute(
                    "SELECT kind,text,line,reason FROM unresolved WHERE path=? ORDER BY line LIMIT 30", (path,))]
                return {"project_id": self.project_id, "file": dict(file),
                        "symbols": symbols[:limit], "truncated": len(symbols) > limit,
                        "unresolved": unresolved, "unresolved_truncated": db.execute(
                            "SELECT count(*) FROM unresolved WHERE path=?", (path,)).fetchone()[0] > 30,
                        "offset": offset, "version": self.entry.get("sha256")}
            if operation == "impact":
                start = str(args.get("id", ""))
                known = db.execute("SELECT 1 FROM symbols WHERE id=?", (start,)).fetchone()
                if not known:
                    known = db.execute("SELECT 1 FROM files WHERE path=?", (start,)).fetchone()
                if not known:
                    raise Conflict("影响分析起点不在此代码索引中")
                found, frontier = set(), [start]
                parents = {}
                expanded = set()
                bounds_hit = set()
                depth = number(args.get("depth"), 2, 1, 3)
                for _ in range(depth):
                    nxt = []
                    if len(frontier) > 100:
                        bounds_hit.add("frontier_limit")
                    for target in frontier[:100]:
                        expanded.add(target)
                        rows = list(db.execute("SELECT source,kind,method,path,line FROM relations "
                                               "WHERE target=? ORDER BY source,path,line,kind,method LIMIT 101", (target,)))
                        if len(rows) > 100:
                            bounds_hit.add("edge_limit")
                        for row in rows[:100]:
                            if row["source"] not in found and row["source"] != start:
                                found.add(row["source"])
                                parents[row["source"]] = {**dict(row), "target": target}
                                nxt.append(row["source"])
                    if len(nxt) > 100:
                        bounds_hit.add("frontier_limit")
                    frontier = nxt[:100]
                    if not frontier:
                        break
                if frontier and not bounds_hit:
                    # A nonempty last layer can be a leaf or a cycle. Only report
                    # omitted depth when an unvisited predecessor actually exists.
                    # Earlier fan-out bounds already report truncation; avoid
                    # constructing a large SQL parameter list from hidden layers.
                    visited = sorted(found | {start})
                    placeholders = ",".join("?" for _ in visited)
                    for target in frontier:
                        hidden = db.execute(
                            "SELECT 1 FROM relations WHERE target=? AND source NOT IN ("
                            + placeholders + ") LIMIT 1", (target, *visited)).fetchone()
                        if hidden:
                            bounds_hit.add("depth_limit")
                            break
                if len(found) > limit:
                    bounds_hit.add("item_limit")
                selected = sorted(found)[:limit]
                candidates = []
                for identity in selected:
                    node = db.execute("SELECT id,path,name,kind,line FROM symbols WHERE id=?",
                                      (identity,)).fetchone()
                    if node:
                        item = dict(node)
                    else:
                        file = db.execute("SELECT path FROM files WHERE path=?", (identity,)).fetchone()
                        item = {"id": identity, "path": file["path"] if file else None,
                                "name": Path(identity).name if file else identity,
                                "kind": "file" if file else "unresolved", "line": 1 if file else None}
                    # Preserve the first BFS edge, so every path really leads
                    # through visited edges to the requested start (not a cycle).
                    witness = parents.get(identity)
                    item["via"] = ({k: v for k, v in witness.items() if k != "source"}
                                   if witness else None)
                    trail, cursor = [], identity
                    for _ in range(depth):
                        edge = parents.get(cursor)
                        if not edge:
                            break
                        trail.append(dict(edge))
                        cursor = edge["target"]
                        if cursor == start:
                            break
                    item["witness_path"] = trail
                    item["path_reaches_start"] = cursor == start
                    candidates.append(item)
                path_ids = {start}
                for item in candidates:
                    for edge in item["witness_path"]:
                        path_ids.update((edge["source"], edge["target"]))
                path_nodes = []
                for identity in sorted(path_ids):
                    row = db.execute("SELECT id,path,name,kind,line FROM symbols WHERE id=?",
                                     (identity,)).fetchone()
                    if row:
                        location = dict(row)
                    else:
                        file = db.execute("SELECT path FROM files WHERE path=?", (identity,)).fetchone()
                        location = {"id": identity, "name": Path(identity).name if file else identity,
                                    "path": file["path"] if file else None,
                                    "kind": "file" if file else "unresolved", "line": 1 if file else None}
                    path_nodes.append(location)
                return {"project_id": self.project_id, "start": start, "depth": depth,
                        "limit": limit,
                        "scope": f"仅静态可解析关系的反向候选，最多{depth}层；动态调用和未覆盖文件不在结论内",
                        "items": selected, "nodes": candidates, "path_nodes": path_nodes,
                        "observed_candidate_count": len(found),
                        "omitted_observed_candidates": len(found) - len(selected),
                        "total_candidates_known": not bool(bounds_hit & {"depth_limit", "frontier_limit", "edge_limit"}),
                        "truncated": len(found) > limit or bool(bounds_hit),
                        "bounds_hit": sorted(bounds_hit), "frontier_limit": 100, "edges_per_target_limit": 100,
                        "version": self.entry.get("sha256")}
            if operation == "neighborhood":
                start = str(args.get("id", ""))
                symbol = db.execute("SELECT id,path,name,kind,line FROM symbols WHERE id=?", (start,)).fetchone()
                if not symbol:
                    indexed_file = db.execute("SELECT path FROM files WHERE path=?", (start,)).fetchone()
                    if not indexed_file:
                        raise Conflict("代码节点不在此项目索引中")
                    symbol = {"id": start, "path": start, "name": Path(start).name,
                              "kind": "file", "line": 1}
                edges = [dict(r) for r in db.execute(
                    "SELECT source,target,kind,method,path,line FROM relations "
                    "WHERE source=? OR target=? ORDER BY path,line LIMIT ?",
                    (start, start, limit + 1))]
                ids = {start}
                for edge in edges[:limit]:
                    ids.update((edge["source"], edge["target"]))
                nodes = []
                for identity in sorted(ids):
                    row = symbol if identity == start else db.execute(
                        "SELECT id,path,name,kind,line FROM symbols WHERE id=?", (identity,)).fetchone()
                    nodes.append(dict(row) if row else {"id": identity, "name": Path(identity).name,
                                                          "kind": "file", "path": identity, "line": 1})
                return {"project_id": self.project_id, "center": start, "nodes": nodes,
                        "edges": edges[:limit], "truncated": len(edges) > limit,
                        "limit": limit, "total_edges": db.execute(
                            "SELECT count(*) FROM relations WHERE source=? OR target=?", (start, start)).fetchone()[0],
                        "returned_edges": len(edges[:limit]),
                        "omitted_edges": max(0, db.execute(
                            "SELECT count(*) FROM relations WHERE source=? OR target=?", (start, start)).fetchone()[0] - len(edges[:limit])),
                        "scope": "仅索引中可静态解析的邻接候选，动态关系保留未知",
                        "version": self.entry.get("sha256")}
            if operation == "context":
                query = str(args.get("query", ""))[:160].strip()
                budget = number(args.get("budget_tokens"), 1000, 100, 8000)
                if not query:
                    raise Conflict("上下文选择需要明确问题或符号词")
                like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
                rows = db.execute("SELECT s.id,s.path,s.name,s.kind,s.line,s.end_line,f.sha256 "
                                  "FROM symbols s JOIN files f ON f.path=s.path "
                                  "WHERE s.name LIKE ? ESCAPE '\\' OR s.path LIKE ? ESCAPE '\\' "
                                  "ORDER BY CASE WHEN s.name=? THEN 0 ELSE 1 END,s.path,s.line LIMIT 101",
                                  (like, like, query))
                candidates = list(rows)
                if args.get("include_source") in {True, 1, "1", "true"}:
                    source_root = json.loads(db.execute(
                        "SELECT value FROM meta WHERE key='source_root'").fetchone()[0])
                    return self.source_context(candidates, source_root, query, args, limit)
                items, used = [], 0
                bounds_hit = {"candidate_limit"} if len(candidates) > 100 else set()
                for row in candidates[:100]:
                    item = dict(row)
                    # Conservative byte budget for locator metadata, never a
                    # promise about the model tokenizer or full source text.
                    cost = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                    if used + cost > budget:
                        bounds_hit.add("budget_limit")
                        continue
                    if len(items) >= limit:
                        bounds_hit.add("item_limit")
                        break
                    used += cost
                    items.append(item)
                return {"project_id": self.project_id, "query": query,
                        "budget_tokens": budget, "estimated_tokens": used, "items": items,
                        "scope": "定位候选；必须回读原件、任务前置和反证，不能用索引代替上下文验证",
                        "truncated": bool(bounds_hit), "bounds_hit": sorted(bounds_hit),
                        "budget_scope": "selected locator metadata only; conservative UTF-8 byte estimate, not source tokens",
                        "candidate_limit": 100, "version": self.entry.get("sha256")}
        raise Conflict("未知代码索引操作")

    def task_context(self, args):
        """Read-only task facts plus explicitly chosen static code anchors."""
        from runtime import ManagedRuntime
        task_id = args.get("task_id")
        seeds = args.get("symbol_ids")
        if not isinstance(task_id, str) or not task_id:
            raise Conflict("任务代码上下文必须明确 task_id")
        if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 10
                or any(not isinstance(s, str) or not s for s in seeds)
                or len(set(seeds)) != len(seeds)):
            raise Conflict("symbol_ids 必须是1–10个不重复的准确代码符号ID")
        budget = number(args.get("budget_bytes"), 16384, 100, 32768)
        task = ManagedRuntime.context(self.store, task_id, include_optional_refs=False)
        got = self.store.call("graph_get", {"id": task_id})
        required_edges = [e for e in got["edges"] if e["relation"] in {
            "reads", "derived_from", "supports", "contradicts", "insufficient_for"}]
        if len(required_edges) > 64:
            raise Conflict("必要证据/反证关系超过64项；先分段核原件，不静默裁剪")
        refs = []
        evidence_refs = []
        for e in sorted(required_edges, key=lambda e: e["id"]):
            ident = e["target"] if e["source"] == task_id else e["source"]
            node = self.store.call("graph_get", {"id": ident})["node"]
            refs.append({"id": ident, "relation": e["relation"], "kind": node["kind"],
                         "title": node["title"], "status": node["status"],
                         "version": node["version"], "source": node["data"].get("source"),
                         "body": node["body"], "data": node["data"], "edge": e,
                         "created_at": node["created_at"], "updated_at": node["updated_at"],
                         "work_revision": node["work_revision"],
                         "reading_status": "已读图节点正文及限定；原件尚未由本接口读验"})
            if node["kind"] in {"Claim", "Hypothesis"}:
                linked = self.store.call("graph_get", {"id": ident})
                for evidence_edge in linked["edges"]:
                    if (evidence_edge["target"] != ident or evidence_edge["relation"] not in {
                            "supports", "contradicts", "insufficient_for"}):
                        continue
                    if len(refs) + len(evidence_refs) >= 64:
                        raise Conflict("必要证据/反证关系超过64项；先分段核原件，不静默裁剪")
                    evidence = self.store.call("graph_get", {"id": evidence_edge["source"]})["node"]
                    evidence_refs.append({"id": evidence["id"], "relation": evidence_edge["relation"],
                                          "via_object": ident, "via_version": node["version"],
                                          "kind": evidence["kind"], "title": evidence["title"],
                                          "status": evidence["status"], "version": evidence["version"],
                                          "source": evidence["data"].get("source"),
                                          "body": evidence["body"], "data": evidence["data"],
                                          "created_at": evidence["created_at"], "updated_at": evidence["updated_at"],
                                          "work_revision": evidence["work_revision"],
                                          "edge": evidence_edge,
                                          "reading_status": "已读图节点正文及限定；原件尚未由本接口读验"})
        if len(refs) + len(evidence_refs) > 64:
            raise Conflict("必要证据/反证关系超过64项；先分段核原件，不静默裁剪")
        refs.extend(evidence_refs)
        payload = {"project_id": self.project_id, "task_context": task,
                   "required_evidence_refs": refs, "symbol_ids": seeds,
                   "code": {"items": [], "omission": "optional code not selected"},
                   "budget_bytes": budget, "used_bytes": 0,
                   "budget_scope": "完整返回JSON的UTF-8字节；不是模型token数",
                   "observed_at": datetime.now(timezone.utc).isoformat(),
                   "scope": "任务/前置/门禁及必要图节点正文、限定、关系依据保留；图正文不代替原件实读。显式符号及一跳静态邻接为候选。图外遗漏、动态关系和业务相关性仍需原件判断。只读预览不授权执行。"}
        size = lambda: len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size() > budget:
            raise Conflict("必要任务/前置/门禁/证据正文及限定超过预算；未静默删除，需提高预算或分段核查")
        with closing(self.connect()) as db:
            source_root = json.loads(db.execute("SELECT value FROM meta WHERE key='source_root'").fetchone()[0])
            candidates, seen, bounds = [], set(), set()
            sql = "SELECT s.id,s.path,s.name,s.kind,s.line,s.end_line,f.sha256 FROM symbols s JOIN files f ON f.path=s.path "
            for ident in seeds:
                row = db.execute(sql + "WHERE s.id=?", (ident,)).fetchone()
                if not row:
                    raise Conflict("显式符号不在此项目缓存；未按名称猜测或跨项目选取")
                item = dict(row); item["selection"] = "explicit_symbol_seed"
                candidates.append(item); seen.add(ident)
            for ident in seeds:
                neighbors = list(db.execute(sql + "WHERE s.id IN (SELECT target FROM relations WHERE source=? UNION SELECT source FROM relations WHERE target=?) ORDER BY s.path,s.line LIMIT 101", (ident, ident)))
                if len(neighbors) > 100:
                    bounds.add("neighbor_query_limit")
                for row in neighbors[:100]:
                    if row["id"] in seen:
                        continue
                    if len(candidates) >= 100:
                        bounds.add("candidate_limit"); break
                    item = dict(row); item["selection"] = "one_hop_static_neighbor"
                    candidates.append(item); seen.add(row["id"])
            # Reserve the wrapper before reading originals. The existing source
            # reader checks SHA, symlink escape, per-file and total read bounds.
            available = budget - size() - 1024
            if available >= 100:
                code = self.source_context(candidates, source_root, "explicit symbol IDs",
                                           {"budget_bytes": available}, number(args.get("limit"), 10, 1, 30))
                payload["code"] = {k: v for k, v in code.items() if k not in {"skipped", "query"}}
                counts = {}
                for skipped in code["skipped"]:
                    reason = skipped["reason"]
                    counts[reason] = counts.get(reason, 0) + 1
                payload["code"].update(omitted_by_reason=counts, candidate_count=len(candidates),
                                       selection_scope="explicit IDs first, then one-hop static neighbors; not keyword relevance")
                payload["code"]["bounds_hit"] = sorted(set(code["bounds_hit"]) | bounds)
                payload["code"]["truncated"] = code["truncated"] or bool(bounds)
                while size() > budget and payload["code"]["items"]:
                    payload["code"]["items"].pop()
                    counts["combined_budget_limit"] = counts.get("combined_budget_limit", 0) + 1
                    payload["code"]["truncated"] = True
                payload["code"]["used_bytes"] = len(json.dumps(payload["code"]["items"], ensure_ascii=False).encode("utf-8"))
                if size() > budget:
                    payload["code"] = {"items": [], "omission": "combined_budget_limit",
                                       "truncated": True, "version": self.entry.get("sha256")}
            else:
                payload["code"] = {"items": [], "omission": "required_context_used_budget",
                                   "truncated": True, "version": self.entry.get("sha256")}
        if self.store.call("project_status")["sequence"] != task["sequence"]:
            raise Conflict("读取期间项目已变化；重新预览，不合并不同版本")
        for _ in range(3):
            payload["used_bytes"] = size()
        if size() > budget:
            raise Conflict("完整响应超过预算；必要约束保持，未返回部分成功")
        return payload

    def source_context(self, candidates, source_root, query, args, limit):
        """Read a few indexed originals, never parse or silently use stale bytes."""
        budget = number(args.get("budget_bytes"), 8192, 100, 32768)
        read_limit, file_limit, line_limit = 1048576, 5, 40
        root = Path(source_root).resolve()
        files, skipped, items = {}, [], []
        read_bytes = 0
        bounds = {"candidate_limit"} if len(candidates) > 100 else set()
        for row in candidates[:100]:
            if len(items) >= limit:
                bounds.add("item_limit")
                break
            item = dict(row)
            path = item["path"]
            if path not in files:
                if len(files) >= file_limit:
                    bounds.add("source_file_limit")
                    skipped.append({"id": item["id"], "path": path, "reason": "source_file_limit"})
                    continue
                files[path] = {"reason": "unread"}
                try:
                    resolved = Path(path).resolve()
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        raise ValueError("outside_source_root") from None
                    with resolved.open("rb") as stream:
                        # fstat binds both observations to the opened original.
                        before = os.fstat(stream.fileno())
                        if before.st_size > 262144 or read_bytes + before.st_size > read_limit:
                            bounds.add("source_read_limit")
                            raise ValueError("source_read_limit")
                        raw = stream.read(min(262144, read_limit - read_bytes))
                        read_bytes += len(raw)
                        after = os.fstat(stream.fileno())
                    if len(raw) > 262144 or read_bytes > read_limit:
                        bounds.add("source_read_limit")
                        raise ValueError("source_read_limit")
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError("source_changed_during_read")
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest != item["sha256"]:
                        raise ValueError("source_sha_mismatch")
                    try:
                        lines = raw.decode("utf-8").splitlines()
                    except UnicodeDecodeError:
                        raise ValueError("source_not_utf8") from None
                    files[path] = {"lines": lines, "sha256": digest}
                except (OSError, ValueError) as error:
                    reason = str(error) if isinstance(error, ValueError) else "source_unavailable"
                    files[path] = {"reason": reason}
            source = files[path]
            if "reason" in source:
                skipped.append({"id": item["id"], "path": path, "reason": source["reason"]})
                continue
            start, end = item["line"], item["end_line"]
            if start < 1 or end < start or end > len(source["lines"]):
                skipped.append({"id": item["id"], "path": path, "reason": "source_line_range_mismatch"})
                continue
            shown_end = min(end, start + line_limit - 1)
            if shown_end < end:
                bounds.add("source_line_limit")
            item.update(source_start_line=start, source_end_line=shown_end,
                        source_truncated=shown_end < end, source_verified=True)
            while shown_end >= start:
                item["source_end_line"] = shown_end
                item["text"] = "\n".join(source["lines"][start - 1:shown_end])
                size = len(json.dumps(items + [item], ensure_ascii=False).encode("utf-8"))
                if size <= budget:
                    items.append(item)
                    break
                bounds.add("budget_limit")
                item["source_truncated"] = True
                shown_end -= 1
            else:
                skipped.append({"id": item["id"], "path": path, "reason": "budget_limit"})
        return {"project_id": self.project_id, "query": query, "items": items,
                "budget_bytes": budget,
                "used_bytes": len(json.dumps(items, ensure_ascii=False).encode("utf-8")),
                "budget_scope": "selected items JSON including locator metadata and source text; UTF-8 bytes, not model tokens",
                "source_read_bytes": read_bytes, "source_read_limit": read_limit,
                "source_file_limit": file_limit, "source_file_byte_limit": 262144,
                "source_line_limit": line_limit, "candidate_limit": 100,
                "skipped": skipped, "truncated": bool(bounds or skipped),
                "bounds_hit": sorted(bounds), "observed_at": datetime.now(timezone.utc).isoformat(),
                "scope": "已登记源码的有界原文片段；SHA匹配后返回。未自动加载任务门禁或反证；动态关系与遗漏仍未知",
                "version": self.entry.get("sha256")}

    def git(self):
        status = self.status()
        if not status["available"]:
            return status
        root = Path(status["source_root"])
        observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        def run(arguments):
            # Git may print a large worktree. Spool off memory and read a
            # bounded prefix; optional index refresh/locks are disabled.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen(["git", "--no-optional-locks", "-C", str(root), *arguments],
                                        stdout=output, stderr=errors)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                    raise
                output.seek(0); raw = output.read(65537)
                errors.seek(0); error = errors.read(4096).decode("utf-8", "replace")
                return proc.returncode, raw[:65536], len(raw) > 65536, error
        try:
            code, top, cut, error = run(["rev-parse", "--show-toplevel"])
            if code and "not a git repository" in error:
                return {"project_id": self.project_id, "state": "nonGit",
                        "source_root": str(root), "observed_at": observed}
            if code or cut:
                raise Conflict("Git根观察失败或截断：" + error[:300])
            commit_code, commit, _, _ = run(["rev-parse", "--verify", "HEAD"])
            changes_code, raw, truncated, error = run(["status", "--porcelain=v1", "-z", "--untracked-files=normal"])
            entries, tokens, pos = [], raw.split(b"\0"), 0
            while pos < len(tokens) and tokens[pos]:
                token = tokens[pos]; pos += 1
                if len(token) < 4 or token[2:3] != b" " or len(entries) >= 100:
                    truncated = True; break
                xy = token[:2].decode("ascii", "replace")
                entry = {"code": xy, "path": token[3:].decode("utf-8", "replace")}
                if "R" in xy or "C" in xy:
                    if pos >= len(tokens) or not tokens[pos]:
                        truncated = True; break
                    entry["original_path"] = tokens[pos].decode("utf-8", "replace"); pos += 1
                entries.append(entry)
            return {"project_id": self.project_id, "state": "observed" if changes_code == 0 else "unknown",
                    "reason": error[:300] if changes_code else None,
                    "source_root": str(root), "git_root": top.decode("utf-8", "replace").strip(),
                    "commit": commit.decode("ascii", "replace").strip() if commit_code == 0 else None,
                    "tracked_worktree_changes": [e["path"] for e in entries if e["code"] != "??"],
                    "staged_changes": [e["path"] for e in entries if e["code"][0] not in " ?"],
                    "unstaged_changes": [e["path"] for e in entries if e["code"][1] not in " ?"],
                    "untracked_paths": [e["path"] for e in entries if e["code"] == "??"],
                    "entries": entries, "truncated": truncated, "limit": 100, "output_byte_limit": 65536,
                    "untracked_included": True, "path_scope": "Git status normal; untracked directories may be grouped",
                    "observed_at": observed,
                    "index_built_at": status["built_at"]}
        except (OSError, subprocess.TimeoutExpired, Conflict) as error:
            return {"project_id": self.project_id, "state": "unknown",
                    "reason": str(error), "observed_at": observed}
