"""Windows parser tests: deletion/re-addition must match a clean rebuild."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BuilderIncrementalTest(unittest.TestCase):
    def test_css_selectors_html_script_style_and_shell_unknowns(self):
        import importlib.util
        builder = Path(__file__).resolve().parents[1] / "scripts" / "code_index_builder.py"
        spec = importlib.util.spec_from_file_location("language_builder", builder)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        fixtures = {
            "css": ('.card, #main {color: red;}\n@media screen {.inner {display: block;}}\n',
                    [("css_rule", ".card, #main", 1, 1), ("css_rule", ".inner", 2, 2)]),
            "html": ('<html><script src="./x.js"></script><style>.card{color:red}</style><img src="./x.png"/></html>',
                     [("html_element", "<html>", 1, 1), ("html_element", '<script src="./x.js">', 1, 1),
                      ("html_element", "<style>", 1, 1), ("html_element", '<img src="./x.png"/>', 1, 1)]),
            "bash": ('target() { true; }\nrun() { target; }\nsource ./other.sh\nrun\n',
                     [("function", "target", 1, 1), ("function", "run", 2, 2)]),
            "powershell": ('function Target { return 1 }\nfunction Run { Target }\n. ./other.ps1\nRun\n',
                           [("function", "Target", 1, 1), ("function", "Run", 2, 2)])}
        for language, (text, expected) in fixtures.items():
            with self.subTest(language=language):
                rows, refs, errors = module.extract(text.encode(), module.parser_for(language))
                self.assertEqual(rows, expected)
                self.assertEqual(errors, [])
                if language in {"bash", "powershell"}:
                    self.assertTrue(any(row[0] == "call" and ("source" in row[1] or "./other.ps1" in row[1]) for row in refs))
                    self.assertEqual(module.same_file_call_bindings(text.encode(), module.parser_for(language), language), {})
        self.assertIn("src/href", module.semantic_capabilities({"html"})["html"]["unknown"])
        self.assertIn("unresolved", module.semantic_capabilities({"bash"})["bash"]["calls"])

    def test_markup_changes_replay_and_shell_commands_never_infer_edges(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); src = root / "source"; src.mkdir()
            (src / "main.css").write_text('@import "./base.css";\n.a {color: red;}')
            (src / "base.css").write_text('.base {color: black;}')
            (src / "index.html").write_text('<script src="./code.js"></script><style>.a{}</style>')
            (src / "run.sh").write_text('target() { true; }\ntarget\nsource ./other.sh\n')
            (src / "run.ps1").write_text('function Target { return 1 }\nTarget\n. ./other.ps1\n')
            builder = Path(__file__).resolve().parents[1] / "scripts" / "code_index_builder.py"
            def build(name, prior=None):
                dest = root / (name + ".sqlite")
                args = [sys.executable, str(builder), "--input-root", str(src), "--original-root", "/authorized/source",
                        "--project-id", "fixture", "--output", str(dest)]
                if prior: args += ["--prior", str(prior)]
                run = subprocess.run(args, capture_output=True, text=True, timeout=45)
                self.assertEqual(run.returncode, 0, run.stderr); return dest
            first = build("first")
            with sqlite3.connect(first) as db:
                self.assertEqual(db.execute("SELECT path,kind,name FROM symbols WHERE kind='css_rule' ORDER BY path").fetchall(),
                                 [("/authorized/source/base.css", "css_rule", ".base"), ("/authorized/source/main.css", "css_rule", ".a")])
                self.assertEqual(db.execute("SELECT path,target FROM relations WHERE kind='imports'").fetchall(),
                                 [("/authorized/source/main.css", "/authorized/source/base.css")])
                self.assertEqual(db.execute("SELECT count(*) FROM relations WHERE kind='calls'").fetchone()[0], 0)
                unknown = dict(db.execute("SELECT path,count(*) FROM unresolved WHERE path LIKE '%.sh' OR path LIKE '%.ps1' GROUP BY path"))
                self.assertEqual(unknown, {"/authorized/source/run.sh": 3, "/authorized/source/run.ps1": 2})
                meta = {k: json.loads(v) for k,v in db.execute("SELECT key,value FROM meta")}
                self.assertEqual(meta["symbol_extractor_version"], 3)
                self.assertEqual(set(meta["semantic_capabilities"]), {"css", "html", "bash", "powershell"})
            (src / "main.css").write_text('@import "./base.css";\n.changed {color: blue;}')
            (src / "index.html").unlink()
            changed, clean = build("changed", first), build("clean")
            with sqlite3.connect(changed) as a, sqlite3.connect(clean) as b:
                for table in ["files", "symbols", "relations", "unresolved", "syntax_refs"]:
                    self.assertEqual(sorted(a.execute("SELECT * FROM " + table)), sorted(b.execute("SELECT * FROM " + table)), table)
                self.assertFalse(a.execute("SELECT id FROM symbols WHERE name='.a' OR path LIKE '%index.html'").fetchall())

    def test_same_file_lexical_visibility_parameter_shadow_and_rebinding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); src = root / "source"; src.mkdir()
            (src / "main.py").write_text("def target(): return 1\n"
                "def blocked(target):\n    return target()\n"
                "def good():\n    return target()\n"
                "def outer():\n    def hidden(): return 2\n    return hidden()\n"
                "hidden()\nclass Example:\n    def method(self):\n        def local(): return 3\n        return local()\nmethod()\nExample()\n")
            (src / "main.js").write_text("function target(){return 1;}\n"
                "function blocked(target){\n return target();\n}\n"
                "function good(){\n return target();\n}\n"
                "function outer(){\n function hidden(){return 2;}\n return hidden();\n}\n"
                "hidden();\nconst object={method(){return 3;}};\nmethod();\n")
            (src / "rebound.py").write_text("def target(): return 1\n"
                "target = lambda: 0\ntarget()\n")
            (src / "rebound.js").write_text("function target(){return 1;}\n"
                "target = ()=>0;\ntarget();\n")
            (src / "same-line.js").write_text("function target(){return 1;} function good(){return target();} function blocked(target){return target();}\n")
            (src / "varshadow.js").write_text("function target(){return 1;}\nfunction blocked(callback){\n if(true){var target=callback;}\n return target();\n}\n")
            (src / "exception.py").write_text("def target(): return 1\ndef blocked():\n try: raise Exception()\n except Exception as target:\n  target()\n")
            (src / "withalias.py").write_text("def target(): return 1\ndef blocked(ctx):\n with ctx as target:\n  target()\n")
            (src / "comprehension.py").write_text("def target(): return 1\nvalues=[target() for target in []]\n")
            (src / "lambda.py").write_text("def target(): return 1\nvalue=lambda target: target()\n")
            (src / "deleted.py").write_text("def target(): return 1\ndef blocked():\n del target\n target()\n")
            (src / "dotted-import.py").write_text("import urllib.request\ndef request(): return 1\nrequest()\n")
            (src / "from-import.py").write_text("from pkg.request import other\ndef request(): return 1\nrequest()\n")
            builder = Path(__file__).resolve().parents[1] / "scripts" / "code_index_builder.py"
            def build(name, prior=None):
                dest = root / (name + ".sqlite")
                args = [sys.executable, str(builder), "--input-root", str(src),
                        "--original-root", "/authorized/source", "--project-id", "fixture", "--output", str(dest)]
                if prior: args += ["--prior", str(prior)]
                run = subprocess.run(args, capture_output=True, text=True, timeout=45)
                self.assertEqual(run.returncode, 0, run.stderr)
                return dest
            first = build("first")
            with sqlite3.connect(first) as db:
                actual = db.execute("SELECT r.path,t.name,r.line FROM relations r JOIN symbols t ON t.id=r.target WHERE r.kind='calls' ORDER BY r.path,r.line").fetchall()
                # Independent source oracle: seven functions and one class call. No inference
                # from emitted graph edges or symbol count supplies this list.
                self.assertEqual(actual, [("/authorized/source/dotted-import.py", "request", 3),
                    ("/authorized/source/from-import.py", "request", 3), ("/authorized/source/main.js", "target", 6),
                    ("/authorized/source/main.js", "hidden", 10),
                    ("/authorized/source/main.py", "target", 5),
                    ("/authorized/source/main.py", "hidden", 8), ("/authorized/source/main.py", "local", 13), ("/authorized/source/main.py", "Example", 15)])
                unknown = db.execute("SELECT path,line FROM unresolved WHERE kind='call' ORDER BY path,line").fetchall()
                self.assertTrue({("/authorized/source/main.py",3),("/authorized/source/main.py",9),
                    ("/authorized/source/main.py",14),("/authorized/source/main.js",3),
                    ("/authorized/source/main.js",12),("/authorized/source/main.js",14),
                    ("/authorized/source/rebound.py",3),("/authorized/source/rebound.js",3),
                    ("/authorized/source/same-line.js",1),("/authorized/source/varshadow.js",4),
                    ("/authorized/source/exception.py",5),("/authorized/source/withalias.py",4),
                    ("/authorized/source/comprehension.py",2),("/authorized/source/lambda.py",2),
                    ("/authorized/source/deleted.py",4)} <= set(unknown))
            (src / "main.py").write_text("def replacement(): return 9\nreplacement()\n")
            changed = build("changed", first)
            clean = build("clean")
            with sqlite3.connect(changed) as a, sqlite3.connect(clean) as b:
                for table in ["files","symbols","relations","unresolved","syntax_refs"]:
                    self.assertEqual(sorted(a.execute("SELECT * FROM "+table)), sorted(b.execute("SELECT * FROM "+table)), table)

    def test_named_relative_calls_alias_shadowing_exports_and_incremental_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); src = root / 'source'; src.mkdir()
            # This hand-written source is the oracle; expectations do not come
            # from querying a graph and feeding its own edges back as truth.
            (src / 'provider.mjs').write_text("export function target(){return 1;}\n"
                "export const arrow = value => value;\nfunction hidden(){return 2;}\n")
            (src / 'good.mjs').write_text("import {target as alias, arrow} from './provider.mjs';\n"
                "export function run(){alias(); arrow(1); hidden();}\n")
            (src / 'shadow.mjs').write_text("import {target as alias} from './provider.mjs';\n"
                "export function run(alias){alias();}\n")
            (src / 'local.mjs').write_text("import {target as alias} from './provider.mjs';\n"
                "export function run(){const alias = ()=>0; alias();}\n")
            (src / 'hidden.mjs').write_text("import {hidden} from './provider.mjs';\n"
                "function run(){hidden();}\n")
            (src / 'namespace.mjs').write_text("import * as api from './provider.mjs';\n"
                "api.target();\n")
            (src / 'external.mjs').write_text("import {target} from 'provider'; target();\n")
            (src / 'typed.ts').write_text("import type {target} from './provider.mjs';\n"
                "type T = typeof target;\n")
            (src / 'ambiguous.mjs').write_text("import {target} from './provider'; target();\n")
            (src / 'provider.ts').write_text("export function target(){return 3;}\n")
            (src / 'default.mjs').write_text("export default function target(){return 4;}\n")
            (src / 'default-user.mjs').write_text("import {target} from './default.mjs'; target();\n")
            (src / 'fake.mjs').write_text("// export function target() {}\nfunction target(){return 0;}\n")
            (src / 'fake-user.mjs').write_text("import {target} from './fake.mjs'; target();\n")
            builder = Path(__file__).resolve().parents[1] / 'scripts' / 'code_index_builder.py'
            def build(name, prior=None):
                dest = root / (name + '.sqlite')
                args = [sys.executable, str(builder), '--input-root', str(src),
                        '--original-root', '/authorized/source', '--project-id', 'fixture', '--output', str(dest)]
                if prior: args += ['--prior', str(prior)]
                result = subprocess.run(args, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                return dest
            def cross_calls(dest):
                with sqlite3.connect(dest) as db:
                    return db.execute("SELECT r.path,t.name,r.line FROM relations r JOIN symbols t ON t.id=r.target "
                        "WHERE r.method='named_relative_export_candidate' ORDER BY r.path,t.name,r.line").fetchall()
            first = build('first')
            expected = [('/authorized/source/good.mjs','arrow',2),('/authorized/source/good.mjs','target',2)]
            self.assertEqual(cross_calls(first), expected, 'known named relative calls must be connected')
            with sqlite3.connect(first) as db:
                unknown = {Path(r[0]).name for r in db.execute("SELECT path FROM unresolved WHERE kind='call'")}
                self.assertTrue({'shadow.mjs','hidden.mjs','namespace.mjs','external.mjs',
                                 'ambiguous.mjs','default-user.mjs','fake-user.mjs'} <= unknown)
            (src / 'provider.mjs').unlink()
            deleted = build('deleted', first)
            self.assertEqual(cross_calls(deleted), [('/authorized/source/ambiguous.mjs','target',1)])
            (src / 'provider.mjs').write_text("export function target(){return 9;}\nexport const arrow = value => value;\n")
            restored, clean = build('restored', deleted), build('clean')
            self.assertEqual(cross_calls(restored), expected)
            with sqlite3.connect(restored) as a, sqlite3.connect(clean) as b:
                for table in ['files','symbols','relations','unresolved','syntax_refs']:
                    self.assertEqual(sorted(a.execute('SELECT * FROM '+table)), sorted(b.execute('SELECT * FROM '+table)), table)
                self.assertEqual(json.loads(a.execute("SELECT value FROM meta WHERE key='resolver_version'").fetchone()[0]), 5)

    def test_exact_extensions_side_effect_assets_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / 'source'
            child = src / 'nested'
            child.mkdir(parents=True)
            (child / 'consumer.js').write_text("import { target } from './provider.js';\n"
                "import '../styles.css';\nimport { unknown } from './provider';\n"
                "import { missing } from './missing.css';\n")
            (child / 'provider.js').write_text('export function target(){ return 1; }')
            (child / 'provider.ts').write_text('export function target(){ return 2; }')
            style = src / 'styles.css'
            style.write_text('.x {color: red;}')
            builder = Path(__file__).resolve().parents[1] / 'scripts' / 'code_index_builder.py'
            def build(name, prior=None):
                dest = root / (name + '.sqlite')
                args = [sys.executable, str(builder), '--input-root', str(src),
                        '--original-root', '/authorized/source', '--project-id', 'fixture', '--output', str(dest)]
                if prior:
                    args += ['--prior', str(prior)]
                result = subprocess.run(args, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                return dest
            def relations(path):
                with sqlite3.connect(path) as db:
                    return db.execute('SELECT * FROM relations ORDER BY 1,2,3').fetchall()
            first = build('first')
            targets = {r[1] for r in relations(first)}
            self.assertEqual(targets, {'/authorized/source/nested/provider.js', '/authorized/source/styles.css'})
            with sqlite3.connect(first) as db:
                pending = [r[0] for r in db.execute('SELECT text FROM unresolved')]
                self.assertTrue(any("'./provider'" in text for text in pending))
                self.assertTrue(any("'./missing.css'" in text for text in pending))
            style.unlink()
            deleted = build('deleted', first)
            self.assertFalse(any(r[1].endswith('styles.css') for r in relations(deleted)))
            style.write_text('.x {color: red;}')
            restored = build('restored', deleted)
            self.assertEqual(relations(restored), relations(build('clean')))

    def test_deleted_import_target_reappears_and_parser_contract_invalidates_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / "source"
            src.mkdir()
            consumer = src / "consumer.js"
            provider = src / "provider.js"
            consumer.write_text("import { target } from './provider.js';\nexport function run(){ target(); }\n")
            provider.write_text("export function target(){ return 1; }\n")
            builder = Path(__file__).resolve().parents[1] / "scripts" / "code_index_builder.py"

            def build(name, prior=None):
                dest = root / (name + ".sqlite")
                args = [sys.executable, str(builder), "--input-root", str(src),
                        "--original-root", "/authorized/source", "--project-id", "fixture",
                        "--output", str(dest)]
                if prior:
                    args += ["--prior", str(prior)]
                result = subprocess.run(args, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                return dest

            def rows(dbfile, table):
                with sqlite3.connect(dbfile) as db:
                    return db.execute("SELECT * FROM " + table + " ORDER BY 1,2,3").fetchall()

            first = build("first")
            self.assertTrue(any(r[2] == "imports" for r in rows(first, "relations")))
            provider.unlink()
            deleted = build("deleted", first)
            self.assertTrue(any(r[1] == "import" for r in rows(deleted, "unresolved")))
            provider.write_text("export function target(){ return 1; }\n")
            restored = build("restored", deleted)
            clean = build("clean")
            self.assertEqual(rows(restored, "relations"), rows(clean, "relations"))
            self.assertTrue(any(r[2] == "imports" for r in rows(restored, "relations")))
            with sqlite3.connect(restored) as db:
                db.execute("UPDATE meta SET value=? WHERE key='parser_versions'", ('{}',))
            changed = build("changed-contract", restored)
            with sqlite3.connect(changed) as db:
                counts = json.loads(db.execute("SELECT value FROM meta WHERE key='counts'").fetchone()[0])
                self.assertEqual(counts.get("reused", 0), 0)



    def test_same_version_binary_fingerprint_and_missing_contract_reject_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); src = root / "source"; src.mkdir()
            (src / "a.py").write_text("def target(): return 1\ndef caller(): return target()\n")
            (src / "b.js").write_text("function target(){return 1;} target();\n")
            builder = Path(__file__).resolve().parents[1] / "scripts" / "code_index_builder.py"
            def build(name, prior=None):
                dest = root / (name + ".sqlite")
                args = [sys.executable, str(builder), "--input-root", str(src),
                        "--original-root", "/authorized/source", "--project-id", "fixture",
                        "--output", str(dest)]
                if prior: args += ["--prior", str(prior)]
                run = subprocess.run(args, capture_output=True, text=True, timeout=45)
                self.assertEqual(run.returncode, 0, run.stderr)
                return dest
            def metadata(path):
                with sqlite3.connect(path) as db:
                    return {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM meta")}
            first = build("first"); meta = metadata(first)
            self.assertIn("grammar_fingerprints", meta)
            self.assertEqual(set(meta["grammar_fingerprints"]), {"core", "python", "javascript"})
            for value in meta["grammar_fingerprints"].values():
                self.assertEqual(len(value["sha256"]), 64)
            same = build("same", first)
            self.assertEqual(metadata(same)["counts"].get("reused", 0), 2)
            with sqlite3.connect(same) as db:
                changed = metadata(same)["grammar_fingerprints"]
                changed["javascript"]["sha256"] = "0" * 64
                db.execute("UPDATE meta SET value=? WHERE key='grammar_fingerprints'",
                           (json.dumps(changed, sort_keys=True),))
            invalid = build("invalid", same)
            self.assertEqual(metadata(invalid)["parser_versions"], meta["parser_versions"])
            self.assertEqual(metadata(invalid)["counts"].get("reused", 0), 0)
            with sqlite3.connect(invalid) as db:
                db.execute("DELETE FROM meta WHERE key='grammar_fingerprints'")
            legacy = build("legacy", invalid)
            self.assertEqual(metadata(legacy)["counts"].get("reused", 0), 0)
            restored = build("restored", legacy)
            self.assertEqual(metadata(restored)["counts"].get("reused", 0), 2)
            clean = build("clean")
            for table in ("files", "symbols", "relations", "unresolved", "syntax_refs"):
                with sqlite3.connect(restored) as a, sqlite3.connect(clean) as b:
                    self.assertEqual(sorted(a.execute("SELECT * FROM " + table)),
                                     sorted(b.execute("SELECT * FROM " + table)))

    def test_arrow_names_use_bindings_not_parameters_or_callbacks(self):
        from scripts.code_index_builder import extract, parser_for
        raw = ("const sleep = ms => Promise.resolve(ms);\n"
               "const sha = (file) => file;\n"
               "const run = function(value) { return value; };\n"
               "const wrapped = (entry => entry);\n"
               "const box = {project: item => item};\n"
               "const alias = function inner(value) { return value; };\n"
               "const factory = () => value => value;\n"
               "items.map(callbackArg => callbackArg);\n"
               "box.route = routeArg => routeArg;\n"
               "const {unknown} = destructuredArg => destructuredArg;\n").encode()
        rows, refs, errors = extract(raw, parser_for('javascript'))
        self.assertEqual(errors, [])
        self.assertEqual([r[1] for r in rows],
                         ['sleep', 'sha', 'run', 'wrapped', 'project', 'inner', 'factory'])
        self.assertTrue(any(r[1] == 'items.map(callbackArg => callbackArg)' for r in refs))

    def test_typescript_wrapped_arrow_binding_and_anonymous_callbacks(self):
        from scripts.code_index_builder import extract, parser_for
        raw = ("const mapper = (entry => entry) as Mapper;\n"
               "const check = ((entry: number) => entry) satisfies Mapper;\n"
               "items.map((callbackArg: number) => callbackArg);\n").encode()
        rows, refs, errors = extract(raw, parser_for('typescript'))
        self.assertEqual(errors, [])
        self.assertEqual([r[1] for r in rows], ['mapper', 'check'])

    def test_arrow_calls_and_old_symbol_contract_force_reparse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / 'source'
            src.mkdir()
            (src / 'main.js').write_text("const sha = file => file;\n"
                "const run = () => sha('x');\nrun();\n")
            builder = Path(__file__).resolve().parents[1] / 'scripts' / 'code_index_builder.py'
            def build(name, prior=None):
                dest = root / (name + '.sqlite')
                args = [sys.executable, str(builder), '--input-root', str(src),
                        '--original-root', '/authorized/source', '--project-id', 'fixture',
                        '--output', str(dest)]
                if prior:
                    args += ['--prior', str(prior)]
                result = subprocess.run(args, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                return dest
            first = build('first')
            with sqlite3.connect(first) as db:
                symbols = dict(db.execute('SELECT name,id FROM symbols'))
                self.assertEqual(set(symbols), {'sha', 'run'})
                self.assertEqual(set(db.execute('SELECT source,target,line FROM relations')),
                    {(symbols['run'], symbols['sha'], 2), ('/authorized/source/main.js', symbols['run'], 3)})
                db.execute('DELETE FROM meta WHERE key=?', ('symbol_extractor_version',))
                db.execute("UPDATE symbols SET name='file' WHERE name='sha'")
            updated = build('updated', first)
            clean = build('clean')
            with sqlite3.connect(updated) as a, sqlite3.connect(clean) as b:
                for table in ['files', 'symbols', 'relations', 'unresolved', 'syntax_refs']:
                    self.assertEqual(sorted(a.execute('SELECT * FROM ' + table)),
                                     sorted(b.execute('SELECT * FROM ' + table)))
                counts = json.loads(a.execute("SELECT value FROM meta WHERE key='counts'").fetchone()[0])
                self.assertEqual(counts.get('reused', 0), 0)
                self.assertEqual(a.execute("SELECT value FROM meta WHERE key='symbol_extractor_version'").fetchone(), ('3',))
            reused = build('same-contract', updated)
            with sqlite3.connect(reused) as db:
                counts = json.loads(db.execute("SELECT value FROM meta WHERE key='counts'").fetchone()[0])
                self.assertEqual(counts.get('reused'), 1)


    def test_python_indexed_imports_are_candidates_and_preserve_partial_unknowns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / 'source'
            (src / 'pkg').mkdir(parents=True)
            (src / 'provider.py').write_text('def target(): return 1\n')
            (src / 'pkg' / '__init__.py').write_text('')
            (src / 'pkg' / 'helper.py').write_text('def helper(): return 2\n')
            (src / 'consumer.py').write_text('import provider as alias, missing\n'
                'from provider import (\n    target,\n)\n'
                'from pkg import helper\n__import__("provider")\n')
            (src / 'pkg' / 'consumer.py').write_text('from .helper import helper\n'
                'from . import helper\nfrom ..provider import target\n'
                'from ...provider import target\n')
            (src / 'ambiguous.py').write_text('')
            (src / 'ambiguous').mkdir()
            (src / 'ambiguous' / '__init__.py').write_text('')
            (src / 'ambiguous_consumer.py').write_text('import ambiguous\n')
            builder = Path(__file__).resolve().parents[1] / 'scripts' / 'code_index_builder.py'
            def build(name, prior=None):
                dest = root / (name + '.sqlite')
                arguments = [sys.executable, str(builder), '--input-root', str(src),
                    '--original-root', '/authorized/source', '--project-id', 'fixture', '--output', str(dest)]
                if prior:
                    arguments += ['--prior', str(prior)]
                result = subprocess.run(arguments, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                return dest
            first = build('first')
            with sqlite3.connect(first) as db:
                edges = set(db.execute("SELECT source,target,line FROM relations WHERE kind='imports'"))
                self.assertEqual(edges, {
                    ('/authorized/source/consumer.py', '/authorized/source/provider.py', 1),
                    ('/authorized/source/consumer.py', '/authorized/source/provider.py', 2),
                    ('/authorized/source/consumer.py', '/authorized/source/pkg/__init__.py', 5),
                    ('/authorized/source/pkg/consumer.py', '/authorized/source/pkg/helper.py', 1),
                    ('/authorized/source/pkg/consumer.py', '/authorized/source/pkg/helper.py', 2),
                    ('/authorized/source/pkg/consumer.py', '/authorized/source/provider.py', 3)})
                pending = list(db.execute('SELECT text,reason FROM unresolved'))
                self.assertTrue(any('import provider as alias, missing' in t and 'partially' in r for t,r in pending))
                self.assertTrue(any(t == 'import ambiguous' for t,r in pending))
                self.assertTrue(any(t == 'from ...provider import target' for t,r in pending))
                self.assertTrue(any('__import__' in t for t,r in pending))
            (src / 'provider.py').unlink()
            deleted = build('deleted', first)
            with sqlite3.connect(deleted) as db:
                self.assertFalse(any(t.endswith('/provider.py') for t, in db.execute('SELECT target FROM relations')))
            (src / 'provider.py').write_text('def target(): return 1\n')
            restored, clean = build('restored', deleted), build('clean')
            with sqlite3.connect(restored) as a, sqlite3.connect(clean) as b:
                for table in ['files', 'symbols', 'relations', 'unresolved', 'syntax_refs']:
                    self.assertEqual(sorted(a.execute('SELECT * FROM '+table)), sorted(b.execute('SELECT * FROM '+table)))
                self.assertEqual(a.execute("SELECT value FROM meta WHERE key='resolver_version'").fetchone(), ('5',))

    def test_python_import_truncated_text_and_module_package_ambiguity_stay_unknown(self):
        from scripts.code_index_builder import python_import_targets
        known = {'/source/a.py', '/source/a/__init__.py', '/source/pkg/b.py'}
        self.assertEqual(python_import_targets('import a', '/source/use.py', '/source', known), ([], False))
        self.assertEqual(python_import_targets('from a import (', '/source/use.py', '/source', known), ([], False))
        self.assertEqual(python_import_targets('import outside', '/source/use.py', '/source', known), ([], False))
        self.assertEqual(python_import_targets('from .b import name', '/source/pkg/use.py', '/source', known),
                         (['/source/pkg/b.py'], True))


if __name__ == "__main__":
    unittest.main()
