import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from service import Store
spec=importlib.util.spec_from_file_location('walkthrough',APP/'examples/create_walkthrough.py');example=importlib.util.module_from_spec(spec);spec.loader.exec_module(example)
class WalkthroughTest(unittest.TestCase):
    def test_coherent_states_and_pending_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'example';result=example.create(root);store=Store(root)
            status=store.call('project_status');self.assertEqual(status['counts']['Task'],16);self.assertEqual(status['counts']['Phase'],3)
            f={t['id']:t for t in status['frontier']}
            for id in ['N01','N02','N03']:self.assertEqual(f[id]['bucket'],'Completed')
            for id in ['N04','N07']:self.assertEqual(f[id]['bucket'],'In Progress')
            for id in ['N05','N06','N10','N15']:self.assertEqual(f[id]['bucket'],'Blocked')
            for id in ['N11','N13']:self.assertEqual(f[id]['bucket'],'Pending Validation')
            self.assertEqual(f['N14']['bucket'],'Waiting Human');self.assertEqual(f['N16']['bucket'],'Deferred')
            self.assertEqual(result['gate'],'pending');self.assertNotIn('Thread',status['counts']);self.assertNotIn('Run',status['counts'])
            self.assertIn('N04',' '.join(f['N05']['reasons']))
    def test_repeat_refusal_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'example';example.create(root);before=(root/'.project-graph/events.jsonl').read_bytes()
            with self.assertRaises(SystemExit):example.create(root)
            self.assertEqual(before,(root/'.project-graph/events.jsonl').read_bytes())
    def test_source_revision_is_actual_and_reopen_retains_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'example';example.create(root);store=Store(root)
            e=store.call('graph_get',{'id':'E04'})['node'];self.assertNotEqual(e['data']['source']['sha256'],hashlib.sha256((root/'privacy.md').read_bytes()).hexdigest())
            output=json.loads(subprocess.check_output([sys.executable,str(APP/'pgw.py'),'--root',str(root),'call','project_status']))
            self.assertEqual(output['project']['data']['project_id'],store.call('project_status')['project']['data']['project_id'])
            self.assertEqual(Store(root).call('graph_get',{'id':'G01'})['node']['status'],'pending')
            self.assertEqual(output['counts']['Task'],16)
if __name__=='__main__':unittest.main()
