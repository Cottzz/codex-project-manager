"""Explicit local project registry. No Codex database writes or runtime control."""
import json
import os
from pathlib import Path
from service import Store, Conflict, file_hash, digest

class Workspace:
    def __init__(self, store):
        self.home = store
        self.path = store.folder / 'workspace.json'

    def entries(self):
        own = json.loads((self.home.folder / 'project.json').read_text())
        entries = {own['project_id']: {'id': own['project_id'], 'root': str(self.home.root), 'name': own['name']}}
        if self.path.exists():
            for e in json.loads(self.path.read_text())['projects']:
                if e['id'] in entries and entries[e['id']]['root'] != e['root']:
                    raise Conflict('项目身份重复')
                entries[e['id']] = e
        for entry in entries.values():
            entry['available'] = Path(entry['root']).is_dir()
        return entries

    def attachment_preview(self, root):
        if not isinstance(root, str) or not root.strip() or not Path(root).expanduser().is_absolute():
            raise Conflict('请输入项目目录的绝对路径')
        path = Path(root).expanduser().resolve()
        if not path.is_dir():
            raise Conflict('项目目录不存在或不是文件夹')
        existing = next((e for e in self.entries().values() if Path(e['root']).resolve() == path), None)
        project_file = path / '.project-graph' / 'project.json'
        identity = json.loads(project_file.read_text()) if project_file.is_file() else None
        if identity and not {'project_id', 'name'} <= identity.keys():
            raise Conflict('现有项目身份文件不完整')
        if identity and Path(identity.get('root', '/')).resolve() != path:
            raise Conflict('状态记录的原项目目录与当前目录不一致；请先按迁移说明核对')
        if identity and identity['project_id'] in self.entries() and not existing:
            raise Conflict('项目 ID 已登记到其他目录，请先核对迁移')
        return {'root': str(path), 'existing': existing,
                'managed': bool(identity), 'identity': identity,
                'action': 'already_registered' if existing else 'reuse' if identity else 'initialize',
                'notice': '初始化仅建立独立空图；原计划、聊天和验收不会自动认定完成。'}

    def attach(self, root, name='', goal=''):
        preview = self.attachment_preview(root)
        if preview['existing']:
            return preview['existing']
        store = Store(preview['root'])
        if not preview['managed']:
            if not name.strip() or not goal.strip():
                raise Conflict('新项目需要名称与目标；原有资料不会自动导入')
            store.init(name.strip(), goal.strip())
        return self.register(preview['root'])

    def resolve(self, project_id):
        e = self.entries().get(project_id)
        if not e:
            raise Conflict('项目未登记到当前工作区')
        store = Store(e['root'])
        identity = store.call('project_open')['project']['data']['project_id']
        if identity != project_id:
            raise Conflict('项目目录与登记身份不一致')
        return store

    def register(self, root):
        # Called only by the local operator, after authorized initialization/import.
        store = Store(root)
        n = store.call('project_open')['project']
        with self.home.locked():
            entries = self.entries()
            for entry in entries.values():
                if Path(entry['root']).resolve() == store.root and entry['id'] != n['data']['project_id']:
                    raise Conflict('同一项目目录已有不同身份')
            entries[n['data']['project_id']] = {'id': n['data']['project_id'], 'name': n['title'], 'root': str(store.root)}
            p = self.path.with_suffix('.tmp')
            p.write_text(json.dumps({'projects': [{k: e[k] for k in ('id', 'name', 'root')} for e in entries.values()]}, ensure_ascii=False, indent=2))
            os.replace(p, self.path)
        return entries[n['data']['project_id']]

    def manifest(self, store):
        p = store.folder / 'imports' / 'preview.json'
        if not p.is_file():
            raise Conflict('此项目尚无已核对的历史导入预览')
        m = json.loads(p.read_text())
        if m['project_root'] != str(store.root):
            raise Conflict('导入预览目录不匹配')
        return m

    def preview(self, store):
        m = self.manifest(store)
        snap = store.snapshot()
        ids = {n['id']: n for n in snap['nodes']}
        stale = [s['uri'] for s in m['sources'] if file_hash(s['uri']) != s['sha256']]
        conflicts = [n['id'] for n in m['nodes'] if n['id'] in ids and ids[n['id']]['data'].get('import_batch') != m['batch_id']]
        return {**m, 'fingerprint': digest(m), 'stale_sources': stale, 'conflicts': conflicts, 'already_imported': sum(n['id'] in ids for n in m['nodes'])}
