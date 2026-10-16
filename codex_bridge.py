"""Bounded, read-only Codex app-server requests for the local workspace.

Each request owns a short-lived stdio process. This deliberately does not claim
control of a turn running in another app-server (including Codex Desktop).
"""
import json
import hashlib
import os
import select
import subprocess
import time
from pathlib import Path


class BridgeError(Exception):
    pass


def request(method, params, timeout=15):
    process = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        messages = [
            {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "project-graph-workspace", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            }},
            {"id": 2, "method": method, "params": params},
        ]
        process.stdin.write(b"".join((json.dumps(m) + "\n").encode() for m in messages))
        process.stdin.flush()
        deadline, pending = time.monotonic() + timeout, b""
        while time.monotonic() < deadline:
            wait = min(1, deadline - time.monotonic())
            if not select.select([process.stdout], [], [], wait)[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise BridgeError("Codex 连接已关闭")
            pending += chunk
            if len(pending) > 12 * 1024 * 1024:
                raise BridgeError("Codex 返回超过预览上限")
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                reply = json.loads(line)
                if reply.get("id") == 2:
                    if "error" in reply:
                        raise BridgeError(reply["error"].get("message", "Codex 请求失败"))
                    return reply["result"]
        raise BridgeError("Codex 读取超时，请稍后重试")
    except FileNotFoundError as exc:
        raise BridgeError("本机未找到 Codex CLI") from exc
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def project_paths(store):
    """Exact cwd allowlist, including verified Git worktrees of this project."""
    paths = {str(store.root)}
    try:
        result = subprocess.run(
            ["git", "-C", str(store.root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        paths.update(line[9:] for line in result.stdout.splitlines() if line.startswith("worktree "))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(str(Path(p).resolve()) for p in paths if Path(p).is_dir())


def discover(store, search="", cursor=None, limit=30):
    params = {"cwd": project_paths(store), "limit": min(max(int(limit), 1), 50)}
    if search:
        params["searchTerm"] = search[:150]
    if cursor:
        params["cursor"] = cursor
    result = request("thread/list", params)
    threads = [{
        "id": t["id"], "title": t.get("name") or t.get("preview") or "未命名聊天",
        "cwd": t.get("cwd"), "source": t.get("source"),
        "originator": t.get("originator"), "model": t.get("model"),
        "updated_at": t.get("updatedAt"), "runtime_status": t.get("status", {}).get("type", "unknown"),
        "readable": True, "control_verified": False,
    } for t in result.get("data", [])]
    # A conversation can be renamed after being registered in the graph.
    # Search historical local aliases without scanning unrelated chats.
    if search and not cursor:
        linked = store.call("graph_search", {"kind": "Thread", "query": search, "limit": 500})["nodes"]
        seen = {t["id"] for t in threads}
        for node in linked:
            tid = node["data"].get("thread_id")
            if not tid or tid in seen:
                continue
            try:
                meta = metadata(store, tid)
            except BridgeError:
                continue
            threads.append({"id": tid, "title": (meta.get("name") or meta.get("preview") or node["title"])
                            + " · 历史登记名：" + node["title"],
                            "cwd": meta.get("cwd"), "source": meta.get("source"),
                            "originator": meta.get("originator"), "model": meta.get("model"),
                            "updated_at": meta.get("updatedAt"),
                            "runtime_status": meta.get("status", {}).get("type", "unknown"),
                            "readable": True, "control_verified": False})
            seen.add(tid)
    return {"threads": threads, "next_cursor": result.get("nextCursor"),
            "scope": "仅本机已选择项目的目录及其 Git 工作树；状态来自独立只读连接，不代表桌面实时运行"}


def preview(store, thread_id, cursor=None, limit=1):
    meta = metadata(store, thread_id)
    return preview_from_meta(meta, thread_id, cursor, limit)


def metadata(store, thread_id):
    meta = request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
    if str(Path(meta.get("cwd") or "/").resolve()) not in project_paths(store):
        raise BridgeError("此聊天的执行目录不属于当前项目")
    return meta


def text_item(item):
    """Keep async questions distinct from answers even when typed agentMessage."""
    if item.get("type") == "userMessage":
        value = "\n".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text")
        return "user", value, "user_text"
    if item.get("type") == "agentMessage":
        return "assistant", item.get("text", ""), (
            "assistant_question" if item.get("questions") else "assistant_report")
    return None


def preview_from_meta(meta, thread_id, cursor=None, limit=1):
    # A single real turn may contain hundreds of tool items. Keep each response
    # bounded; the cursor still lets the user walk the entire turn history.
    params = {"threadId": thread_id, "limit": 1,
              "itemsView": "full", "sortDirection": "desc"}
    if cursor:
        params["cursor"] = cursor
    warning = None
    try:
        page = request("thread/turns/list", params)
    except BridgeError as exc:
        if "超过预览上限" not in str(exc):
            raise
        params["itemsView"] = "summary"
        page = request("thread/turns/list", params)
        warning = "本轮内容超过预览上限；仅显示前几项文本，不能视为本轮已完整读取。"
        for turn in page.get("data", []):
            items = request("thread/items/list", {"threadId": thread_id,
                      "turnId": turn["id"], "limit": 5})
            turn["items"] = [entry.get("item", {}) for entry in items.get("data", [])]
            turn["items_next_cursor"] = items.get("nextCursor")
    turns = []
    for turn in page.get("data", []):
        items = []
        text_count = 0
        for item in turn.get("items", []):
            parsed = text_item(item)
            if parsed is None:
                continue
            text_count += 1
            if len(items) == 30:
                continue
            role, value, message_kind = parsed
            items.append({"id": item.get("id"), "role": role, "text": value[:4000],
                          "truncated": len(value) > 4000, "message_kind": message_kind})
        incomplete = bool(turn.get("items_next_cursor")) or text_count > len(items)
        if incomplete:
            warning = "本轮文本未全部显示；更早轮次按钮不能补齐本轮遗漏项，须按原片段定位回读。"
        turns.append({"id": turn.get("id"), "status": turn.get("status"),
                      "started_at": turn.get("startedAt"), "items": items,
                      "text_items_truncated": incomplete,
                      "visible_text_items": len(items), "text_items_observed": text_count,
                      "raw_items_observed": len(turn.get("items", [])),
                      "items_next_cursor": turn.get("items_next_cursor")})
    return {"thread": {"id": meta["id"], "title": meta.get("name") or meta.get("preview") or "未命名聊天",
                        "cwd": meta.get("cwd"), "source": meta.get("source"), "path": meta.get("path"),
                        "status": meta.get("status", {}).get("type", "unknown")},
            "turns": turns, "next_cursor": page.get("nextCursor"),
            "scope": "只显示当前页的用户和助手文本；助手描述的完成不等于实际完成",
            "warning": warning}


def message(store, thread_id, turn_id, item_id):
    meta = metadata(store, thread_id)
    cursor = None
    for _ in range(100):
        params = {"threadId": thread_id, "turnId": turn_id, "limit": 30}
        if cursor:
            params["cursor"] = cursor
        try:
            page = request("thread/items/list", params)
        except BridgeError as exc:
            if "超过预览上限" not in str(exc):
                raise
            params["limit"] = 5
            page = request("thread/items/list", params)
        for entry in page.get("data", []):
            item = entry.get("item", {})
            if entry.get("turnId") != turn_id or item.get("id") != item_id:
                continue
            parsed = text_item(item)
            if parsed is None:
                raise BridgeError("此类型不能作为用户或助手文本导入")
            role, body, message_kind = parsed
            return {"thread_id": thread_id, "turn_id": turn_id, "item_id": item_id,
                    "role": role, "text": body, "sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "message_kind": message_kind,
                    "runtime_path": meta.get("path")}
        cursor = page.get("nextCursor")
        if not cursor:
            break
    raise BridgeError("在当前聊天轮次中未找到指定消息")
