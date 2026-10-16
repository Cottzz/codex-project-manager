import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
class ReleaseInstallTest(unittest.TestCase):
    def test_clean_demo_cli_http_restart_and_preserved_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'demo'
            subprocess.run([sys.executable, str(APP/'scripts/create_demo.py'), str(root)], check=True, capture_output=True)
            rejected = subprocess.run([sys.executable, str(APP/'scripts/create_demo.py'), str(root)], capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            def cli(name):
                return json.loads(subprocess.check_output([sys.executable, str(APP/'pgw.py'), '--root', str(root), 'call', name]))
            before = (root/'.project-graph/events.jsonl').read_bytes()
            state = cli('project_status')
            ident = state['project']['data']['project_id']
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(2):
                with tempfile.TemporaryFile() as log:
                    proc = subprocess.Popen([sys.executable, str(APP/'pgw.py'), '--root', str(root), 'serve', '--port', str(port)], stdout=log, stderr=log)
                    try:
                        for attempt in range(80):
                            try:
                                with opener.open(f'http://127.0.0.1:{port}/api/state?project={ident}', timeout=1) as response:
                                    data = json.load(response)
                                break
                            except OSError:
                                if proc.poll() is not None:
                                    log.seek(0); self.fail(log.read().decode())
                                time.sleep(.1)
                        else:
                            self.fail('Fresh service did not respond')
                        self.assertEqual(data['project']['data']['project_id'], ident)
                        self.assertEqual(data['project']['title'], 'Release readiness demo')
                        gate = next(n for n in data['nodes'] if n['id']=='REVIEW-001')
                        self.assertNotEqual(gate['status'], 'approved')
                    finally:
                        proc.terminate(); proc.wait(timeout=10)
            self.assertEqual(before, (root/'.project-graph/events.jsonl').read_bytes())

    def test_first_cache_import_preserves_other_projects_and_refuses_foreign_identity(self):
        import importlib.util
        import sqlite3
        spec = importlib.util.spec_from_file_location('install_code_index', APP/'scripts/install_code_index.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); root = base/'demo'
            subprocess.run([sys.executable, str(APP/'scripts/create_demo.py'), str(root)], check=True, capture_output=True)
            ident = json.loads((root/'.project-graph/project.json').read_text())['project_id']
            events = (root/'.project-graph/events.jsonl').read_bytes()
            source = base/'source'; source.mkdir()
            incoming = base/'index.sqlite'
            with sqlite3.connect(incoming) as db:
                db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
                for table, columns in {'files':'path,language,sha256,bytes,status,errors','symbols':'id,path,kind,name,line,end_line','relations':'source,target,kind,method,path,line','unresolved':'path,kind,text,line,reason','syntax_refs':'path,kind,text,line,reason'}.items():
                    db.execute('CREATE TABLE '+table+' ('+columns+')')
                db.executemany('INSERT INTO meta VALUES (?,?)', [('project_id', json.dumps(ident)), ('source_root', json.dumps(str(source)))])
            target = base/'cache'; target.mkdir()
            (target/'manifest.json').write_text(json.dumps({'other': {'filename': 'keep.sqlite'}}))
            result = module.install(root, source, incoming, target)
            self.assertEqual(result['project_id'], ident)
            self.assertEqual(json.loads((target/'manifest.json').read_text())['other'], {'filename': 'keep.sqlite'})
            (target/'refresh-cutover.lock').mkdir()
            with self.assertRaises(FileExistsError): module.install(root, source, incoming, target)
            self.assertTrue((target/'refresh-cutover.lock').exists())
            (target/'refresh-cutover.lock').rmdir()
            with sqlite3.connect(incoming) as db:
                db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps('foreign'), 'project_id'))
            with self.assertRaises(ValueError): module.install(root, source, incoming, target)
            with sqlite3.connect(incoming) as db:
                db.execute('DROP TABLE symbols')
            with self.assertRaises(ValueError): module.install(root, source, incoming, target)
            self.assertEqual(events, (root/'.project-graph/events.jsonl').read_bytes())
