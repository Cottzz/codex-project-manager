"""Explicitly install a verified derived code cache; never updates graph events."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

def install(project_root, source_root, cache_file, cache_root=None):
    project_root, source_root, cache_file = (Path(p).expanduser().resolve() for p in (project_root, source_root, cache_file))
    ident = json.loads((project_root/'.project-graph/project.json').read_text())['project_id']
    if cache_file.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('Code cache exceeds the 128 MiB import limit')
    with sqlite3.connect(cache_file.as_uri()+'?mode=ro', uri=True) as db:
        required = {
            'files': {'path','language','sha256','bytes','status','errors'},
            'symbols': {'id','path','kind','name','line','end_line'},
            'relations': {'source','target','kind','method','path','line'},
            'unresolved': {'path','kind','text','line','reason'},
            'syntax_refs': {'path','kind','text','line','reason'},
        }
        for table, columns in required.items():
            actual = {row[1] for row in db.execute('PRAGMA table_info('+table+')')}
            if not columns.issubset(actual):
                raise ValueError('Incompatible code cache schema: '+table)
        meta = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM meta')}
        if meta['project_id'] != ident or Path(meta['source_root']).resolve() != source_root:
            raise ValueError('Code cache belongs to another project/source root')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Code cache integrity failed')
    target = Path(cache_root) if cache_root else Path(__file__).resolve().parents[1]/'.project-graph/code-index'
    target.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(cache_file.read_bytes()).hexdigest()
    name = 'index-'+sha+'.sqlite'
    lock = target/'refresh-cutover.lock'
    lock.mkdir()
    try:
        pointer = target/'manifest.json'
        current = json.loads(pointer.read_text()) if pointer.exists() else {}
        if cache_file != (target/name).resolve():
            shutil.copyfile(cache_file, target/name)
        if hashlib.sha256((target/name).read_bytes()).hexdigest() != sha:
            raise ValueError('Copy checksum mismatch')
        current[ident] = {'filename': name, 'sha256': sha}
        temp = target/'manifest.next'
        temp.write_text(json.dumps(current, indent=2), encoding='utf-8')
        os.replace(temp, pointer)
    finally:
        lock.rmdir()
    return {'project_id': ident, 'sha256': sha, 'task_state_unchanged': True}

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True)
    p.add_argument('--source-root', required=True)
    p.add_argument('--cache', required=True)
    a = p.parse_args()
    print(json.dumps(install(a.root, a.source_root, a.cache)))
