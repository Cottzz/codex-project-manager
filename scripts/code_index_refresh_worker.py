"""Pinned Windows/WSL worker: code only, no business execution or task-state write."""
import hashlib,json,sqlite3,subprocess,sys
from pathlib import Path
job=Path(sys.argv[1]).resolve();m=json.loads((job/'stage/manifest.json').read_text());root=job/'stage/source'
for f in m['files']:
 p=root/f['relative_path'];assert p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==f['sha256']
assert hashlib.sha256((job/'stage/builder.py').read_bytes()).hexdigest()==m['builder_sha256']
assert hashlib.sha256((job/'stage/prior.sqlite').read_bytes()).hexdigest()==m['prior']['sha256']
base=[sys.executable,str(job/'stage/builder.py'),'--input-root',str(root),'--original-root',m['source_root'],'--project-id',m['project_id']]
for mode,extra in [('incremental',['--prior',str(job/'stage/prior.sqlite')]),('clean',[])]:
 with (job/(mode+'.log')).open('wb') as out:subprocess.run(base+extra+['--output',str(job/(mode+'.sqlite'))],stdout=out,stderr=subprocess.STDOUT,check=True)
with sqlite3.connect(job/'incremental.sqlite') as a,sqlite3.connect(job/'clean.sqlite') as b:
 for table in ['files','symbols','relations','unresolved','syntax_refs']:assert sorted(a.execute('SELECT * FROM '+table))==sorted(b.execute('SELECT * FROM '+table)),table
 assert dict(a.execute('SELECT path,sha256 FROM files'))=={f['path']:f['sha256'] for f in m['files']}
 assert a.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 metadata={k:json.loads(v) for k,v in a.execute('SELECT key,value FROM meta')}
 assert metadata['project_id']==m['project_id'] and metadata['source_root']==m['source_root']
r={'project_id':m['project_id'],'request_id':m['request_id'],'source_manifest_sha256':hashlib.sha256((job/'stage/manifest.json').read_bytes()).hexdigest(),'cache_sha256':hashlib.sha256((job/'incremental.sqlite').read_bytes()).hexdigest(),'five_tables_equal':True,'counts':metadata['counts'],'source_count':len(m['files']),'scope':'frozen deterministic code snapshot only; no business/model/task execution'}
(job/'result.json').write_text(json.dumps(r,indent=2));print(json.dumps(r),flush=True)
