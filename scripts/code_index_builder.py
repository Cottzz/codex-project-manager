"""Build a replaceable, source-versioned code cache on the Windows worker.

Only paths under an explicitly supplied source root are scanned. This file is
not imported by the Mac service, and the resulting SQLite DB never owns task
or approval state.
"""

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tree_sitter_language_pack import get_parser

LANGUAGES = {
    ".py": "python", ".mjs": "javascript", ".cjs": "javascript",
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".css": "css", ".html": "html",
    ".ps1": "powershell", ".sh": "bash", ".command": "bash",
}
SYMBOL_KINDS = {
    "function_definition": "function", "function_declaration": "function",
    "function_expression": "function", "arrow_function": "function",
    "method_definition": "method", "class_definition": "class",
    "class_declaration": "class", "function_statement": "function",
    "rule_set": "css_rule", "element": "html_element",
    "script_element": "html_element", "style_element": "html_element",
}
IMPORT_KINDS = {"import_statement", "import_from_statement", "import_declaration",
                "export_statement"}
CALL_KINDS = {"call", "call_expression", "command_invocation", "command"}
# Changing symbol semantics must invalidate same-byte prior caches.
SYMBOL_EXTRACTOR_VERSION = 3



def semantic_capabilities(languages):
    """Declare extraction boundaries; parsed syntax is not runtime completeness."""
    result = {}
    for language in sorted(languages):
        if language in {"python", "javascript", "typescript", "tsx"}:
            imports = ("indexed Python modules" if language == "python"
                       else "unique relative indexed files")
            calls = ("lexically proven same-file names" if language == "python"
                     else "lexically proven same-file names and named relative exports")
            symbols = "named functions, classes and supported methods"
            unknown = "dynamic/member dispatch, external or ambiguous targets; syntax errors do not prove bindings"
        elif language == "css":
            symbols = "selector rule sets including nested rules"
            imports = "quoted relative @import exact indexed files"
            calls = "none; CSS value functions stay syntax references"
            unknown = "url()/external assets, cascade, specificity and runtime styling"
        elif language == "html":
            symbols = "element start tags, script/style and self-closing elements"
            imports = "none"
            calls = "none"
            unknown = "src/href dependencies and embedded script/style semantics"
        else:
            symbols = "named function declarations"
            imports = "none"
            calls = "commands retained as unresolved syntax; no target binding"
            unknown = "source/dot-sourcing, aliases, external commands and runtime scope"
        result[language] = {"symbols": symbols, "imports": imports, "calls": calls,
                            "unknown": unknown, "qualification": "static extraction scope; not runtime or whole-program completeness"}
    return result

def parser_for(language):
    if language == "python":
        from tree_sitter import Language, Parser
        import tree_sitter_python
        return Parser(Language(tree_sitter_python.language()))
    return get_parser(language)


def binary_fingerprint(path):
    """Hash actual Windows worker bindings, never their filename or mtime."""
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"binary": path.name, "bytes": path.stat().st_size,
            "sha256": digest.hexdigest()}


def parser_fingerprints(languages):
    """Same package version alone cannot authorize reuse of different bytes.

    Keep unavailable grammars explicit. Fingerprints own only cache validity;
    official revision/license qualification is a separate bounded receipt.
    """
    from importlib import import_module
    from tree_sitter_language_pack import cache_dir
    result = {}
    try:
        result["core"] = binary_fingerprint(import_module("tree_sitter._binding").__file__)
    except Exception as error:
        result["core"] = {"error": str(error)[:300]}
    for language in sorted(languages):
        try:
            parser = parser_for(language)
            if language == "python":
                path = import_module("tree_sitter_python._binding").__file__
            else:
                paths = [Path(cache_dir()) / ("libtree_sitter_" + language + suffix)
                         for suffix in (".so", ".dll", ".dylib")]
                paths = [path for path in paths if path.is_file()]
                if len(paths) != 1:
                    raise ValueError("actual grammar binary not uniquely identified")
                path = paths[0]
            result[language] = dict(binary_fingerprint(path),
                                    abi_version=parser.language.abi_version,
                                    semantic_version=parser.language.semantic_version)
        except Exception as error:
            result[language] = {"error": str(error)[:300]}
    return result


def decode_name(node, raw):
    name = node.child_by_field_name("name") or node.child_by_field_name("function")
    if name is None and node.type in {"arrow_function", "function_expression"}:
        # A bare arrow parameter is not the function's name. Only a direct,
        # statically named binding supplies one; callbacks remain anonymous.
        value = node
        while value.parent is not None and value.parent.type in {
                "parenthesized_expression", "as_expression", "satisfies_expression"}:
            value = value.parent
        parent = value.parent
        if parent is not None:
            if (parent.type == "variable_declarator"
                    and parent.child_by_field_name("value") == value):
                name = parent.child_by_field_name("name")
            elif (parent.type == "pair"
                    and parent.child_by_field_name("value") == value):
                name = parent.child_by_field_name("key")
            elif (parent.type == "assignment_expression"
                    and parent.child_by_field_name("right") == value):
                name = parent.child_by_field_name("left")
        if name is None or name.type not in {"identifier", "property_identifier"}:
            return None
    if name is None and node.type == "rule_set":
        name = next((c for c in node.named_children if c.type == "selectors"), None)
    if name is None and node.type in {"element", "script_element", "style_element"}:
        name = next((c for c in node.children if c.type in {"start_tag", "self_closing_tag"}), None)
    if name is None:
        name = next((c for c in node.named_children if c.type in {
            "identifier", "property_identifier", "type_identifier", "function_name"}), None)
    if name is None:
        return None
    value = raw[name.start_byte:name.end_byte].decode("utf-8", "replace").strip()
    return value[:180] if value else None


def extract(raw, parser):
    tree = parser.parse(raw)
    rows, unresolved, errors = [], [], []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "ERROR":
            errors.append({"line": node.start_point.row + 1, "kind": "syntax_error"})
        kind = SYMBOL_KINDS.get(node.type)
        if kind:
            name = decode_name(node, raw)
            if name:
                rows.append((kind, name, node.start_point.row + 1, node.end_point.row + 1))
        elif node.type in IMPORT_KINDS | CALL_KINDS:
            value = raw[node.start_byte:node.end_byte].decode("utf-8", "replace")[:260]
            unresolved.append(("import" if node.type in IMPORT_KINDS else "call",
                               value, node.start_point.row + 1,
                               "syntax extracted; target unresolved"))
        stack.extend(reversed(node.named_children))
    if tree.root_node.has_error and not errors:
        errors.append({"line": 1, "kind": "tree_has_error"})
    return rows, unresolved, errors


def same_file_call_bindings(raw, parser, language):
    """Prove lexical visibility before proposing a same-file bare-name edge.

    This is a conservative static candidate, not runtime dispatch proof.
    Rebinding in a containing scope is unknown; member/dynamic calls stay
    unresolved. Equal truncated text/line keys require every occurrence to
    agree, so one call cannot stand in for another on the same source line.
    """
    if language not in {"python", "javascript", "typescript", "tsx"}:
        return {}
    tree = parser.parse(raw)
    if tree.root_node.has_error:
        return {}
    python = language == "python"
    functions = {"function_definition", "function_declaration", "function_expression", "arrow_function", "method_definition", "lambda"}
    scope_types = functions | {"module", "program"}
    if not python:
        scope_types.add("statement_block")
    def walk(node):
        pending = [node]
        while pending:
            current = pending.pop()
            yield current
            pending.extend(reversed(current.named_children))
    def text(node):
        return raw[node.start_byte:node.end_byte].decode("utf-8") if node else ""
    def scope(node, function_only=False):
        current = node.parent
        while current is not None:
            if current.type in scope_types and (not function_only or current.type != "statement_block"):
                return current
            current = current.parent
        return tree.root_node
    def identifiers(node):
        if node is None:
            return set()
        if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
            return {text(node)}
        if node.type in {"default_parameter", "typed_parameter", "typed_default_parameter", "assignment_pattern"}:
            return identifiers(node.child_by_field_name("name") or node.child_by_field_name("left") or node.named_children[0])
        if node.type == "pair_pattern":
            return identifiers(node.child_by_field_name("value"))
        return set().union(*(identifiers(child) for child in node.named_children))
    definitions, blocked, calls = {}, [], {}
    def block(names, owner):
        if names:
            blocked.append((names, owner.start_byte, owner.end_byte))
    for node in walk(tree.root_node):
        if node.type in functions | {"class_definition", "class_declaration"}:
            block(identifiers(node.child_by_field_name("parameters") or node.child_by_field_name("parameter")), node)
            name = decode_name(node, raw)
            allowed = bool(name) and node.type not in {"method_definition", "lambda"}
            ancestor = node.parent
            while python and ancestor is not None:
                if ancestor.type == "class_definition":
                    allowed = False
                    break
                if ancestor.type in functions:
                    break
                ancestor = ancestor.parent
            if node.type in {"arrow_function", "function_expression"}:
                parent = node.parent
                allowed = (allowed and parent is not None and parent.type == "variable_declarator"
                           and parent.child_by_field_name("value") == node
                           and parent.child_by_field_name("name") is not None
                           and parent.child_by_field_name("name").type == "identifier"
                           and text(parent.child_by_field_name("name")) == name
                           and node.child_by_field_name("name") is None)
            if allowed:
                owner = scope(node)
                key = (name, node.start_point.row + 1)
                definitions.setdefault(key, []).append((owner.start_byte, owner.end_byte))
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            named_function = (value is not None and value.type in {"arrow_function", "function_expression"}
                              and node.child_by_field_name("name") is not None
                              and text(node.child_by_field_name("name")) == decode_name(value, raw)
                              and value.child_by_field_name("name") is None)
            if not named_function:
                block(identifiers(node.child_by_field_name("name")), scope(node, function_only=node.parent is not None and node.parent.type == "variable_declaration"))
        if node.type in {"assignment", "augmented_assignment", "named_expression", "assignment_expression", "augmented_assignment_expression"}:
            block(identifiers(node.child_by_field_name("left") or node.child_by_field_name("name")), scope(node, function_only=True))
        if node.type in {"for_statement", "for_in_statement"}:
            block(identifiers(node.child_by_field_name("left")), scope(node, function_only=python))
        if node.type in {"catch_clause", "except_clause"}:
            block(identifiers(node.child_by_field_name("parameter") or node.child_by_field_name("alias")), node)
        if python and node.type == "as_pattern":
            block(identifiers(node.child_by_field_name("alias")), scope(node, function_only=True))
        if python and node.type == "delete_statement":
            block(identifiers(node), scope(node, function_only=True))
        if python and node.type == "for_in_clause":
            owner = node.parent
            while owner is not None and owner.type not in {"list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression"}:
                owner = owner.parent
            if owner is not None:
                block(identifiers(node.child_by_field_name("left")), owner)
        if python and node.type in {"global_statement", "nonlocal_statement"}:
            block(identifiers(node), scope(node, function_only=True))
        if python and node.type in {"import_statement", "import_from_statement"}:
            try:
                statement = ast.parse(text(node)).body[0]
                imported = {alias.asname or (alias.name.split(".")[0] if isinstance(statement, ast.Import) else alias.name)
                            for alias in statement.names}
            except (SyntaxError, AttributeError):
                imported = {"*"}
            block(imported, scope(node, function_only=True))
        if node.type in CALL_KINDS:
            value = text(node)[:260]
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", value)
            if match:
                caller = node.parent
                while caller is not None and caller.type not in functions:
                    caller = caller.parent
                caller_name = decode_name(caller, raw) if caller is not None and caller.type != "lambda" else None
                caller_key = (caller_name, caller.start_point.row + 1, caller.end_point.row + 1) if caller_name else None
                calls.setdefault((value, node.start_point.row + 1), []).append((match.group(1), node.start_byte, caller_key))
    result = {}
    for key, occurrences in calls.items():
        allowed = set()
        for definition, owners in definitions.items():
            if len(owners) != 1:
                continue
            name, _ = definition
            start, end = owners[0]
            if all(callee == name and start <= position < end
                   and not any((name in names or "*" in names) and a <= position < b for names, a, b in blocked)
                   for callee, position, _ in occurrences):
                allowed.add(definition)
        callers = {caller for _, _, caller in occurrences}
        result[key] = {"definitions": allowed, "caller": next(iter(callers)) if len(callers) == 1 else None,
                       "caller_ambiguous": len(callers) != 1}
    return result


def python_import_targets(value, path, source_root, known):
    """Indexed file candidates, not interpreter/sys.path or member resolution."""
    try:
        nodes = ast.parse(value.strip()).body
    except (SyntaxError, ValueError):
        return [], False
    if len(nodes) != 1 or not isinstance(nodes[0], (ast.Import, ast.ImportFrom)):
        return [], False
    statement = nodes[0]
    root = Path(source_root)
    modules = []
    if isinstance(statement, ast.Import):
        modules = [root.joinpath(*alias.name.split(".")) for alias in statement.names]
    else:
        base = root
        if statement.level:
            base = Path(path).parent
            for _ in range(statement.level - 1):
                base = base.parent
            try:
                base.relative_to(root)
            except ValueError:
                return [], False
        if statement.module:
            modules = [base.joinpath(*statement.module.split("."))]
        else:
            modules = [base if alias.name == "*" else base / alias.name
                       for alias in statement.names]
    targets, complete = set(), True
    for module in modules:
        matches = [candidate for candidate in
                   (str(module.with_suffix(".py")), str(module / "__init__.py"))
                   if candidate in known]
        if len(matches) == 1:
            targets.add(matches[0])
        else:
            complete = False
    return sorted(targets), complete and bool(modules)


def javascript_bindings(raw, parser):
    """Direct named imports/exports only; any local namesake stays unknown.

    File-wide shadow exclusion is intentionally conservative. This does not
    model live bindings, execution order, re-exports or namespace members.
    """
    tree = parser.parse(raw)
    if tree.root_node.has_error:
        return {}, set(), set()
    def walk(node):
        yield node
        for child in node.named_children:
            yield from walk(child)
    def text(node):
        return raw[node.start_byte:node.end_byte].decode("utf-8") if node else ""
    def names(node):
        return {text(child) for child in walk(node)
                if child.type in {"identifier", "shorthand_property_identifier_pattern"}} if node else set()
    imports, exports, shadowed = {}, set(), set()
    for node in walk(tree.root_node):
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            module = text(source)
            if len(module) < 2 or module[0] not in {"'", '"'} or module[-1] != module[0]:
                continue
            module = module[1:-1]
            if not module.startswith(".") or "\\" in module:
                continue
            for spec in walk(node):
                if spec.type != "import_specifier":
                    continue
                imported = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias") or imported
                # Strings and type-only imports are not callable identifiers.
                if imported is None or alias is None or imported.type != "identifier":
                    continue
                if text(spec).lstrip().startswith("type ") or text(node).lstrip().startswith("import type "):
                    continue
                imports.setdefault(text(alias), []).append((module, text(imported)))
        if node.type == "export_statement":
            declaration = node.child_by_field_name("declaration")
            if declaration is None:
                continue
            if text(node).lstrip().startswith("export default "):
                continue
            if declaration.type == "function_declaration":
                name = declaration.child_by_field_name("name")
                if name:
                    exports.add(text(name))
            elif declaration.type in {"lexical_declaration", "variable_declaration"}:
                for binding in declaration.named_children:
                    name = binding.child_by_field_name("name")
                    value = binding.child_by_field_name("value")
                    if (name is not None and name.type == "identifier" and value is not None
                            and value.type in {"arrow_function", "function_expression"}):
                        # Named function expressions use their own internal name
                        # in the symbol table, so only arrows/anonymous expressions
                        # whose binding is the actual indexed name are candidates.
                        if value.type == "arrow_function" or value.child_by_field_name("name") is None:
                            exports.add(text(name))
        parameters = node.child_by_field_name("parameters")
        if parameters:
            shadowed.update(names(parameters))
        if node.type == "arrow_function":
            shadowed.update(names(node.child_by_field_name("parameter")))
        if node.type in {"variable_declarator", "function_declaration", "function_expression",
                          "class_declaration", "catch_clause"}:
            shadowed.update(names(node.child_by_field_name("name") or node.child_by_field_name("parameter")))
    return {name: entries[0] for name, entries in imports.items() if len(entries) == 1}, exports, shadowed


def setup(db):
    db.executescript("""
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE files(path TEXT PRIMARY KEY, language TEXT NOT NULL, sha256 TEXT NOT NULL,
      bytes INTEGER NOT NULL, status TEXT NOT NULL, errors TEXT NOT NULL);
    CREATE TABLE symbols(id TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL,
      name TEXT NOT NULL, line INTEGER NOT NULL, end_line INTEGER NOT NULL);
    CREATE INDEX symbols_name ON symbols(name);
    CREATE TABLE unresolved(path TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
      line INTEGER NOT NULL, reason TEXT NOT NULL);
    CREATE INDEX unresolved_path ON unresolved(path);
    CREATE TABLE relations(source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
      method TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL);
    CREATE INDEX relations_source ON relations(source);
    CREATE INDEX relations_target ON relations(target);
    CREATE TABLE syntax_refs(path TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                             line INTEGER NOT NULL, reason TEXT NOT NULL);
    """)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-root", required=True)
    ap.add_argument("--original-root", required=True)
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--prior")
    args = ap.parse_args()
    root = Path(args.input_root).resolve()
    out = Path(args.output).resolve()
    if not root.is_dir() or not Path(args.original_root).is_absolute():
        ap.error("source roots must exist and original root must be absolute")
    if out.exists():
        ap.error("output already exists; use a new version path")
    prior = sqlite3.connect(f"file:{Path(args.prior).resolve()}?mode=ro", uri=True) if args.prior else None
    parser_versions = {}
    for package in ("tree-sitter", "tree-sitter-language-pack", "tree-sitter-python"):
        try:
            parser_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            parser_versions[package] = "unknown"
    languages = {LANGUAGES[file.suffix.lower()] for file in root.rglob("*")
                 if file.is_file() and not file.is_symlink() and not file.name.startswith("._")
                 and file.suffix.lower() in LANGUAGES}
    fingerprints = parser_fingerprints(languages)
    fingerprints_complete = all("sha256" in value for value in fingerprints.values())
    fingerprint_contract = json.dumps(fingerprints, sort_keys=True)
    prior_fingerprints = prior.execute(
        "SELECT value FROM meta WHERE key='grammar_fingerprints'").fetchone() if prior else None
    parser_contract = json.dumps(parser_versions, sort_keys=True)
    prior_contract = prior.execute("SELECT value FROM meta WHERE key='parser_versions'").fetchone() if prior else None
    prior_symbols = prior.execute("SELECT value FROM meta WHERE key='symbol_extractor_version'").fetchone() if prior else None
    reuse_parser = (fingerprints_complete and prior_fingerprints == (fingerprint_contract,)
                    and prior_contract == (parser_contract,)
                    and prior_symbols == (str(SYMBOL_EXTRACTOR_VERSION),)
                    and "unknown" not in parser_versions.values())
    out.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(out)
    setup(db)
    parsers = {}
    counts = Counter()
    try:
        for file in sorted(root.rglob("*")):
            if file.name.startswith("._") or not file.is_file() or file.is_symlink():
                continue
            original = str(Path(args.original_root) / file.relative_to(root))
            language = LANGUAGES.get(file.suffix.lower())
            if not language:
                db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)",
                           (original, "unclassified", "", file.stat().st_size,
                            "unsupported_extension", "[]"))
                counts["unsupported_extension"] += 1
                continue
            raw = file.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            if len(raw) > 1024 * 1024 or b"\0" in raw:
                status, errors = "skipped_size_or_binary", [{"kind": "limit_or_binary"}]
                symbols, unresolved = [], []
                prior_relations = []
            elif (reuse_parser and prior.execute("SELECT value FROM meta WHERE key='syntax_refs_version'").fetchone() == ('1',)
                  and prior.execute("SELECT sha256,status FROM files WHERE path=?", (original,)).fetchone() == (sha, "parsed")):
                status, errors = "parsed", []
                symbols = [(r[1], r[2], r[3], r[4]) for r in prior.execute(
                    "SELECT id,kind,name,line,end_line FROM symbols WHERE path=?", (original,))]
                # Keep every syntax reference, including previously resolved
                # imports. Resolve against the current file set, not old edges.
                unresolved = list(prior.execute("SELECT kind,text,line,reason FROM syntax_refs WHERE path=?", (original,)))
                prior_relations = []
                counts["reused"] += 1
            else:
                try:
                    if language not in parsers:
                        parsers[language] = parser_for(language)
                    parser = parsers[language]
                    print(f"parsing {original} ({language})", flush=True)
                    symbols, unresolved, errors = extract(raw, parser)
                    status = "parse_error" if errors else "parsed"
                    prior_relations = []
                except Exception as error:
                    symbols, unresolved = [], []
                    prior_relations = []
                    errors = [{"kind": "parser_unavailable", "detail": str(error)[:300]}]
                    status = "parser_unavailable"
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)",
                       (original, language, sha, len(raw), status,
                        json.dumps(errors, ensure_ascii=False)))
            for kind, name, line, end_line in symbols:
                identity = hashlib.sha256(f"{original}:{kind}:{name}:{line}".encode()).hexdigest()[:24]
                db.execute("INSERT OR IGNORE INTO symbols VALUES (?,?,?,?,?,?)",
                           (identity, original, kind, name, line, end_line))
            db.executemany("INSERT INTO unresolved VALUES (?,?,?,?,?)",
                           [(original, *row) for row in unresolved])
            db.executemany("INSERT INTO syntax_refs VALUES (?,?,?,?,?)",
                           [(original, *row) for row in unresolved])
            db.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?)", prior_relations)
            counts["reused_relations"] += len(prior_relations)
            counts[status] += 1
            counts[language] += 1
        known = {row[0] for row in db.execute("SELECT path FROM files WHERE status='parsed'")}
        db.execute("DELETE FROM relations WHERE target NOT IN (SELECT id FROM symbols) "
                   "AND target NOT IN (SELECT path FROM files)")
        file_languages = dict(db.execute("SELECT path,language FROM files"))
        by_name = {}
        enclosing = {}
        for identity, path, kind, name, start, end in db.execute(
                "SELECT id,path,kind,name,line,end_line FROM symbols"):
            by_name.setdefault((path, name), []).append(identity)
            if kind in {"function", "method"}:
                enclosing.setdefault(path, []).append((start, end, identity))
        # Syntax reuse remains valid, but binding candidates are rebuilt from
        # the frozen originals each time; old relations never own this truth.
        js_bindings, js_exports, js_shadowed = {}, {}, {}
        for path in sorted(known):
            language = file_languages[path]
            if language not in {"javascript", "typescript", "tsx"}:
                continue
            file = root / Path(path).relative_to(Path(args.original_root))
            if language not in parsers:
                parsers[language] = parser_for(language)
            bindings, exports, shadowed = javascript_bindings(file.read_bytes(), parsers[language])
            js_bindings[path], js_exports[path], js_shadowed[path] = bindings, exports, shadowed
        lexical_bindings = {}
        for path in sorted(known):
            language = file_languages[path]
            if language not in {"python", "javascript", "typescript", "tsx"}:
                continue
            file = root / Path(path).relative_to(Path(args.original_root))
            if language not in parsers:
                parsers[language] = parser_for(language)
            lexical_bindings[path] = same_file_call_bindings(file.read_bytes(), parsers[language], language)
        for rowid, path, kind, value, line in list(db.execute(
                "SELECT rowid,path,kind,text,line FROM unresolved")):
            target = None
            if kind == "import" and file_languages.get(path) == "python":
                targets, complete = python_import_targets(value, path, args.original_root, known)
                for imported in targets:
                    db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                               (path, imported, "imports", "indexed_python_module_candidate", path, line))
                    counts["resolved_import"] += 1
                if complete:
                    db.execute("DELETE FROM unresolved WHERE rowid=?", (rowid,))
                elif targets:
                    db.execute("UPDATE unresolved SET reason=? WHERE rowid=?",
                               ("partially resolved indexed modules; remaining targets unknown", rowid))
                continue
            if kind == "call":
                match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", value)
                possible = by_name.get((path, match.group(1)), []) if match else []
                if len(possible) == 1:
                    candidate = possible[0]
                    definition = db.execute("SELECT name,line,kind FROM symbols WHERE id=?", (candidate,)).fetchone()
                    policy = lexical_bindings.get(path, {}).get((value, line), {})
                    caller = policy.get("caller")
                    callers = db.execute("SELECT id FROM symbols WHERE path=? AND name=? AND line=? AND end_line=?",
                                         (path, *caller)).fetchall() if caller else []
                    if (definition and definition[2] in {"function", "class"}
                            and (definition[0], definition[1]) in policy.get("definitions", set())
                            and not policy.get("caller_ambiguous", True)
                            and (not caller or len(callers) == 1)):
                        target = candidate
                        source = callers[0][0] if caller else path
                        method = "lexical_same_file_candidate"
                    else:
                        db.execute("UPDATE unresolved SET reason=? WHERE rowid=?",
                                   ("same-file namesake exists; lexical binding or caller identity not proven", rowid))
                elif match and not possible:
                    name = match.group(1)
                    binding = js_bindings.get(path, {}).get(name)
                    if binding and name not in js_shadowed.get(path, set()):
                        module, imported_name = binding
                        base = Path(os.path.normpath(str(Path(path).parent / module)))
                        candidates = [str(base)]
                        if not base.suffix:
                            extensions = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
                            candidates.extend(str(base.with_suffix(ext)) for ext in extensions)
                            candidates.extend(str(base / ("index" + ext)) for ext in extensions)
                        matches = [candidate for candidate in candidates if candidate in known]
                        if len(matches) == 1 and imported_name in js_exports.get(matches[0], set()):
                            targets = by_name.get((matches[0], imported_name), [])
                            if len(targets) == 1:
                                target = targets[0]
                                parent = [item for item in enclosing.get(path, []) if item[0] <= line <= item[1]]
                                source = min(parent, key=lambda item: item[1] - item[0])[2] if parent else path
                                method = "named_relative_export_candidate"
            else:
                match = re.search(r"\b(?:from|import)\s+['\"](\.[^'\"]+)['\"]", value)
                if match:
                    base = Path(os.path.normpath(str(Path(path).parent / match.group(1))))
                    # An explicit extension names exactly that indexed file.
                    # Do not redirect './x.js' to x.ts or a missing './x.css' to x.js.
                    candidates = [str(base)]
                    if not base.suffix:
                        extensions = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
                        candidates.extend(str(base.with_suffix(ext)) for ext in extensions)
                        candidates.extend(str(base / ("index" + ext)) for ext in extensions)
                    matches = [candidate for candidate in candidates if candidate in known]
                    if len(matches) == 1:
                        source, target, method = path, matches[0], "relative_import_exact_file"
            if target:
                db.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                           (source, target, "calls" if kind == "call" else "imports",
                            method, path, line))
                db.execute("DELETE FROM unresolved WHERE rowid=?", (rowid,))
                counts["resolved_" + kind] += 1
        meta = {"project_id": args.project_id, "source_root": args.original_root,
                "syntax_refs_version": 1,
                "symbol_extractor_version": SYMBOL_EXTRACTOR_VERSION,
                "semantic_capabilities": semantic_capabilities(set(file_languages.values())),
                "resolver_version": 5,
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "parser": "; ".join(f"{package} {version}" for package, version in parser_versions.items()),
                "parser_versions": parser_versions,
                "grammar_fingerprints": fingerprints,
                "counts": dict(counts)}
        db.executemany("INSERT INTO meta VALUES (?,?)",
                       [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in meta.items()])
        db.commit()
        print(json.dumps(meta, ensure_ascii=False))
    finally:
        db.close()
        if prior:
            prior.close()


if __name__ == "__main__":
    main()
