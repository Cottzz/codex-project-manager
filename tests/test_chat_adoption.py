import json
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from service import Store


APP = Path(__file__).resolve().parents[1]


class ChatAdoptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root)
        self.store.init('test', 'isolated')
        self.project_id = self.store.call('project_open')['project']['data']['project_id']
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        script = '''import sys, pgw, hashlib
from pathlib import Path
from service import Store
from codex_bridge import BridgeError
root, port = sys.argv[1], int(sys.argv[2])
def fake(store, tid, turn, item):
    if str(store.root) != root or (tid,turn,item) != ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','turn-1','item-1'):
        raise BridgeError('wrong scope')
    changed = Path(root, 'changed-message.txt')
    text = changed.read_text() if changed.exists() else 'reported historical work'
    sha = hashlib.sha256(text.encode()).hexdigest() if changed.exists() else 'a'*64
    return {'thread_id':tid,'turn_id':turn,'item_id':item,'role':'assistant',
            'text':text,'sha256':sha,'runtime_path':'test-rollout.jsonl'}
pgw.thread_message=fake
def preview(store, tid, cursor=None):
    if str(store.root) != root or tid != 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa':
        raise BridgeError('wrong preview scope')
    return {'thread':{'id':tid,'title':'isolated','cwd':root,'source':'test','status':'notLoaded'},
            'turns':[{'id':'turn-1','status':'completed','items':[{'id':'item-1','role':'assistant','text':'reported historical work','truncated':False}]}],
            'next_cursor':'older', 'warning':None}
pgw.preview_thread=preview
pgw.serve(Store(root), port)
'''
        self.proc = subprocess.Popen([sys.executable, '-u', '-c', script, str(self.root), str(self.port)],
                                     cwd=APP, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertIn('http://', self.proc.stdout.readline())
        self.addCleanup(self._stop)
        self.base = f'http://127.0.0.1:{self.port}'
        self.csrf = self.get('/api/session')['csrf']

    def _stop(self):
        self.proc.terminate()
        _, errors = self.proc.communicate(timeout=5)
        if errors:
            print(errors, file=sys.stderr)

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path))

    def post(self, path, data):
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json', 'X-PGW-CSRF': self.csrf})
        return json.load(urllib.request.urlopen(req))

    def test_candidate_preview_adoption_replay_and_source(self):
        ids = {'thread_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','turn_id':'turn-1','item_id':'item-1'}
        query = urllib.parse.urlencode({**ids, 'kind': 'Task', 'project': self.project_id})
        preview = self.get('/api/adoption-preview?' + query)
        self.assertIsNone(preview['existing'])
        request = {'project_id':self.project_id, **ids, 'kind':'Task', 'title':'candidate task',
                   'body':'Scope still needs verification', 'basis':'user selected after reading source',
                   'message_sha256':preview['message_sha256']}
        first = self.post('/api/adopt-message', request)['result']
        again = self.post('/api/adopt-message', request)['result']
        self.assertEqual(first['imported'], 1)
        self.assertEqual(again['imported'], 0)
        node = self.store.call('graph_get', {'id': first['node_id']})['node']
        self.assertEqual(node['status'], 'pending_validation')
        self.assertEqual(node['data']['user_acceptance'], 'NOT_CONFIRMED')
        self.assertEqual(node['data']['original_kind'], 'Task')
        page = self.get('/api/thread-preview?' + urllib.parse.urlencode({'project':self.project_id,'thread_id':ids['thread_id']}))
        self.assertEqual({n['kind'] for n in page['turns'][0]['items'][0]['registered']}, {'Evidence','Task'})
        self.assertIn('仅当前轮次', page['coverage'])
        self.assertEqual(page['page_coverage'], {'visible_items':1,'registered_items':1,'has_older':True})
        source = self.get('/api/source?' + urllib.parse.urlencode({'project':self.project_id,'id':preview['source_id']}))
        self.assertEqual(source['text'], 'reported historical work')
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/adopt-message', {**request, 'title':'silent overwrite'})
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/adopt-message', {**request, 'message_sha256':'wrong'})

    def test_artifact_requires_real_file_and_decision_alternatives(self):
        ids = {'thread_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','turn_id':'turn-1','item_id':'item-1'}
        base = {'project_id':self.project_id, **ids, 'title':'candidate', 'body':'bounded',
                'basis':'read actual source', 'message_sha256':'a'*64}
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/adopt-message', {**base, 'kind':'Artifact', 'artifact_path':str(self.root/'missing')})
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/adopt-message', {**base, 'kind':'Decision'})
        file = self.root/'proof.txt'
        file.write_text('actual file')
        saved = self.post('/api/adopt-message', {**base, 'kind':'Artifact','artifact_path':str(file)})['result']
        self.assertEqual(self.store.call('graph_get', {'id':saved['node_id']})['node']['kind'], 'Artifact')
        decided = self.post('/api/adopt-message', {**base, 'kind':'Decision',
                                                   'alternatives':'defer and recheck'})['result']
        node = self.store.call('graph_get', {'id':decided['node_id']})['node']
        self.assertEqual(node['status'], 'open')
        self.assertEqual(node['data']['alternatives'], ['defer and recheck'])
        idea = self.post('/api/adopt-message', {**base, 'kind':'Idea', 'disposition':'defer'})['result']
        self.assertEqual(self.store.call('graph_get', {'id':idea['node_id']})['node']['status'], 'deferred')
        requirement = self.post('/api/adopt-message', {**base, 'kind':'Requirement', 'disposition':'exclude'})['result']
        self.assertEqual(self.store.call('graph_get', {'id':requirement['node_id']})['node']['status'], 'cancelled')

    def test_changed_original_is_new_revision_and_old_result_is_preserved(self):
        ids = {'thread_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','turn_id':'turn-1','item_id':'item-1'}
        base = {'project_id':self.project_id, **ids, 'kind':'Idea', 'title':'old interpretation',
                'body':'old source meaning', 'basis':'old source read', 'message_sha256':'a'*64}
        first = self.post('/api/adopt-message', base)['result']
        (self.root/'changed-message.txt').write_text('revised original content')
        query = urllib.parse.urlencode({**ids,'kind':'Idea','title':'revised interpretation','project':self.project_id})
        preview = self.get('/api/adoption-preview?' + query)
        self.assertEqual(preview['revision_of'], first['node_id'])
        self.assertEqual(preview['previous_excerpt'], 'reported historical work')
        self.assertNotEqual(preview['candidate_id'], first['node_id'])
        self.assertTrue(preview['possible_matches'])
        revised = self.post('/api/adopt-message', {**base, 'title':'revised interpretation',
                            'body':'updated source meaning', 'basis':'compared old and revised original',
                            'message_sha256':preview['message_sha256']})['result']
        self.assertEqual(revised['imported'], 1)
        old = self.store.call('graph_get', {'id':first['node_id']})['node']
        self.assertEqual(old['title'], 'old interpretation')
        new = self.store.call('graph_get', {'id':revised['node_id']})
        self.assertEqual(new['node']['data']['revises_node_id'], first['node_id'])
        self.assertTrue(any(e['relation']=='supersedes' and e['target']==first['node_id'] for e in new['edges']))
        repeated = self.post('/api/adopt-message', {**base, 'title':'revised interpretation',
                             'body':'updated source meaning', 'basis':'compared old and revised original',
                             'message_sha256':preview['message_sha256']})['result']
        self.assertEqual(repeated['imported'], 0)

    def test_revoke_candidate_is_append_only_and_requires_current_version(self):
        ids = {'thread_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','turn_id':'turn-1','item_id':'item-1'}
        base = {'project_id':self.project_id, **ids, 'kind':'Task', 'title':'candidate task',
                'body':'not verified', 'basis':'source read', 'message_sha256':'a'*64}
        ident = self.post('/api/adopt-message', base)['result']['node_id']
        node = self.store.call('graph_get', {'id':ident})['node']
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/revoke-adoption', {'project_id':self.project_id,'node_id':ident,
                       'expected_version':node['version']+1,'disposition':'defer','reason':'new conflict'})
        seq = self.store.snapshot()['sequence']
        out = self.post('/api/revoke-adoption', {'project_id':self.project_id,'node_id':ident,
                        'expected_version':node['version'],'disposition':'defer','reason':'new conflict'})['result']
        self.assertEqual(out['status'], 'deferred')
        self.assertEqual(self.store.snapshot()['sequence'], seq+1)
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/revoke-adoption', {'project_id':self.project_id,'node_id':ident,
                       'expected_version':node['version'],'disposition':'defer','reason':'new conflict'})

    def test_project_source_inventory_discloses_changes_and_read_scope(self):
        plan = self.root/'PLAN.md'
        plan.write_text('current plan')
        local = self.root/'proof.txt'
        local.write_text('original source')
        self.store.call('evidence_add', {'id':'EV-SOURCE','title':'source',
                        'source':{'uri':str(local),'locator':'all'},'request_id':'source-once'})
        local.write_text('changed source')
        query = urllib.parse.urlencode({'project':self.project_id,'offset':0,'limit':1})
        first = self.get('/api/source-inventory?' + query)
        self.assertEqual(first['total'], 2)
        self.assertEqual(len(first['files']), 1)
        second = self.get('/api/source-inventory?' + urllib.parse.urlencode({
            'project':self.project_id,'offset':first['next_offset'],'limit':1}))
        rows = {row['uri']:row for row in first['files']+second['files']}
        self.assertEqual(rows[str(local)]['status'], 'changed')
        self.assertEqual(rows[str(local)]['reference_count'], 1)
        self.assertEqual(rows[str(plan)]['status'], 'unregistered_hash')
        self.assertIn('不证明原件已阅读', first['scope'])

    def test_candidate_preview_shows_cross_type_and_lexical_review_leads(self):
        self.store.call('task_create', {'id':'TASK-EXISTING','title':'候选来源核查',
                        'body':'Existing task scope; compare originals before adoption',
                        'request_id':'existing-task'})
        ids = {'thread_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','turn_id':'turn-1','item_id':'item-1'}
        cross = self.get('/api/adoption-preview?' + urllib.parse.urlencode({
            **ids,'project':self.project_id,'kind':'Decision','title':'候选来源核查'}))
        match = next(n for n in cross['possible_matches'] if n['id']=='TASK-EXISTING')
        self.assertIn('标题完全相同', match['reasons'])
        self.assertEqual(match['kind'], 'Task')
        lexical = self.get('/api/adoption-preview?' + urllib.parse.urlencode({
            **ids,'project':self.project_id,'kind':'Task','title':'候选来源核查更新'}))
        match = next(n for n in lexical['possible_matches'] if n['id']=='TASK-EXISTING')
        self.assertIn('同类型标题词面相近', match['reasons'])
        self.assertIn('不能证明语义相同', lexical['match_scope'])


if __name__ == '__main__':
    unittest.main()
