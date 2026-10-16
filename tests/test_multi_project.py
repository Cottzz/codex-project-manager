import json
import hashlib
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from pathlib import Path
from service import Store, Conflict, file_hash, digest
from workspace import Workspace
from pgw import local_source_preview

APP = Path(__file__).resolve().parents[1] / 'pgw.py'
class MultiProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = [Store(Path(self.tmp.name)/n) for n in ('A','B')]
        for s in (self.a,self.b):
            s.root.mkdir();s.init(s.root.name,'test')
        self.ws=Workspace(self.a);self.ws.register(self.b.root)
        self.aid=self.a.call('project_open')['project']['data']['project_id']
        self.bid=self.b.call('project_open')['project']['data']['project_id']
        self.source=self.b.root/'PLAN.md';self.source.write_text('original source')
    def manifest(self):
        src={'uri':str(self.source),'locator':'line 1'}
        return {'project_root':str(self.b.root),'batch_id':'test-1','sources':[{**src,'sha256':file_hash(self.source)}], 'nodes':[
            {'id':'S08.2','kind':'HumanGate','title':'User approval','body':'pending','source':src,'data':{'original_status':'未获用户验收','original_kind':'HumanGate'}},
            {'id':'S13.1@D','kind':'Task','title':'Future task','body':'test','state':'todo','source':src,'data':{'original_status':'未开始'}},
            {'id':'S01','kind':'Task','title':'Historical claim','body':'reported complete','state':'reported','source':src,'data':{'original_status':'已完成且已验证'}}], 'edges':[{'source':'S13.1@D','target':'S08.2','relation':'depends_on','basis':'original plan'}]}
    def apply(self,m,request='test'):
        return self.b.call('history_import',{'manifest':m,'request_id':request})
    def test_history_import_rejects_changed_preview_sequence_and_preserves_later_data(self):
        m=self.manifest();m['expected_sequence']=self.b.snapshot()['sequence']
        later=self.b.call('idea_capture',{'title':'later event','body':'must survive',
                                         'request_id':'later-preview-writer'})
        before=self.b.log.read_bytes()
        with self.assertRaisesRegex(Conflict,'项目事件已变'):
            self.apply(m,'stale-preview')
        self.assertEqual(self.b.log.read_bytes(),before)
        self.assertEqual(self.b.call('graph_get',{'id':later['id']})['node'],later)
        m['expected_sequence']=self.b.snapshot()['sequence']
        self.assertEqual(self.apply(m,'fresh-preview')['imported'],3)
        sequence=self.b.snapshot()['sequence']
        self.assertEqual(self.apply(m,'fresh-preview-replay')['imported'],0)
        self.assertEqual(self.b.snapshot()['sequence'],sequence)
        for value in (True,-1,'1',None):
            invalid=self.manifest();invalid['batch_id']='invalid-'+str(value)
            invalid['expected_sequence']=value
            with self.assertRaisesRegex(Conflict,'非负整数'):
                self.apply(invalid,'invalid-'+str(value))

    def test_history_import_rejects_original_change_during_candidate_creation(self):
        m=self.manifest();before=self.b.log.read_bytes()
        from service import file_hash as original_hash
        def observed(path):
            # Sources are checked first; mutate only after a node source is read.
            result=original_hash(path)
            if path == str(self.source):
                observed.calls += 1
                if observed.calls == 2:
                    self.source.write_text('later source bytes; never restore')
            return result
        observed.calls=0
        with patch('service.file_hash',side_effect=observed):
            with self.assertRaisesRegex(Conflict,'导入过程中原件已变'):
                self.apply(m,'source-race')
        self.assertEqual(self.b.log.read_bytes(),before)
        self.assertEqual(self.source.read_text(),'later source bytes; never restore')
        self.assertEqual(len(self.b.snapshot()['nodes']),1)
        self.assertEqual(len(self.a.snapshot()['nodes']),1)

    def test_atomic_idempotent_import_gate_and_replay(self):
        m=self.manifest();self.apply(m)
        self.assertEqual(self.apply(m,'again')['imported'],0)
        snap=self.b.snapshot();g=next(n for n in snap['nodes'] if n['id']=='S08.2')
        self.assertEqual(g['status'],'pending')
        f={t['id']:t for t in snap['frontier']}
        self.assertEqual(f['S13.1@D']['bucket'],'Waiting Human')
        self.assertEqual(f['S01']['bucket'],'Pending Validation')
        with self.assertRaises(Conflict):self.b.call('task_claim',{'id':'S13.1@D','expected_version':1,'request_id':'claim'})
        self.b.db.unlink();self.assertEqual(Store(self.b.root).snapshot()['nodes'],snap['nodes'])
        self.assertEqual(len(self.a.snapshot()['nodes']),1)
    def test_cli_import_uses_same_atomic_idempotent_store(self):
        m=self.manifest()
        argv=[sys.executable,str(APP),'--root',str(self.b.root),'call','history_import',
              json.dumps({'manifest':m,'request_id':'cli-import-test'})]
        first=subprocess.run(argv,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(first.stdout)['imported'],3)
        sequence=self.b.snapshot()['sequence']
        second=subprocess.run(argv,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(second.stdout)['imported'],3)
        self.assertEqual(self.b.snapshot()['sequence'],sequence)
        argv[-1]=json.dumps({'manifest':m,'request_id':'cli-import-test-retry'})
        third=subprocess.run(argv,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(third.stdout)['imported'],0)
        self.assertEqual(self.b.snapshot()['sequence'],sequence)
    def test_stale_conflict_and_cycle_leave_no_partial_objects(self):
        m=self.manifest();self.source.write_text('new write')
        with self.assertRaises(Conflict):self.apply(m)
        self.assertEqual(len(self.b.snapshot()['nodes']),1)
        m=self.manifest();m['edges'].append({'source':'S08.2','target':'S13.1@D','relation':'depends_on','basis':'cycle'})
        with self.assertRaises(Conflict):self.apply(m)
        self.assertEqual(len(self.b.snapshot()['nodes']),1)
        m=self.manifest();self.apply(m);m['nodes'][0]['body']='altered'
        with self.assertRaises(Conflict):self.apply(m,'new')
    def test_prior_permission_is_not_new_user_acceptance(self):
        m=self.manifest();src=m['nodes'][0]['source']
        m['nodes'].extend([
          {'id':'USER-RESUME','kind':'Evidence','title':'actual user message','body':'resume work','source':src,'data':{'message_role':'user'}},
          {'id':'G-RESUME','kind':'HumanGate','title':'existing permission','body':'prior authorization','source':src,'data':{'gate_category':'existing_execution_authorization','authorization_message':'USER-RESUME'}}])
        self.apply(m)
        self.assertEqual(self.b.call('graph_get',{'id':'G-RESUME'})['node']['status'],'pending')
        self.assertEqual(self.b.call('graph_get',{'id':'G-RESUME'})['node']['data']['user_acceptance'],'NOT_CONFIRMED')
        self.assertEqual(self.b.call('graph_get',{'id':'S08.2'})['node']['status'],'pending')

    def test_generated_user_role_never_approves_imported_gate_or_unblocks_task(self):
        m=self.manifest();src=m['nodes'][0]['source']
        m['nodes'].extend([
          {'id':'GENERATED-GOAL','kind':'Evidence','title':'generated goal','body':'continue work',
           'source':src,'data':{'message_role':'user','type':'goal.internal_context','human_authored':True}},
          {'id':'G-GENERATED','kind':'HumanGate','title':'claimed old permission','state':'approved',
           'source':src,'data':{'gate_category':'existing_execution_authorization',
                               'authorization_message':'GENERATED-GOAL'}},
          {'id':'AFTER-GENERATED','kind':'Task','title':'dependent','state':'todo','source':src}])
        m['edges'].append({'source':'AFTER-GENERATED','target':'G-GENERATED',
                          'relation':'depends_on','basis':'original stop point'})
        self.apply(m)
        gate=self.b.call('graph_get',{'id':'G-GENERATED'})['node']
        self.assertEqual(gate['status'],'pending')
        self.assertIn('not a current human decision',gate['data']['approval_qualification'])
        frontier={n['id']:n for n in self.b.snapshot()['frontier']}
        self.assertEqual(frontier['AFTER-GENERATED']['bucket'],'Waiting Human')
        before=self.b.log.read_bytes()
        with self.assertRaises(Conflict):
            self.b.call('human_gate_resolve',{'id':gate['id'],'expected_version':gate['version'],
                        'decision':'approved','body':'agent guessed','request_id':'no-auto-approve'})
        self.assertEqual(self.b.log.read_bytes(),before)
        sequence=self.b.snapshot()['sequence']
        self.assertEqual(self.apply(m,'generated-replay')['imported'],0)
        self.assertEqual(self.b.snapshot()['sequence'],sequence)

    def test_history_import_preserves_existing_actual_approved_and_pending_gates(self):
        self.b.call('task_create',{'id':'T-EXISTING','title':'existing task',
                                 'body':'isolated test','request_id':'existing-task'})
        approved=self.b.call('human_gate_request',{'task_id':'T-EXISTING','id':'G-EXISTING','title':'existing actual decision',
                  'body':'test-only accurate human channel','request_id':'existing-gate'})
        approved=self.b.call('human_gate_resolve',{'id':approved['id'],
                  'expected_version':approved['version'],'decision':'approved',
                  'body':'isolated component human action','request_id':'actual-ui-decision'},channel='ui')
        before=self.b.call('graph_get',{'id':approved['id']})['node']
        self.apply(self.manifest())
        self.assertEqual(self.b.call('graph_get',{'id':approved['id']})['node'],before)
        self.assertEqual(self.b.call('graph_get',{'id':'S08.2'})['node']['status'],'pending')
        nodes=self.b.snapshot()['nodes'];log=self.b.log.read_bytes()
        self.b.db.unlink()
        self.assertEqual(Store(self.b.root).snapshot()['nodes'],nodes)
        self.assertEqual(self.b.log.read_bytes(),log)

    def test_http_import_fingerprint_and_retry_preserve_later_edits(self):
        m=self.manifest();(self.b.folder/'imports').mkdir();(self.b.folder/'imports/preview.json').write_text(json.dumps(m))
        base,csrf=self.start()
        def post(payload):
            req=urllib.request.Request(base+'/api/import',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-PGW-CSRF':csrf})
            return json.load(urllib.request.urlopen(req))
        args={'project_id':self.bid,'batch_id':m['batch_id'],'fingerprint':digest(m)}
        with self.assertRaises(urllib.error.HTTPError):post({**args,'fingerprint':'old'})
        self.assertEqual(len(self.b.snapshot()['nodes']),1)
        self.b.call('history_import',{'manifest':m,'request_id':'import-'+m['batch_id'],'actor':'codex-main'})
        imported_sequence=self.b.snapshot()['sequence']
        post(args)
        self.assertEqual(self.b.snapshot()['sequence'],imported_sequence)
        n=self.b.call('graph_get',{'id':'S01'})['node']
        self.b.call('task_progress',{'id':'S01','expected_version':n['version'],'body':'later user record','status':'pending_validation','request_id':'later'})
        before=self.b.snapshot();post(args);after=self.b.snapshot()
        self.assertEqual(before['sequence'],after['sequence'])
        self.assertEqual(before['nodes'],after['nodes'])
        self.assertEqual(len(self.a.snapshot()['nodes']),1)

    def test_project_identity_change_rejected(self):
        entries=self.ws.entries();entries[self.bid]['root']=str(self.a.root)
        self.ws.path.write_text(json.dumps({'projects':list(entries.values())}))
        with self.assertRaises(Conflict):self.ws.resolve(self.bid)
    def start(self):
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        p=subprocess.Popen([sys.executable,str(APP),'--root',str(self.a.root),'serve','--port',str(port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.assertIn('http://',p.stdout.readline())
        self.addCleanup(lambda:(p.terminate(),p.communicate(timeout=5)))
        base=f'http://127.0.0.1:{port}';csrf=json.load(urllib.request.urlopen(base+'/api/session'))['csrf']
        return base,csrf
    def test_http_isolation_source_and_restart(self):
        self.apply(self.manifest());base,csrf=self.start()
        def post(body):
            r=urllib.request.Request(base+'/api/call',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-PGW-CSRF':csrf})
            return json.load(urllib.request.urlopen(r))
        for pid,title in ((self.aid,'only A'),(self.bid,'only B')):
            post({'project_id':pid,'name':'idea_capture','arguments':{'id':'same-id','title':title,'request_id':'same-request'}})
        for pid,title in ((self.aid,'only A'),(self.bid,'only B')):
            snap=json.load(urllib.request.urlopen(base+'/api/state?project='+pid))
            self.assertEqual(next(n['title'] for n in snap['nodes'] if n['id']=='same-id'),title)
        for body in ({'name':'idea_capture','arguments':{'title':'no project','request_id':'bad'}},{'project_id':'unknown','name':'project_status'}, {'project_id':self.aid,'name':'idea_capture','arguments':{'project_root':str(self.b.root),'title':'cross','request_id':'bad'}}):
            with self.assertRaises(urllib.error.HTTPError):post(body)
        with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(base+'/api/source?project='+self.aid+'&id=S01')
        self.assertEqual(json.load(urllib.request.urlopen(base+'/api/source?project='+self.bid+'&id=S01'))['text'],'original source')
        base2,_=self.start()
        restored=json.load(urllib.request.urlopen(base2+'/api/state?project='+self.bid))
        self.assertEqual(next(n['title'] for n in restored['nodes'] if n['id']=='same-id'),'only B')

    def test_source_versions_match_changed_and_unregistered(self):
        self.source.write_bytes('原件\r\n'.encode())
        source={'uri':str(self.source),'sha256':file_hash(self.source)}
        result=local_source_preview(source)
        self.assertEqual(result['freshness'],'match')
        self.assertEqual(result['text'],'原件\r\n')
        self.assertEqual(result['current_sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.source.write_text('新版原件')
        self.assertEqual(local_source_preview(source)['freshness'],'changed')
        unknown=local_source_preview({'uri':str(self.source)})
        self.assertEqual(unknown['freshness'],'unregistered')
        self.assertIsNone(unknown['registered_sha256'])

    def test_source_http_warns_without_mutating_registered_state(self):
        self.apply(self.manifest());base,_=self.start()
        before=self.b.snapshot()
        url=base+'/api/source?project='+self.bid+'&id=S01'
        self.assertEqual(json.load(urllib.request.urlopen(url))['freshness'],'match')
        self.source.write_text('new source after import')
        changed=json.load(urllib.request.urlopen(url))
        self.assertEqual(changed['freshness'],'changed')
        self.assertEqual(changed['text'],'new source after import')
        self.assertNotEqual(changed['current_sha256'],changed['registered_sha256'])
        self.assertEqual(self.b.snapshot(),before)

    def test_source_message_hashes_entire_file_and_rejects_ambiguous_id(self):
        path=self.b.root/'messages.json'
        payload={'messages':[{'message_id':'M','text':'中文原文'},{'message_id':'N','text':'其他'}]}
        path.write_text(json.dumps(payload,ensure_ascii=False))
        source={'uri':str(path),'sha256':file_hash(path)}
        result=local_source_preview(source,'M')
        self.assertEqual(json.loads(result['text']),payload['messages'][0])
        self.assertEqual(result['hash_scope'],'whole_file')
        self.assertEqual(result['current_sha256'],file_hash(path))
        with self.assertRaises(Conflict):local_source_preview(source,'absent')
        payload['messages'].append(payload['messages'][0]);path.write_text(json.dumps(payload))
        with self.assertRaises(Conflict):local_source_preview(source,'M')

    def test_source_oversize_and_invalid_utf8_are_not_previewed(self):
        self.source.write_bytes(b'a'*512001)
        with self.assertRaises(Conflict):local_source_preview({'uri':str(self.source)})
        self.source.write_bytes(b'\xff')
        with self.assertRaises(UnicodeError):local_source_preview({'uri':str(self.source)})

    def test_source_read_is_bounded_and_growth_is_rejected(self):
        path=self.source
        real_open=Path.open
        seen=[]
        class GrowingReader:
            def __enter__(self):
                self.handle=real_open(path,'rb');return self
            def __exit__(self,*args):self.handle.close()
            def fileno(self):return self.handle.fileno()
            def read(self,size):
                seen.append(size)
                with real_open(path,'ab') as output:output.write(b'a'*512001)
                return self.handle.read(size)
        with patch.object(Path,'open',return_value=GrowingReader()):
            with self.assertRaises(Conflict):local_source_preview({'uri':str(path)})
        self.assertEqual(seen,[512001])

    def test_source_path_replacement_is_rejected(self):
        path=self.source
        real_open=Path.open
        class ReplacingReader:
            def __enter__(self):self.handle=real_open(path,'rb');return self
            def __exit__(self,*args):self.handle.close()
            def fileno(self):return self.handle.fileno()
            def read(self,size):
                raw=self.handle.read(size)
                replacement=path.with_suffix('.new')
                with real_open(replacement,'wb') as output:output.write(raw)
                replacement.replace(path)
                return raw
        with patch.object(Path,'open',return_value=ReplacingReader()):
            with self.assertRaises(Conflict):local_source_preview({'uri':str(path)})

if __name__=='__main__':unittest.main()
