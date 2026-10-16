#!/usr/bin/env python3
"""Project Graph Workspace: stdlib CLI, stdio MCP and loopback Web UI."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import mimetypes
import os
import secrets
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlencode
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
from service import Store, Conflict, KINDS, READS, RELATIONS, digest, file_hash, encode, now
from workspace import Workspace
from codex_bridge import BridgeError, discover as discover_threads, preview as preview_thread, metadata as thread_metadata, message as thread_message
from runtime import ManagedRuntime
from code_index import CodeIndex

HERE = Path(__file__).resolve().parent


def local_source_preview(source, message_id=None):
    """Hash the whole bounded original, even when displaying only one message."""
    path = Path(source["uri"])
    limit = 512000
    def version(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if before.st_size > limit:
            raise Conflict("原件超过轻量预览范围；请在本地应用按路径打开")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > limit:
        raise Conflict("原件读取中增长并超过轻量预览范围；请核对版本后重试")
    if version(before) != version(after) or version(after) != version(path.stat()) or len(raw) != after.st_size:
        raise Conflict("原件在读取期间已变化；本次未展示，请核对版本后重试")
    current_sha = hashlib.sha256(raw).hexdigest()
    registered_sha = source.get("sha256")
    freshness = ("match" if registered_sha.lower() == current_sha else "changed") if registered_sha else "unregistered"
    text = raw.decode("utf-8")
    if message_id and path.suffix == ".json":
        messages = json.loads(text).get("messages", [])
        found = [m for m in messages if m.get("message_id") == message_id]
        if len(found) != 1:
            raise Conflict("原文中登记的消息ID缺失或重复；请核对版本")
        text = json.dumps(found[0], ensure_ascii=False, indent=2)
    return {"path": str(path), "locator": source.get("locator"), "text": text,
            "registered_sha256": registered_sha, "current_sha256": current_sha,
            "freshness": freshness, "observed_at": now(), "hash_scope": "whole_file",
            "source_bytes": len(raw), "preview_limit_bytes": limit}


def runtime_cli(store, operation, arguments, port=8766, timeout=20):
    """Use the running workspace connection; never start another App Server."""
    allowed = {"status", "context", "create", "continue", "steer", "interrupt", "reconcile"}
    if operation not in allowed:
        raise Conflict("运行 CLI 不批准人工请求；审批请由用户在网页核对准确请求")
    if not isinstance(arguments, dict) or not 1 <= port <= 65535:
        raise Conflict("运行参数须为 JSON 对象，端口须为 1–65535")
    if operation not in {"status", "context"} and not arguments.get("request_id"):
        raise Conflict("运行操作需要显式稳定 request_id；超时后先 status/reconcile")
    if "action" in arguments or "project_id" in arguments:
        raise Conflict("action/project_id 由命令和显式 --root 确定，不能覆盖")
    identity = store.call("project_open")["project"]["data"]["project_id"]
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def request(path, payload=None, token=None):
        raw = None if payload is None else encode(payload).encode()
        if raw is not None and len(raw) > 128 * 1024:
            raise Conflict("运行请求超过 128 KiB 上限")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-PGW-CSRF"] = token
        try:
            with opener.open(Request(base + path, data=raw, headers=headers),
                             timeout=timeout) as response:
                data = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            error = exc.read(8192).decode("utf-8", "replace")
            raise Conflict(f"工作区拒绝请求 HTTP {exc.code}: {error}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise Conflict("连接失败或超时；未自动重发。保留 request_id，先核对原请求: " +
                           str(exc)) from exc
        if len(data) > 2 * 1024 * 1024:
            raise Conflict("工作区返回超过 2 MiB 上限；结果待核对，未重发")
        return json.loads(data)

    session = request("/api/session")
    if session.get("demo_mode"):
        raise Conflict("Demo 不连接真实运行")
    entries = request("/api/projects")["projects"]
    matching = [entry for entry in entries if entry.get("id") == identity and
                entry.get("root") == str(store.root)]
    if not matching or not any(e.get("root") == session.get("root") for e in entries):
        raise Conflict("服务身份与项目根不匹配；未发送运行操作")
    if operation == "context":
        if set(arguments) != {"task_id"} or not isinstance(arguments["task_id"], str):
            raise Conflict("context只接受明确task_id；无运行派发")
        return {"project_id": identity, "root": str(store.root), "context": request(
            "/api/runtime/context?" + urlencode({"project": identity, "task": arguments["task_id"]}))}
    if operation == "status":
        if arguments:
            raise Conflict("status 不接受运行动作参数")
        return {"project_id": identity, "root": str(store.root),
                "runtime": request("/api/runtime?" + urlencode({"project": identity}))}
    return {"project_id": identity, "root": str(store.root), **request("/api/runtime", {
        **arguments, "action": operation, "project_id": identity}, session["csrf"])}


def code_refresh_cli(store, arguments):
    """Dispatch only confirmed code roots to Windows; no local Python parsing."""
    import shutil
    import subprocess
    import tempfile
    if not isinstance(arguments, dict) or set(arguments) - {"action", "request_id", "worker"}:
        raise Conflict("refresh只接受action/request_id/worker；不接受任意命令或来源目录")
    if arguments.get("action", "status") not in {"start", "status", "apply"}:
        raise Conflict("refresh action仅start/status/apply；不会自动重发")
    ident = arguments.get("request_id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", ident):
        raise Conflict("refresh须稳定request_id（1–48字母数字下划线短横线）")
    if "worker" in arguments and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", str(arguments["worker"])):
        raise Conflict("未知Windows通道；不修改SSH、网络或凭据")
    node = shutil.which("node")
    if not node:
        raise Conflict("既有Node不可用；未在Mac降级成Python批量处理")
    identity = store.call("project_open")["project"]["data"]["project_id"]
    command = [node, str(HERE/"scripts"/"code_index_refresh.mjs"), str(store.root), identity,
               json.dumps(arguments, ensure_ascii=False)]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as error:
        try:
            result = subprocess.run(command, stdout=out, stderr=error, timeout=180, check=False)
        except subprocess.TimeoutExpired as exc:
            raise Conflict("Windows刷新结果待核对；保留request_id，用status核回，不自动重新start") from exc
        out.seek(0); raw = out.read(512*1024+1)
        error.seek(0); detail = error.read(4096).decode("utf-8", "replace")
    if len(raw)>512*1024:
        raise Conflict("刷新返回超过512KiB；结果待核对，不自动重发")
    if result.returncode:
        raise Conflict("Windows刷新拒绝/未确认："+detail)
    response = json.loads(raw)
    if response.get("project_id") != identity or response.get("request_id") != ident:
        raise Conflict("刷新结果身份不匹配；先核旧索引和request_id")
    return {"project_id": identity, "root": str(store.root), "refresh": response,
            "scope": "仅Windows派生代码缓存；项目任务/用户门禁/业务原件不由刷新修改"}


def source_cli(store, operation, arguments, port=8766, timeout=20):
    """Read registered originals through the existing workspace; never infer qualification."""
    if not isinstance(arguments, dict) or not 1 <= port <= 65535:
        raise Conflict("来源参数须为对象，端口须为1–65535")
    allowed = {"inventory": {"offset", "limit"}, "preview": {"id", "expected_version", "version"}, "thread_observe": {"id", "expected_version"}}
    if operation not in allowed or set(arguments) - allowed[operation]:
        raise Conflict("仅支持有界 inventory/preview；不接受任意路径或写入参数")
    node = None
    if operation == "inventory":
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 10)
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 10:
            raise Conflict("CLI清单offset须非负整数，limit须1–10；按next_offset续读")
        query = {"offset": offset, "limit": limit}
        endpoint = "/api/source-inventory"
    else:
        if not isinstance(arguments.get("id"), str) or not arguments["id"] or type(arguments.get("expected_version")) is not int:
            raise Conflict("原件预览需要明确id和整数expected_version")
        if arguments.get("version", "imported") not in {"imported", "current"}:
            raise Conflict("version仅支持imported/current；两者分别核版本")
        node = store.call("graph_get", {"id": arguments["id"]})["node"]
        if node["version"] != arguments["expected_version"]:
            raise Conflict("对象版本已变化；未读取原件，先回读对象")
        query = {"id": node["id"], "version": arguments.get("version", "imported")}
        if operation == "thread_observe":
            if node["kind"] != "Thread":
                raise Conflict("必须是已关联Thread，未读取")
            query = {"id": node["id"], "expected_version": node["version"]}
            endpoint = "/api/thread-observation"
        else:
            endpoint = "/api/source"
    identity = store.call("project_open")["project"]["data"]["project_id"]
    opener = build_opener(ProxyHandler({}))
    def get(path):
        try:
            with opener.open(Request(f"http://127.0.0.1:{port}" + path), timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raise Conflict(f"来源服务拒绝HTTP {exc.code}: " + exc.read(8192).decode("utf-8", "replace")) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise Conflict("来源读取失败或超时；未重试: " + str(exc)) from exc
        if len(raw) > 2 * 1024 * 1024:
            raise Conflict("来源响应超过2MiB；未展示，未重试")
        return json.loads(raw)
    session = get("/api/session")
    entries = get("/api/projects")["projects"]
    if not any(e.get("id") == identity and e.get("root") == str(store.root) for e in entries) or not any(e.get("root") == session.get("root") for e in entries):
        raise Conflict("服务项目身份与--root不匹配；未读取原件")
    result = get(endpoint + "?" + urlencode({"project": identity, **query}))
    if node and store.call("graph_get", {"id": node["id"]})["node"]["version"] != node["version"]:
        raise Conflict("读取期间对象版本变化；未展示，先核新版本")
    return {"project_id": identity, "root": str(store.root), "operation": operation,
            "node_id": node["id"] if node else None, "node_version": node["version"] if node else None,
            "observed_at": now(), "source": result,
            "qualification": "仅有界原件读取及版本观察，不自动采纳或批准业务状态"}


def thread_observation(store, node_id, expected_version):
    """One registered exact thread, metadata only; never control or prove desktop activity."""
    if not isinstance(node_id, str) or not node_id or type(expected_version) is not int:
        raise Conflict("聊天观察需要明确id及整数expected_version")
    node = store.call("graph_get", {"id": node_id})["node"]
    if node["kind"] != "Thread" or node["version"] != expected_version or not isinstance(node["data"].get("thread_id"), str):
        raise Conflict("聊天对象身份/版本已变或不是已关联Thread，未读取")
    meta = thread_metadata(store, node["data"]["thread_id"])
    if meta.get("id") != node["data"]["thread_id"]:
        raise Conflict("只读聊天返回身份不同，未采用")
    if store.call("graph_get", {"id": node_id})["node"]["version"] != expected_version:
        raise Conflict("观察期间聊天对象版本已变，未展示")
    status = meta.get("status") if isinstance(meta.get("status"), dict) else {}
    return {"project_id": store.call("project_open")["project"]["data"]["project_id"],
            "node_id": node_id, "node_version": expected_version,
            "thread": {"id": meta["id"], "title": str(meta.get("name") or meta.get("preview") or node["title"])[:1000],
                       "status": status.get("type", "unknown"), "updated_at": meta.get("updatedAt")},
            "observed_at": now(), "registered_at": node["updated_at"],
            "source": "Codex 独立只读连接（includeTurns=false）", "control_verified": False,
            "scope": "仅这一已关联聊天元数据时点；notLoaded不是桌面空闲，不读取轮次正文、不执行或写项目状态"}


def threads_cli(store, operation, arguments, port=8766, timeout=20):
    if operation != "observe" or not isinstance(arguments, dict) or set(arguments) != {"id", "expected_version"}:
        raise Conflict("仅支持单一已关联聊天observe及id/expected_version；无发现批量/正文/控制/写入")
    value = source_cli(store, "thread_observe", arguments, port, timeout)
    observed = value.pop("source")
    node = store.call("graph_get", {"id": arguments["id"]})["node"]
    if observed.get("project_id") != value["project_id"] or observed.get("node_id") != node["id"] or observed.get("node_version") != node["version"] or observed.get("thread", {}).get("id") != node["data"].get("thread_id"):
        raise Conflict("观察响应身份/版本不匹配，未采用")
    return {**value, "operation": operation, "observation": observed,
            "qualification": "元数据观察不代表桌面实时控制、任务完成或人工批准"}


def cli_arguments(args):
    if getattr(args, "args_file", None):
        if args.arguments != "{}":
            raise Conflict("JSON 参数与 --args-file 不能同时使用")
        with Path(args.args_file).open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise Conflict("参数文件超过 1 MiB 上限")
        value = json.loads(raw)
    else:
        value = json.loads(args.arguments)
    if not isinstance(value, dict):
        raise Conflict("参数须为 JSON 对象")
    return value


class BoundedHTTPServer(ThreadingHTTPServer):
    """Cap simultaneous HTTP graph requests without queuing unlimited workers."""

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                body = b'{"error":"\xe6\x9c\x8d\xe5\x8a\xa1\xe6\x9a\x82\xe6\x97\xb6\xe7\xb9\x81\xe5\xbf\x99\xef\xbc\x8c\xe8\xaf\xb7\xe7\xa8\x8d\xe5\x90\x8e\xe9\x87\x8d\xe8\xaf\x95"}'
                head = ("HTTP/1.1 503 Service Unavailable\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Content-Type: application/json; charset=utf-8\r\n"
                        "Connection: close\r\nRetry-After: 1\r\n\r\n").encode()
                request.sendall(head + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

def chat_source_id(thread_id, turn_id, item_id):
    return "chat-source-" + digest([thread_id, turn_id, item_id])[:20]

def chat_candidate_id(thread_id, turn_id, item_id, kind):
    return "chat-" + kind.lower() + "-" + digest([thread_id, turn_id, item_id, kind])[:20]

def project_nodes(target, max_nodes=5000):
    nodes, offset = [], 0
    while offset < max_nodes:
        page = target.call('graph_get', {'limit': 500, 'offset': offset})
        nodes.extend(page['nodes'])
        if page['next_offset'] is None:
            return nodes, False
        offset = page['next_offset']
    return nodes, True

def source_inventory(target, offset=0, limit=40, demo=False):
    """A bounded, read-only index of files already named by the project graph."""
    if offset < 0 or not 1 <= limit <= 100:
        raise Conflict('来源清单分页参数无效')
    nodes, incomplete = project_nodes(target)
    indexed = {}
    for name in ('AGENTS.md', 'PLAN.md', 'WORK.md', 'README.md'):
        path = target.root / name
        if path.is_file():
            indexed[str(path.resolve())] = {'uri': str(path.resolve()), 'references': [], 'recorded_sha256': set(), 'entry': True}
    for node in nodes:
        for key in ('source', 'original_source'):
            source = node.get('data', {}).get(key)
            if not isinstance(source, dict) or not str(source.get('uri', '')).startswith('/'):
                continue
            uri = source['uri'] if demo else str(Path(source['uri']).expanduser().resolve())
            entry = indexed.setdefault(uri, {'uri': uri, 'references': [], 'recorded_sha256': set(), 'entry': False})
            entry['references'].append({'id': node['id'], 'kind': node['kind'], 'field': key})
            if source.get('sha256'):
                entry['recorded_sha256'].add(source['sha256'])
    paths = sorted(indexed)
    rows = []
    for uri in paths[offset:offset+limit]:
        entry = indexed[uri]
        path = Path(uri)
        inside = path == target.root or target.root in path.parents
        if demo and not inside:
            rows.append({'uri': uri, 'entry': entry['entry'], 'status': 'not_bundled',
                         'size': None, 'current_sha256': None,
                         'recorded_sha256': sorted(entry['recorded_sha256']),
                         'reference_count': len(entry['references']), 'references': entry['references'][:10]})
            continue
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        exists = size is not None
        hashes = sorted(entry['recorded_sha256'])
        try:
            current = file_hash(uri) if exists and inside and size <= 4 * 1024 * 1024 else None
        except OSError:
            current, exists, size = None, False, None
        if not exists:
            status = 'missing'
        elif not inside:
            status = 'outside_project_unchecked'
        elif current is None:
            status = 'large_unchecked'
        elif not hashes:
            status = 'unregistered_hash'
        elif len(hashes) > 1:
            status = 'multiple_recorded_versions'
        else:
            status = 'matched' if current == hashes[0] else 'changed'
        rows.append({'uri': uri, 'entry': entry['entry'], 'status': status,
                     'size': size, 'current_sha256': current, 'recorded_sha256': hashes,
                     'reference_count': len(entry['references']), 'references': entry['references'][:10]})
    return {'files': rows, 'total': len(paths), 'offset': offset,
            'next_offset': offset+limit if offset+limit < len(paths) else None,
            'graph_truncated': incomplete,
            'scope': ('Demo 仅携带图记录；包外原件只列历史路径，不访问宿主机；清单不证明原件已阅读或业务判断已验证'
                      if demo else '仅项目根入口文件与图中已登记的本地文件来源；目录外文件和大于4 MiB的文件只列位置，不核当前内容；清单不证明原件已阅读或业务判断已验证')}

def candidate_matches(nodes, thread_id, turn_id, item_id, kind, title, exclude_id):
    """Deterministic review leads, never a semantic equivalence decision."""
    def grams(value):
        value = ''.join(c for c in value.casefold() if c.isalnum())
        return {value[i:i+2] for i in range(len(value)-1)} if len(value) >= 4 else set()
    target_grams = grams(title)
    matches = []
    for node in nodes:
        if node['id'] == exclude_id or node['kind'] not in {'Requirement','Task','Idea','Blocker','Decision','Artifact'}:
            continue
        data = node.get('data', {})
        reasons = []
        if (data.get('thread_id'), data.get('turn_id'), data.get('item_id')) == (thread_id, turn_id, item_id):
            reasons.append('同一聊天片段')
        if title and node['title'].casefold() == title.casefold():
            reasons.append('标题完全相同')
        elif target_grams and node['kind'] == kind:
            other = grams(node['title'])
            if other and len(target_grams & other) / len(target_grams | other) >= 0.55:
                reasons.append('同类型标题词面相近')
        if not reasons:
            continue
        source = data.get('source') or data.get('original_source') or {}
        matches.append({'id': node['id'], 'kind': node['kind'], 'title': node['title'],
                        'status': node['status'], 'excerpt': node.get('body','')[:300],
                        'source': {'uri': source.get('uri'), 'locator': source.get('locator')},
                        'reasons': reasons})
    matches.sort(key=lambda x: (-len(x['reasons']), x['id']))
    return matches[:20], len(matches)

def chat_identity(target, item, kind=None):
    """Keep the first ID stable; a changed original becomes an explicit revision."""
    base = (chat_candidate_id(item['thread_id'], item['turn_id'], item['item_id'], kind)
            if kind else chat_source_id(item['thread_id'], item['turn_id'], item['item_id']))
    try:
        previous = target.call('graph_get', {'id': base})['node']
    except Conflict:
        previous = None
    if previous and previous['data'].get('message_sha256') != item['sha256']:
        return base + '-v-' + item['sha256'][:12], previous
    return base, None

def ensure_chat_source(target, item, request_id):
    ident, previous = chat_identity(target, item)
    try:
        existing = target.call('graph_get', {'id': ident})['node']
    except Conflict:
        existing = None
    if existing:
        if existing['data'].get('message_sha256') != item['sha256']:
            raise Conflict('同一消息版本身份冲突，不能覆盖旧导入')
        if previous and not any(e['relation'] == 'supersedes' and e['target'] == previous['id']
                                for e in target.call('graph_get', {'id': ident})['edges']):
            target.call('relation_add', {'source_id': ident, 'target_id': previous['id'],
                        'relation': 'supersedes', 'basis': '同一聊天片段的原文字节变化；保留旧版本供比较',
                        'request_id': 'ui-source-revision-' + ident}, channel='ui')
        return existing, True
    locator = f"turn/{item['turn_id']}/item/{item['item_id']}"
    args = {
        'id': ident, 'title': ('用户原文' if item['role'] == 'user' else '助手原文（未核实）') + ' · ' + item['text'][:60],
        'body': item['text'][:4000],
        'source': {'uri': f"codex://threads/{item['thread_id']}", 'locator': locator},
        'epistemic_status': ('user_original' if item['role'] == 'user' else
                             'assistant_question_unanswered' if item.get('message_kind') == 'assistant_question'
                             else 'assistant_report_unverified'),
        'data': {'thread_id': item['thread_id'], 'turn_id': item['turn_id'], 'item_id': item['item_id'],
                 'message_role': item['role'], 'message_sha256': item['sha256'],
                 'message_kind': item.get('message_kind', 'user_text' if item['role'] == 'user' else 'assistant_report'),
                 'runtime_path_at_import': item['runtime_path'],
                 'text_truncated': len(item['text']) > 4000,
                 'adoption': 'source_only; no task completion or user acceptance inferred',
                 **({'revises_node_id': previous['id']} if previous else {})},
        'request_id': request_id + ('-v-' + item['sha256'][:12] if previous else ''),
    }
    try:
        node = target.call('evidence_add', args, channel='ui')
    except Conflict:
        node = target.call('graph_get', {'id': ident})['node']
        if node['data'].get('message_sha256') != item['sha256']:
            raise Conflict('同一消息版本身份冲突，不能覆盖旧导入')
        return node, True
    if previous:
        target.call('relation_add', {'source_id': ident, 'target_id': previous['id'],
                    'relation': 'supersedes', 'basis': '同一聊天片段的原文字节变化；保留旧版本供比较',
                    'request_id': 'ui-source-revision-' + ident}, channel='ui')
    return node, False
STR = {"type": "string"}
INT = {"type": "integer"}
SOURCE = {"type": "object", "properties": {"uri": STR, "locator": STR, "observed_at": STR}, "required": ["uri", "locator"], "additionalProperties": False}
PROPS = {k: STR.copy() for k in "project_root request_id actor id title body query kind status source_id target_id relation basis method thread_id task_id agent started_at observation_source path sha result check decision epistemic_status revisit_when".split()}
PROPS.update({"expected_version": INT, "limit": INT, "offset": INT, "before": INT, "depth": INT, "source": SOURCE, "data": {"type": "object"}, "manifest": {"type": "object"}, "alternatives": {"type": "array", "items": STR}, "evidence_ids": {"type": "array", "items": STR}, "depends_on": {"type": "array", "items": STR}, "checks": {"type": "array", "items": STR}, "relations": {"type": "array", "items": STR}, "files": {"type": "array", "items": SOURCE}})
PROPS["inputs"] = {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "properties": {"id": STR, "expected_version": INT}, "required": ["id", "expected_version"], "additionalProperties": False}}
PROPS["request_id"]["description"] = "Stable unique mutation ID. Reuse the same ID and identical arguments on retry."
PROPS["project_root"]["description"] = "Explicit already-managed project root. Omit to use configured root; verify root before writing. Never initializes another project."
PROPS["expected_version"] = {"type": "integer", "description": "Current node version from graph_get; conflicts require reread."}

# name: description, required, optional. All write paths call the same service.
DEFS = {
 "project_open": ("打开已接入项目，返回项目身份、前沿与状态来源", "", ""),
 "project_status": ("读取项目状态与真正可执行的前沿", "", ""),
 "current_frontier": ("依赖、验证和人工门禁共同计算任务前沿", "", ""),
 "graph_get": ("读取节点或分页子图；原件仍需回读", "", "id kind limit offset"),
 "graph_search": ("按文本/类型搜索已登记节点", "query", "kind limit offset"),
 "graph_neighbors": ("已知图中的有界邻域；不保证图外完整", "id", "depth relations limit offset"),
 "impact_analysis": ("按来源/论据/依赖/产物关系方向查询有界影响候选；非原件资格或状态批准", "id", "depth relations limit offset"),
 "timeline": ("读取持久变更历史，使用 before 分页", "", "before limit"),
 "node_create": ("新建知识/阶段/文件对象；特殊生命周期请用专用工具", "kind title", "id body data source"),
 "node_update": ("按版本修改知识对象或聊天观察；不可修改任务/验证/门禁", "id expected_version", "title body data status source"),
 "relation_add": ("登记有依据的有向关系；depends_on 需要 source 的 expected_version", "source_id relation target_id basis", "expected_version method"),
 "relation_remove": ("撤回关系并保留历史；任务依赖需 source 的 expected_version", "id basis", "expected_version"),
 "task_create": ("建立任务及验收规格/检查；依赖由服务拒绝循环", "title body", "id checks depends_on"),
 "task_claim": ("领取 Ready 任务；并发及依赖不满足时拒绝", "id expected_version", ""),
 "task_progress": ("更新任务进度；todo 可解除手动阻塞/重新打开，不能直接 completed", "id expected_version body", "status"),
 "task_block": ("记录真实阻碍与原因", "id expected_version body", ""),
 "task_complete": ("当前验证、依赖、门禁都满足后才完成任务", "id expected_version", ""),
 "idea_capture": ("保存有长期价值的想法，不自动采集聊天全文", "title", "id body data"),
 "idea_promote": ("保留原想法并原子地创建关联假设或任务", "id expected_version kind title body", "checks"),
 "hypothesis_create": ("新建待检验假设", "title body", "id data"),
 "evidence_add": ("登记可定位来源，自动记录本地原件 SHA；不代替实读", "title source", "id body task_id epistemic_status data"),
 "history_import": ("按已核清单原子导入历史对象及关系；同批次重复调用不增对象，不批准用户验收", "manifest", ""),
 "claim_update": ("更新判断或假设；supported/contradicted 需要证据边", "id expected_version", "title body status data"),
 "decision_record": ("记录选择、其他选项、依据及重评条件", "title body alternatives evidence_ids", "id revisit_when data"),
 "artifact_register": ("登记真实产物及其来源文件", "title source", "id body task_id data"),
 "thread_link": ("把实际聊天映射到任务；runtime 状态只是带时间的观察", "title thread_id task_id", "id body agent status started_at observation_source"),
 "worktree_link": ("登记已有工作树路径；不会创建或切换", "title path", "id body task_id"),
 "commit_link": ("登记完整提交 SHA；不会创建提交，需另行核实 git", "title path sha", "id body task_id"),
 "validation_record": ("登记实际检查回执、被测文件和当前任务版本；非独立审查", "id expected_version check result body source", "files inputs"),
 "human_gate_request": ("登记已明确的人工停止点；此 MCP 不提供批准工具", "title task_id body", "id"),
}

def tools_list():
    out = []
    for name, (description, required, optional) in DEFS.items():
        req = required.split() + ([] if name in READS else ["request_id"])
        keys = req + optional.split() + ["project_root"] + ([] if name in READS else ["actor"])
        out.append({"name": name, "description": description, "inputSchema": {"type": "object", "properties": {k: PROPS[k] for k in keys}, "required": req, "additionalProperties": False}, "annotations": {"readOnlyHint": name in READS, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}})
    return out

def validate_input(name, a):
    if name not in DEFS:
        raise Conflict("未知工具")
    if not isinstance(a, dict):
        raise Conflict("arguments 必须是对象")
    schema = next(t["inputSchema"] for t in tools_list() if t["name"] == name)
    if set(a) - set(schema["properties"]):
        raise Conflict("不支持的字段：" + ",".join(set(a) - set(schema["properties"])))
    for key in schema["required"]:
        if key not in a:
            raise Conflict("缺少字段：" + key)
    for k, v in a.items():
        typ = schema["properties"][k]["type"]
        expected = {"string": str, "integer": int, "object": dict, "array": list}[typ]
        if type(v) is not expected:
            raise Conflict(f"{k} 必须是 {typ}")

def dispatch(store, name, a, channel="mcp"):
    validate_input(name, a)
    if a.get("project_root"):
        store = Store(a["project_root"])
    a = {k: v for k, v in a.items() if k != "project_root"}
    return store.call(name, a, channel=channel)

def mcp(store):
    initialized = False
    for line in sys.stdin:
        req = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
                raise Conflict("Invalid JSON-RPC request")
            method = req.get("method")
            if "id" not in req:
                continue
            if method == "initialize":
                if initialized:
                    raise Conflict("Already initialized")
                initialized = True
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "project-graph-workspace", "version": "0.1.0"}, "instructions": "Project state lives in this service. Verify project root, load current_frontier, link the actual thread, claim a task, record durable evidence/ideas/decisions, validate then complete. Human decisions remain with the user. Registered thread status is not live runtime."}
            elif not initialized:
                raise Conflict("Initialize first")
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools_list()}
            elif method == "tools/call":
                p = req.get("params", {})
                try:
                    value = dispatch(store, p["name"], p.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
                except (Conflict, KeyError, TypeError, ValueError, OSError) as e:
                    result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None, "error": {"code": -32602 if isinstance(e, (Conflict, ValueError)) else -32603, "message": str(e)}}), flush=True)

def serve(store, port, demo=False):
    token = secrets.token_urlsafe(32)
    workspace = Workspace(store)
    runtime = ManagedRuntime()
    own_project = store.call("project_open")["project"]
    own_id = own_project["data"]["project_id"]

    def inside_demo_path(value):
        path = Path(value).expanduser()
        if path != store.root and store.root not in path.parents:
            return False
        resolved = path.resolve()
        return resolved == store.root or store.root in resolved.parents

    def demo_target(project_id):
        if demo:
            if project_id != own_id:
                raise Conflict("Demo 只包含示例项目图")
            return store
        return workspace.resolve(project_id)

    def demo_file_guard(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"uri", "path"} and isinstance(item, str) and item.startswith("/"):
                    if not inside_demo_path(item):
                        raise Conflict("Demo 不读取包外原件；请将新原件放入解压目录")
                demo_file_guard(item)
        elif isinstance(value, list):
            for item in value:
                demo_file_guard(item)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, status=200, content_type="application/json; charset=utf-8"):
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers(); self.wfile.write(raw)

        def safe_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.safe_host():
                return self.send({"error": "Host rejected"}, 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                target = demo_target(q["project"][0]) if "project" in q else store
                if u.path == "/api/session":
                    return self.send({"csrf": token, "root": str(store.root), "kinds": KINDS,
                                      "relations": sorted(RELATIONS), "demo_mode": demo})
                if u.path == "/api/projects":
                    return self.send({"projects": ([{"id": own_id, "root": str(store.root),
                                                     "name": own_project["title"], "available": True}]
                                                    if demo else list(workspace.entries().values()))})
                if u.path == "/api/runtime/context":
                    if "project" not in q or not q.get("task", [""])[0]:
                        raise Conflict("上下文预览必须明确项目与任务")
                    return self.send(runtime.context(target, q["task"][0]))
                if u.path == "/api/runtime":
                    if demo:
                        raise Conflict("Demo 不连接实时聊天")
                    return self.send(runtime.status(target))
                if u.path == "/api/code":
                    op = q.get("op", ["status"])[0]
                    if op == "task_context" and "project" not in q:
                        raise Conflict("任务代码上下文必须明确项目身份")
                    if demo and op == "task_context":
                        raise Conflict("Demo 不加载宿主任务代码上下文")
                    return self.send(CodeIndex(target).query(op, {
                        "task_id": q.get("task_id", [""])[0],
                        "symbol_ids": q.get("symbol_id", []),
                        "query": q.get("query", [""])[0], "path": q.get("path", [""])[0],
                        "id": q.get("id", [""])[0], "limit": q.get("limit", [30])[0],
                        "offset": q.get("offset", [0])[0], "depth": q.get("depth", [2])[0],
                        "status": q.get("status", [""])[0],
                        "include_source": q.get("include_source", ["0"])[0],
                        "budget_bytes": q.get("budget_bytes", [8192])[0],
                        "budget_tokens": q.get("budget_tokens", [1000])[0]}))
                if demo and u.path in {"/api/project-preview", "/api/discover-threads", "/api/thread-preview",
                                       "/api/adoption-preview", "/api/import-preview", "/api/thread-observation"}:
                    raise Conflict("Demo 不连接宿主项目、聊天或历史导入源")
                if u.path == "/api/project-preview":
                    return self.send(workspace.attachment_preview(q.get("path", [""])[0]))
                if u.path == "/api/discover-threads":
                    return self.send(discover_threads(target, q.get("search", [""])[0],
                                                      q.get("cursor", [None])[0], int(q.get("limit", [30])[0])))
                if u.path == "/api/thread-observation":
                    return self.send(thread_observation(target, q.get("id", [""])[0], int(q.get("expected_version", [0])[0])))
                if u.path == "/api/thread-preview":
                    page = preview_thread(target, q.get("thread_id", [""])[0], q.get("cursor", [None])[0])
                    known, incomplete = project_nodes(target)
                    visible = 0
                    registered = 0
                    for turn in page["turns"]:
                        for item in turn["items"]:
                            visible += 1
                            related = [n for n in known if n["data"].get("thread_id") == page["thread"]["id"]
                                       and n["data"].get("turn_id") == turn["id"]
                                       and n["data"].get("item_id") == item["id"]]
                            item["registered"] = [{"id": n["id"], "kind": n["kind"], "status": n["status"]}
                                                  for n in related]
                            registered += bool(related)
                    page["page_coverage"] = {"visible_items": visible, "registered_items": registered,
                                             "has_older": bool(page.get("next_cursor"))}
                    page["coverage"] = "仅当前轮次、可显示的用户/助手文本；有更早游标或截断时不代表整段聊天已整理" + ("；项目对象超过5000，登记状态检索不完整" if incomplete else "")
                    return self.send(page)
                if u.path == "/api/source-inventory":
                    return self.send(source_inventory(target, int(q.get('offset', [0])[0]),
                                                      int(q.get('limit', [40])[0]), demo=demo))
                if u.path == "/api/adoption-preview":
                    thread_id, turn_id, item_id = (q.get(k, [""])[0] for k in ("thread_id", "turn_id", "item_id"))
                    kind = q.get("kind", [""])[0]
                    if kind not in {"Requirement", "Task", "Idea", "Blocker", "Decision", "Artifact"}:
                        raise Conflict("请选择支持的候选类型")
                    item = thread_message(target, thread_id, turn_id, item_id)
                    ident, previous = chat_identity(target, item, kind)
                    source_ident, previous_source = chat_identity(target, item)
                    try:
                        existing = target.call("graph_get", {"id": ident})["node"]
                    except Conflict:
                        existing = None
                    title = q.get("title", [""])[0].strip().casefold()
                    known, incomplete = project_nodes(target)
                    related, match_count = candidate_matches(known, thread_id, turn_id, item_id, kind, title, ident)
                    return self.send({"candidate_id": ident, "source_id": source_ident,
                                      "message_sha256": item["sha256"], "role": item["role"],
                                      "message_kind": item.get("message_kind"),
                                      "excerpt": item["text"][:4000], "truncated": len(item["text"]) > 4000,
                                      "existing": {"id": existing["id"], "title": existing["title"], "status": existing["status"]} if existing else None,
                                      "revision_of": previous["id"] if previous else None,
                                      "previous_sha256": previous_source["data"].get("message_sha256") if previous_source else None,
                                      "previous_excerpt": previous_source["body"][:1000] if previous_source else None,
                                      "possible_matches": related, "match_count": match_count,
                                      "match_scope": "仅当前项目" + ("前5000个" if incomplete else "全部") + "已登记对象；同片段、完全同标题或同类型标题双字词面相近。只提供最多20条核对线索，不能证明语义相同或矛盾；须回读双方来源、状态与适用条件",
                                      "rule": "只是候选；任务待验证，决定需依据，产物需真实文件；助手提问及选项不等于用户回答，用户验收不自动批准"})
                if u.path == "/api/import-preview":
                    return self.send(workspace.preview(target))
                if u.path == "/api/state":
                    return self.send(target.snapshot())
                if u.path == "/api/timeline":
                    return self.send(target.call("timeline", {"before": int(q["before"][0])} if "before" in q else {}))
                if u.path == "/api/source":
                    n = target.call("graph_get", {"id": q.get("id", [""])[0]})["node"]
                    source = n["data"].get("original_source" if q.get("version", [""])[0] == "current" else "source", {})
                    uri = source.get("uri", "")
                    if demo and (uri.startswith("codex://") or
                                 (uri.startswith("/") and not inside_demo_path(uri))):
                        raise Conflict("原件未随 Demo 打包；仅保留图中历史路径与定位")
                    if uri.startswith("codex://threads/"):
                        d = n["data"]
                        if not all(d.get(k) for k in ("thread_id", "turn_id", "item_id")):
                            raise Conflict("此记录缺少聊天片段定位")
                        item = thread_message(target, d["thread_id"], d["turn_id"], d["item_id"])
                        if item["sha256"] != d.get("message_sha256"):
                            raise Conflict("聊天原文已变化；保留导入时记录，需重新核对")
                        return self.send({"path": uri, "locator": source.get("locator"), "text": item["text"], "registered_sha256": item["sha256"],
                                          "current_sha256": item["sha256"], "freshness": "match",
                                          "observed_at": now(), "hash_scope": "chat_item"})
                    if not uri.startswith("/"):
                        raise Conflict("此对象没有可预览的本地原件")
                    return self.send(local_source_preview(source, n["data"].get("message_id")))
                files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/visual.js": "visual.js", "/delivery.js": "delivery.js", "/fonts/ui-sc.woff2": "fonts/ui-sc.woff2"}
                if u.path not in files:
                    return self.send({"error": "Not found"}, 404)
                p = HERE / "web" / files[u.path]
                return self.send(p.read_bytes(), content_type=("font/woff2" if p.suffix == ".woff2" else (mimetypes.guess_type(p.name)[0] or "text/plain") + "; charset=utf-8"))
            except (Conflict, BridgeError, ValueError, KeyError, OSError, UnicodeError) as e:
                return self.send({"error": str(e)}, 400)

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if not self.safe_host() or (origin and origin not in allowed) or not secrets.compare_digest(self.headers.get("X-PGW-CSRF", ""), token):
                return self.send({"error": "请求来源或会话校验失败，请刷新工作区"}, 403)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 1024 * 1024:
                    raise Conflict("请求大小无效")
                if self.headers.get_content_type() != "application/json":
                    raise Conflict("需要 application/json")
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/runtime":
                    if demo:
                        raise Conflict("Demo 不连接实时聊天")
                    if not body.get("project_id"):
                        raise Conflict("运行操作必须明确项目身份")
                    target = workspace.resolve(body["project_id"])
                    return self.send({"result": runtime.action(target, body)})
                if demo and self.path in {"/api/attach-project", "/api/link-thread", "/api/import-message",
                                             "/api/adopt-message", "/api/import"}:
                    raise Conflict("Demo 不连接宿主项目、聊天或历史导入源")
                if self.path == "/api/attach-project":
                    result = workspace.attach(body.get("root", ""), body.get("name", ""), body.get("goal", ""))
                    return self.send({"result": result})
                if not body.get("project_id"):
                    raise Conflict("写入必须明确选择项目；请刷新旧页面")
                target = demo_target(body["project_id"])
                if self.path == "/api/link-thread":
                    thread_id, task_id = body.get("thread_id", ""), body.get("task_id", "")
                    found = thread_metadata(target, thread_id)
                    task = target.call("graph_get", {"id": task_id})["node"]
                    if task["kind"] not in {"Task", "Subtask", "PlanItem"}:
                        raise Conflict("聊天只能关联任务或计划项")
                    snap = target.snapshot()
                    existing = next((n for n in snap["nodes"] if n["kind"] == "Thread" and n["data"].get("thread_id") == thread_id), None)
                    if existing is None:
                        existing = target.call("thread_link", {
                            "title": found.get("name") or found.get("preview") or "未命名聊天", "thread_id": thread_id, "task_id": task_id,
                            "agent": "Codex", "status": "unknown", "observation_source": "Codex app-server 独立只读发现",
                            "request_id": body.get("request_id") or str(uuid.uuid4()),
                        }, channel="ui")
                    edge_id = "edge-" + __import__('service').digest([task_id, "executed_by", existing["id"]])[:20]
                    edges = target.call("graph_get", {"id": existing["id"]})["edges"]
                    if not any(e["id"] == edge_id for e in edges):
                        target.call("relation_add", {"source_id": task_id, "target_id": existing["id"],
                                    "relation": "executed_by", "basis": "用户从真实聊天发现结果关联",
                                    "request_id": "ui-thread-edge-" + edge_id}, channel="ui")
                    return self.send({"result": {"node_id": existing["id"], "thread_id": thread_id}})
                if self.path == "/api/import-message":
                    thread_id, turn_id, item_id = (body.get(k, "") for k in ("thread_id", "turn_id", "item_id"))
                    item = thread_message(target, thread_id, turn_id, item_id)
                    node, existed = ensure_chat_source(target, item, body.get("request_id") or "ui-message-" + chat_source_id(thread_id, turn_id, item_id))
                    return self.send({"result": {"node_id": node["id"], "existing": existed}})
                if self.path == "/api/adopt-message":
                    thread_id, turn_id, item_id = (body.get(k, "") for k in ("thread_id", "turn_id", "item_id"))
                    kind = body.get("kind")
                    if kind not in {"Requirement", "Task", "Idea", "Blocker", "Decision", "Artifact"}:
                        raise Conflict("不支持的候选类型")
                    disposition = body.get("disposition", "adopt")
                    if disposition not in {"adopt", "defer", "exclude"}:
                        raise Conflict("候选处置须为采用、暂缓或排除")
                    item = thread_message(target, thread_id, turn_id, item_id)
                    if body.get("message_sha256") != item["sha256"]:
                        raise Conflict("预览后原文已变化，请重新核对")
                    title, explanation = body.get("title", "").strip(), body.get("body", "").strip()
                    basis = body.get("basis", "").strip()
                    if not title or len(title) > 300 or not explanation or not basis:
                        raise Conflict("标题、具体内容和采用依据均必填")
                    source_node, _ = ensure_chat_source(target, item, "ui-message-" + chat_source_id(thread_id, turn_id, item_id))
                    ident, previous = chat_identity(target, item, kind)
                    locator = f"turn/{turn_id}/item/{item_id}"
                    source = {"uri": f"codex://threads/{thread_id}", "locator": locator}
                    sources = []
                    if kind == "Artifact":
                        path = Path(body.get("artifact_path", "")).expanduser()
                        if not path.is_absolute() or not path.is_file():
                            raise Conflict("产物必须提供实际存在的本地文件")
                        source = {"uri": str(path.resolve()), "locator": body.get("artifact_locator") or "完整文件"}
                        sources = [{"uri": source["uri"], "sha256": file_hash(source["uri"])}]
                    alternatives = [v.strip() for v in body.get("alternatives", "").splitlines() if v.strip()]
                    if kind == "Decision" and not alternatives:
                        raise Conflict("决定需说明其他选项或处置")
                    deps = [v.strip() for v in body.get("depends_on", "").replace('，', ',').split(',') if v.strip()]
                    if kind != "Task" and deps:
                        raise Conflict("只有任务候选可以声明前置依赖")
                    data = {"original_kind": kind, "original_status": body.get("original_status", "历史片段候选，未独立核实").strip() or "历史片段候选，未独立核实",
                            "message_role": item["role"], "message_sha256": item["sha256"],
                            "thread_id": thread_id, "turn_id": turn_id, "item_id": item_id,
                            "adoption_basis": basis, "import_disposition": disposition, "user_acceptance": "NOT_CONFIRMED",
                            **({"revises_node_id": previous["id"]} if previous else {})}
                    if kind == "Decision":
                        data.update({"alternatives": alternatives, "revisit_when": body.get("revisit_when", "新证据或范围变化")})
                    if kind == "Artifact":
                        data["chat_source"] = {"uri": f"codex://threads/{thread_id}", "locator": locator}
                    batch = "chat-adopt-" + digest([thread_id, turn_id, item_id, kind]
                                                   + ([item["sha256"]] if previous else []))[:24]
                    manifest = {"project_root": str(target.root), "batch_id": batch, "sources": sources,
                                "nodes": [{"id": ident, "kind": kind, "title": title, "body": explanation,
                                           "state": "reported", "source": source, "data": data}],
                                "edges": [{"source": ident, "target": source_node["id"], "relation": "derived_from", "basis": basis}]
                                + ([{"source": ident, "target": dep, "relation": "depends_on", "basis": "用户在导入候选时声明的前置"} for dep in deps] if kind == "Task" else [])
                                + ([{"source": ident, "target": previous["id"], "relation": "supersedes", "basis": "同一聊天片段新原文；保留旧候选与判断"}] if previous else [])}
                    try:
                        existing = target.call("graph_get", {"id": ident})["node"]
                    except Conflict:
                        existing = None
                    if existing:
                        if existing["data"].get("import_batch") != batch or existing["data"].get("import_fingerprint") != digest(manifest):
                            raise Conflict("候选已存在但内容不同；请核对差异，不覆盖旧对象")
                        return self.send({"result": {"node_id": ident, "batch_id": batch, "imported": 0, "existing": 1}})
                    result = target.call("history_import", {"manifest": manifest,
                                                            "request_id": "ui-adopt-" + batch}, channel="ui")
                    return self.send({"result": {"node_id": ident, "batch_id": batch, **result}})
                if self.path == "/api/revoke-adoption":
                    ident = body.get("node_id", "")
                    reason = body.get("reason", "").strip()
                    disposition = body.get("disposition", "defer")
                    if not reason or len(reason) > 2000 or disposition not in {"defer", "exclude"}:
                        raise Conflict("撤回须说明理由并选择暂缓或排除")
                    node = target.call("graph_get", {"id": ident})["node"]
                    if node["data"].get("import_disposition") != "adopt" or not node["data"].get("thread_id"):
                        raise Conflict("只有已采用的聊天候选可从此入口撤回")
                    if node["status"] in {"deferred", "cancelled"}:
                        raise Conflict("此候选已暂缓或排除；请回读历史")
                    if node["version"] != body.get("expected_version"):
                        raise Conflict("候选版本已变化，请刷新后重新核对")
                    new_status = "deferred" if disposition == "defer" else "cancelled"
                    args = {"id": ident, "expected_version": node["version"], "status": new_status,
                            "request_id": body.get("request_id") or "ui-revoke-" + digest([ident, node["version"], disposition, reason])[:24]}
                    if node["kind"] in {"Task", "Subtask", "PlanItem"}:
                        args["body"] = node["body"] + "\n\n采用撤回（" + disposition + "）：" + reason
                        result = target.call("task_progress", args, channel="ui")
                    else:
                        args["data"] = {"adoption_reversal": {"disposition": disposition, "reason": reason}}
                        result = target.call("node_update", args, channel="ui")
                    return self.send({"result": {"node_id": ident, "status": result["status"]}})
                if self.path == "/api/import":
                    m = workspace.manifest(target)
                    if body.get("batch_id") != m["batch_id"]:
                        raise Conflict("导入预览已变化，请重新核对")
                    if body.get("fingerprint") != digest(m):
                        raise Conflict("预览内容已变化，请重新核对")
                    return self.send({"result": target.call("history_import", {"manifest": m, "request_id": "ui-import-" + m["batch_id"]}, channel="ui")})
                if self.path != "/api/call":
                    return self.send({"error": "Not found"}, 404)
                op, args = body["name"], body.get("arguments", {})
                if demo:
                    if op in {"history_import", "thread_link", "worktree_link", "commit_link"}:
                        raise Conflict("Demo 不连接宿主项目、聊天或工作树")
                    demo_file_guard(args)
                if args.get("project_root") and Path(args["project_root"]).resolve() != target.root:
                    raise Conflict("项目路由与操作目录不一致")
                # Human decisions are absent from the MCP and CLI tool catalogue.
                if op == "human_gate_resolve":
                    result = target.call(op, args, channel="ui")
                else:
                    if args.get("project_root") and Path(args["project_root"]).resolve() != target.root:
                        raise Conflict("当前界面只操作已打开的项目")
                    result = dispatch(target, op, args, channel="ui")
                return self.send({"result": result})
            except (Conflict, BridgeError, ValueError, KeyError, TypeError, OSError) as e:
                return self.send({"error": str(e)}, 409)
    server = BoundedHTTPServer(("127.0.0.1", port), Handler)
    print(f"Codex Project Manager http://127.0.0.1:{server.server_port} | {store.root}", flush=True)
    try:
        server.serve_forever()
    finally:
        runtime.close()
        server.server_close()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=str(HERE))
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init"); init.add_argument("--name", required=True); init.add_argument("--goal", required=True)
    web = commands.add_parser("serve"); web.add_argument("--port", type=int, default=8766)
    web.add_argument("--demo", action="store_true", help="仅操作包内图副本，不读取宿主项目、聊天与原件")
    commands.add_parser("mcp"); commands.add_parser("tools")
    call = commands.add_parser("call"); call.add_argument("name"); call.add_argument("arguments", nargs="?", default="{}")
    call.add_argument("--args-file", help="读取有界 JSON 对象，避免 shell 转义")
    code = commands.add_parser("code"); code.add_argument("operation"); code.add_argument("arguments", nargs="?", default="{}")
    code.add_argument("--args-file")
    runtime = commands.add_parser("runtime", help="通过现有持久服务操作受管聊天；不自动审批")
    runtime.add_argument("operation", choices=["status", "context", "create", "continue", "steer", "interrupt", "reconcile"])
    runtime.add_argument("arguments", nargs="?", default="{}")
    runtime.add_argument("--args-file")
    runtime.add_argument("--port", type=int, default=8766)
    threads = commands.add_parser("threads", help="单个已关联聊天的有界只读元数据；不控制桌面")
    threads.add_argument("operation", choices=["observe"])
    threads.add_argument("arguments", nargs="?", default="{}")
    threads.add_argument("--args-file")
    threads.add_argument("--port", type=int, default=8766)
    source = commands.add_parser("source", help="通过现有8766只读清单与原件；需要明确--root")
    source.add_argument("operation", choices=["inventory", "preview"])
    source.add_argument("arguments", nargs="?", default="{}")
    source.add_argument("--args-file")
    source.add_argument("--port", type=int, default=8766)
    args = p.parse_args(); store = Store(args.root)
    try:
        if args.command == "init":
            result = store.init(args.name, args.goal)
        elif args.command == "serve":
            if args.demo and store.root != HERE:
                raise Conflict("Demo 项目根必须是服务源码所在目录")
            store.call("project_open"); serve(store, args.port, demo=args.demo); return
        elif args.command == "mcp":
            mcp(store); return
        elif args.command == "tools":
            result = tools_list()
        elif args.command == "code":
            if args.operation == "refresh":
                if not any(a == "--root" or a.startswith("--root=") for a in sys.argv[1:]):
                    raise Conflict("刷新CLI需要显式--root；避免隐式跨项目更新")
                result = code_refresh_cli(store, cli_arguments(args))
            else:
                result = CodeIndex(store).query(args.operation, cli_arguments(args))
        elif args.command == "threads":
            if not any(a == "--root" or a.startswith("--root=") for a in sys.argv[1:]):
                raise Conflict("聊天观察CLI需要显式--root")
            result = threads_cli(store, args.operation, cli_arguments(args), args.port)
        elif args.command == "source":
            if not any(a == "--root" or a.startswith("--root=") for a in sys.argv[1:]):
                raise Conflict("来源CLI需要显式--root，避免隐式操作另一个项目")
            result = source_cli(store, args.operation, cli_arguments(args), args.port)
        elif args.command == "runtime":
            result = runtime_cli(store, args.operation, cli_arguments(args), args.port)
        else:
            payload = cli_arguments(args)
            if args.name not in READS:
                payload.setdefault("request_id", str(uuid.uuid4()))
            result = dispatch(store, args.name, payload, channel="cli")
        # Task context budgets measure the complete JSON bytes, including metadata.
        indent = None if args.command == "code" and args.operation == "task_context" else 2
        print(json.dumps(result, ensure_ascii=False, indent=indent))
    except (Conflict, ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()
