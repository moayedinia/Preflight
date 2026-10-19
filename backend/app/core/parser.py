import os
import re
import ast
from pathlib import Path
from typing import Optional, Dict, Any, List

# Try importing Tree-sitter core
try:
    from tree_sitter import Language, Parser
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    Parser = None
    Language = None

# Cache for initialized Tree-sitter parsers
_PARSERS: Dict[str, Any] = {}

def _init_parser(lang_name: str, grammar_module: str, grammar_func: str = "language"):
    """Dynamically loads and caches a Tree-sitter parser grammar if installed."""
    if not TREE_SITTER_AVAILABLE:
        return None
    if lang_name in _PARSERS:
        return _PARSERS[lang_name]

    try:
        mod = __import__(grammar_module)
        fn = getattr(mod, grammar_func)
        lang_ptr = fn()
        try:
            ts_lang = Language(lang_ptr, lang_name)
        except TypeError:
            ts_lang = Language(lang_ptr)

        try:
            p = Parser()
            if hasattr(p, "set_language"):
                p.set_language(ts_lang)
            else:
                p = Parser(ts_lang)
        except Exception:
            p = Parser(ts_lang)

        _PARSERS[lang_name] = p
        return p
    except Exception as e:
        return None

# Mapping of file extensions to language identifiers
EXTENSION_MAP = {
    # Python
    ".py": "python", ".pyw": "python",
    # JavaScript & TypeScript
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    # Go
    ".go": "go",
    # Java & JVM
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    # C & C++
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    # C#
    ".cs": "c_sharp",
    # Rust
    ".rs": "rust",
    # Web backend
    ".php": "php", ".rb": "ruby",
    # Mobile
    ".swift": "swift", ".dart": "dart",
    # Scripts & Data
    ".sh": "shell", ".bash": "shell", ".sql": "sql", ".r": "r", ".lua": "lua"
}

def detect_language(file_path: str) -> str:
    """Detects programming language from file extension."""
    ext = Path(file_path).suffix.lower()
    return EXTENSION_MAP.get(ext, "unknown")

# --- Encoding and Text Handlers for Multi-Encoding / Persian Source Code ---

def decode_source_code(raw_bytes: bytes) -> tuple[str, bytes]:
    """
    Decodes source code bytes into Unicode text and canonical UTF-8 bytes.
    Accurately handles UTF-8, UTF-8-BOM (Visual Studio), UTF-16 LE/BE,
    Windows-1256 (Persian/Arabic ANSI), and Latin-1 without replacing characters with question marks.
    Returns:
        (code_text: str, utf8_bytes: bytes)
    """
    if not raw_bytes:
        return "", b""

    # 1. Check explicit BOMs first (UTF-8 BOM or UTF-16 LE/BE BOM)
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        try:
            text = raw_bytes.decode("utf-8-sig")
            return text, text.encode("utf-8")
        except UnicodeDecodeError:
            pass

    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        for enc in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                text = raw_bytes.decode(enc)
                return text, text.encode("utf-8")
            except (UnicodeDecodeError, LookupError):
                pass

    # 2. Clean UTF-8 (Strict)
    try:
        text = raw_bytes.decode("utf-8")
        return text, raw_bytes
    except UnicodeDecodeError:
        pass

    # 3. UTF-16 without BOM (detect null-byte cadence common in UTF-16 code files)
    if len(raw_bytes) >= 4 and (raw_bytes[1::2].count(0) > len(raw_bytes) // 4 or raw_bytes[0::2].count(0) > len(raw_bytes) // 4):
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                text = raw_bytes.decode(enc)
                return text, text.encode("utf-8")
            except (UnicodeDecodeError, LookupError):
                pass

    # 4. Windows-1256 / CP1256 (Standard Persian/Arabic Windows ANSI encoding)
    try:
        text = raw_bytes.decode("cp1256")
        return text, text.encode("utf-8")
    except UnicodeDecodeError:
        pass

    # 5. Latin-1 / ISO-8859-1 fallback
    try:
        text = raw_bytes.decode("latin-1")
        return text, text.encode("utf-8")
    except Exception:
        pass

    # 6. Absolute fallback: UTF-8 with replacement
    text = raw_bytes.decode("utf-8", errors="replace")
    return text, text.encode("utf-8")

def _get_node_text(node, code_bytes: bytes) -> str:
    return code_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

def _parse_with_treesitter(p: Parser, lang: str, file_path: str, code_bytes: bytes) -> dict:
    """AST parsing for languages supported by Tree-sitter grammars."""
    tree = p.parse(code_bytes)
    root = tree.root_node

    entities = {
        "file": file_path,
        "language": lang,
        "classes": [],
        "functions": [],
        "imports": []
    }

    class_types = {
        "python": ["class_definition"],
        "javascript": ["class_declaration"],
        "typescript": ["class_declaration", "interface_declaration"],
        "go": ["type_declaration"],
        "java": ["class_declaration", "interface_declaration", "enum_declaration"],
        "cpp": ["class_specifier", "struct_specifier"],
        "c": ["struct_specifier"],
        "c_sharp": ["class_declaration", "interface_declaration", "struct_declaration"],
        "rust": ["struct_item", "trait_item", "impl_item"]
    }.get(lang, ["class_definition", "class_declaration"])

    func_types = {
        "python": ["function_definition"],
        "javascript": ["function_declaration", "method_definition", "arrow_function"],
        "typescript": ["function_declaration", "method_definition"],
        "go": ["function_declaration", "method_declaration"],
        "java": ["method_declaration", "constructor_declaration"],
        "cpp": ["function_definition"],
        "c": ["function_definition"],
        "c_sharp": ["method_declaration", "constructor_declaration"],
        "rust": ["function_item"]
    }.get(lang, ["function_definition", "function_declaration", "method_declaration"])

    import_types = [
        "import_statement", "import_from_statement", "import_declaration",
        "use_declaration", "preproc_include", "using_directive"
    ]

    def _find_identifier(node) -> str:
        # Check standard field names first
        for field in ["name", "declarator", "identifier"]:
            fn_child = node.child_by_field_name(field)
            if fn_child:
                if fn_child.type in ["identifier", "property_identifier", "field_identifier", "type_identifier"]:
                    text = _get_node_text(fn_child, code_bytes).strip()
                    if text:
                        return text
                # If declarator (e.g. C/C++ function_declarator), inspect its children
                for sub in fn_child.children:
                    if sub.type in ["identifier", "property_identifier", "field_identifier", "type_identifier"]:
                        text = _get_node_text(sub, code_bytes).strip()
                        if text:
                            return text

        # Check direct children
        for child in node.children:
            if child.type in ["identifier", "property_identifier", "field_identifier", "type_identifier", "name"]:
                text = _get_node_text(child, code_bytes).strip()
                if text:
                    return text

        # Check one level deeper for declarators or compound names
        for child in node.children:
            for sub in child.children:
                if sub.type in ["identifier", "property_identifier", "field_identifier"]:
                    text = _get_node_text(sub, code_bytes).strip()
                    if text:
                        return text

        return "Anonymous"

    def _find_calls(node) -> list[str]:
        calls = set()
        CALL_IGNORE = {"if", "for", "while", "switch", "catch", "try", "return", "print", "println", "console", "len", "range", "str", "int", "float", "bool", "list", "dict", "set"}
        def _scan(curr):
            if curr.type in ["call", "call_expression", "method_invocation"]:
                fn_child = curr.child_by_field_name("function") or curr.child_by_field_name("name")
                if fn_child:
                    raw_name = _get_node_text(fn_child, code_bytes).split(".")[-1].split("->")[-1].strip()
                    clean = re.sub(r'[^A-Za-z0-9_]', '', raw_name)
                    if clean and len(clean) >= 2 and clean.lower() not in CALL_IGNORE:
                        calls.add(clean)
            for ch in curr.children:
                _scan(ch)
        _scan(node)
        return list(calls)

    def _traverse(node, current_class: Optional[str] = None):
        if node.type in class_types:
            cls_name = _find_identifier(node)
            raw_code = _get_node_text(node, code_bytes)
            # Extract base classes / superclasses
            bases = []
            for ch in node.children:
                if ch.type in ["argument_list", "class_heritage", "superclasses", "base_class_clause", "type_parameters"]:
                    for sub in ch.children:
                        if sub.type in ["identifier", "type_identifier", "property_identifier"]:
                            b_text = _get_node_text(sub, code_bytes).strip()
                            if b_text and b_text not in ["extends", "implements", "public", "private", "protected"]:
                                bases.append(b_text)

            cls_obj = {
                "name": cls_name,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "code": raw_code[:4000],
                "docstring": "",
                "bases": bases,
                "methods": []
            }
            entities["classes"].append(cls_obj)
            for ch in node.children:
                _traverse(ch, current_class=cls_name)
            return

        if node.type in func_types:
            fn_name = _find_identifier(node)
            raw_code = _get_node_text(node, code_bytes)
            calls = _find_calls(node)
            fn_obj = {
                "name": fn_name,
                "parent_class": current_class,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "code": raw_code[:4000],
                "docstring": "",
                "calls": calls
            }
            if current_class:
                # Find class and append method
                for c in entities["classes"]:
                    if c["name"] == current_class:
                        c["methods"].append(fn_obj)
                        break
            else:
                entities["functions"].append(fn_obj)
            return

        if node.type in import_types:
            entities["imports"].append(_get_node_text(node, code_bytes).strip())
            return

        for ch in node.children:
            _traverse(ch, current_class=current_class)

    _traverse(root)
    return entities

# --- Universal Polyglot Fallback Parser (For ALL Other Languages) ---

RE_CLASS = re.compile(
    r'^\s*(?:export\s+|public\s+|private\s+|protected\s+|open\s+|abstract\s+|final\s+)*'
    r'(?:class|struct|interface|trait|type)\s+([A-Za-z0-9_]+)(?:\s*(?:\(|extends\s+|:\s*)([A-Za-z0-9_,\s\.]+))?',
    re.MULTILINE
)

RE_FUNCTION = re.compile(
    r'^\s*(?:async\s+|public\s+|private\s+|protected\s+|static\s+|def\s+|func\s+|fn\s+|function\s+|sub\s+|fun\s+|void\s+|int\s+|string\s+)*'
    r'(?:def|func|fn|function|fun|sub)?\s*([A-Za-z0-9_]+)\s*\([^)]*\)\s*(?:->|\{|:|=)',
    re.MULTILINE
)

RE_IMPORT = re.compile(
    r'^\s*(?:import|from|#include|use|require|package|using)\s+([A-Za-z0-9_\.\/<>\"]+)',
    re.MULTILINE
)

def _parse_python_ast(file_path: str, text: str) -> Optional[dict]:
    """
    Standard Library AST Parser for Python:
    Extracts 100% precise class definitions, methods, docstrings, decorators,
    call expressions, imports, and imported symbols without external binary dependencies.
    """
    try:
        tree = ast.parse(text)
    except Exception:
        return None

    lines = text.splitlines()
    entities = {
        "file": file_path,
        "language": "python",
        "classes": [],
        "functions": [],
        "imports": [],
        "imported_symbols": {},
        "api_routes": [],
        "api_clients": [],
        "is_test": "test" in file_path.lower() or "spec" in file_path.lower()
    }

    # Extract imports and imported symbols
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                entities["imports"].append(f"import {alias.name}")
                as_name = alias.asname or alias.name
                entities["imported_symbols"][as_name] = alias.name
        elif isinstance(stmt, ast.ImportFrom):
            mod = stmt.module or ""
            names = [a.name for a in stmt.names]
            entities["imports"].append(f"from {mod} import {', '.join(names)}")
            for alias in stmt.names:
                as_name = alias.asname or alias.name
                entities["imported_symbols"][as_name] = f"{mod}.{alias.name}" if mod else alias.name

    def _extract_calls(fn_node) -> list[str]:
        calls = set()
        CALL_IGNORE = {"if", "for", "while", "print", "len", "range", "str", "int", "float", "bool", "list", "dict", "set", "super"}
        for sub in ast.walk(fn_node):
            if isinstance(sub, ast.Call):
                name = None
                if isinstance(sub.func, ast.Name):
                    name = sub.func.id
                elif isinstance(sub.func, ast.Attribute):
                    name = sub.func.attr
                if name and len(name) >= 2 and name not in CALL_IGNORE:
                    calls.add(name)
        return list(calls)

    def _extract_decorators(fn_node) -> list[str]:
        decs = []
        for d in getattr(fn_node, "decorator_list", []):
            try:
                decs.append(ast.unparse(d))
            except Exception:
                pass
        return decs

    # Extract classes and methods
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            bases = []
            for b in stmt.bases:
                try:
                    bases.append(ast.unparse(b).split(".")[-1])
                except Exception:
                    pass

            start_l = stmt.lineno
            end_l = getattr(stmt, "end_lineno", min(start_l + 80, len(lines)))
            raw_code = "\n".join(lines[start_l - 1:end_l])

            cls_methods = []
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_start = item.lineno
                    m_end = getattr(item, "end_lineno", min(m_start + 40, len(lines)))
                    m_code = "\n".join(lines[m_start - 1:m_end])
                    m_calls = _extract_calls(item)
                    m_decs = _extract_decorators(item)
                    cls_methods.append({
                        "name": item.name,
                        "parent_class": stmt.name,
                        "start_line": m_start,
                        "end_line": m_end,
                        "code": m_code[:4000],
                        "docstring": ast.get_docstring(item) or "",
                        "decorators": m_decs,
                        "calls": m_calls
                    })

            entities["classes"].append({
                "name": stmt.name,
                "start_line": start_l,
                "end_line": end_l,
                "code": raw_code[:4000],
                "docstring": ast.get_docstring(stmt) or "",
                "bases": bases,
                "base_classes": bases,
                "methods": cls_methods
            })
            entities["functions"].extend(cls_methods)

        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_start = stmt.lineno
            fn_end = getattr(stmt, "end_lineno", min(fn_start + 40, len(lines)))
            fn_code = "\n".join(lines[fn_start - 1:fn_end])
            fn_calls = _extract_calls(stmt)
            fn_decs = _extract_decorators(stmt)

            test_target = None
            if stmt.name.startswith("test_") and len(stmt.name) > 5:
                raw_target = stmt.name[5:]
                for c in fn_calls:
                    if c in raw_target:
                        raw_target = c
                        break
                test_target = raw_target

            entities["functions"].append({
                "name": stmt.name,
                "parent_class": None,
                "start_line": fn_start,
                "end_line": fn_end,
                "code": fn_code[:4000],
                "docstring": ast.get_docstring(stmt) or "",
                "decorators": fn_decs,
                "calls": fn_calls,
                "test_target": test_target
            })

    return entities

def _parse_scoped_code(file_path: str, lang: str, code_text: str) -> dict:
    """
    Scoped Block Parser with Brace Matching:
    Properly scopes methods inside class braces { ... }, eliminates call bleeding,
    and extracts imports and test targets for JavaScript/TypeScript, Go, Java, C#, C++, Rust.
    """
    lines = code_text.splitlines()
    entities = {
        "file": file_path,
        "language": lang,
        "classes": [],
        "functions": [],
        "imports": [],
        "imported_symbols": {},
        "api_routes": [],
        "api_clients": [],
        "is_test": "test" in file_path.lower() or "spec" in file_path.lower()
    }

    # 1. ES6 import statements: import { a, b as c } from './module'; or import React from 'react';
    re_es6 = re.compile(r'import\s+(?:(?:\*\s+as\s+([A-Za-z0-9_]+)|{([^}]+)}|([A-Za-z0-9_]+))\s+from\s+)?[\'"]([^\'"]+)[\'"]')
    for m in re_es6.finditer(code_text):
        wildcard, braced, default_id, mod = m.groups()
        entities["imports"].append(m.group(0).strip())
        if wildcard:
            entities["imported_symbols"][wildcard] = mod
        if default_id:
            entities["imported_symbols"][default_id] = mod
        if braced:
            for item in braced.split(","):
                parts = [p.strip() for p in item.split(" as ")]
                sym = parts[-1]
                if sym:
                    entities["imported_symbols"][sym] = f"{mod}.{parts[0]}"

    # 2. CommonJS require: const { a, b } = require('./module');
    re_cjs = re.compile(r'(?:const|let|var)\s+(?:{([^}]+)}|([A-Za-z0-9_]+))\s*=\s*require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)')
    for m in re_cjs.finditer(code_text):
        braced, single, mod = m.groups()
        entities["imports"].append(m.group(0).strip())
        if single:
            entities["imported_symbols"][single] = mod
        if braced:
            for item in braced.split(","):
                sym = item.strip()
                if sym:
                    entities["imported_symbols"][sym] = f"{mod}.{sym}"

    # 3. Go, Java, C# import / package / using statements
    re_general_import = re.compile(r'^\s*(?:package|import|using)\s+([A-Za-z0-9_\.\/<>\"]+)', re.MULTILINE)
    for m in re_general_import.finditer(code_text):
        line = m.group(0).strip()
        if not any(line in imp for imp in entities["imports"]):
            entities["imports"].append(line)
            mod_sym = m.group(1).replace('"', '').replace(';', '').split('/')[-1].split('.')[-1]
            if mod_sym:
                entities["imported_symbols"][mod_sym] = m.group(1).replace('"', '').replace(';', '')

    def find_closing_brace(start_pos: int) -> int:
        depth = 0
        in_string = False
        str_char = None
        for i in range(start_pos, len(code_text)):
            c = code_text[i]
            if in_string:
                if c == str_char and code_text[i-1] != '\\':
                    in_string = False
                continue
            if c in ('"', "'", '`'):
                in_string = True
                str_char = c
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return i
        return len(code_text)

    CALL_IGNORE = {"if", "for", "while", "switch", "catch", "try", "return", "print", "println", "console", "require", "import", "fetch", "super", "constructor"}
    def extract_calls_from_text(slice_text: str, self_name: str) -> list[str]:
        calls = set()
        for cm in re.finditer(r'([A-Za-z0-9_]+)\s*\(', slice_text):
            call_n = cm.group(1)
            if call_n != self_name and call_n not in CALL_IGNORE and len(call_n) >= 2:
                calls.add(call_n)
        return list(calls)

    line_offsets = []
    curr_off = 0
    for l in lines:
        line_offsets.append(curr_off)
        curr_off += len(l) + 1

    def offset_to_line(offset: int) -> int:
        for idx, lo in enumerate(line_offsets, 1):
            if offset < lo:
                return max(1, idx - 1)
        return len(lines)

    # Class declaration with brace
    re_class_decl = re.compile(
        r'^\s*(?:export\s+|public\s+|private\s+|protected\s+|abstract\s+|final\s+)*'
        r'(?:class|interface|struct)\s+([A-Za-z0-9_]+)(?:\s*(?:extends|implements|:)\s*([A-Za-z0-9_,\s\.]+))?\s*\{',
        re.MULTILINE
    )

    class_ranges = []
    for m in re_class_decl.finditer(code_text):
        cls_name = m.group(1)
        raw_bases = m.group(2)
        bases = []
        if raw_bases:
            bases = [b.strip().split(".")[-1] for b in re.split(r'[,:\s]+', raw_bases) if b.strip() and b.strip() not in ['{', 'extends', 'implements']]

        brace_open = m.end() - 1
        brace_close = find_closing_brace(brace_open)
        start_line = offset_to_line(m.start())
        end_line = offset_to_line(brace_close)
        class_body = code_text[brace_open + 1:brace_close]

        class_ranges.append((brace_open, brace_close, cls_name))

        # Extract methods inside this class body
        methods = []
        re_method = re.compile(
            r'^\s*(?:async\s+|public\s+|private\s+|protected\s+|static\s+|override\s+|def\s+)*'
            r'(?:(?:function|fun|sub|void|int|string|bool)\s+)?'
            r'([A-Za-z0-9_]+)\s*\([^)]*\)\s*(?::\s*[A-Za-z0-9_<>[\]]+\s*)?\{',
            re.MULTILINE
        )
        for mm in re_method.finditer(class_body):
            m_name = mm.group(1)
            if m_name in ["if", "for", "while", "switch", "catch"]:
                continue
            m_open = brace_open + 1 + mm.end() - 1
            m_close = find_closing_brace(m_open)
            m_start_l = offset_to_line(brace_open + 1 + mm.start())
            m_end_l = offset_to_line(m_close)
            m_code = code_text[brace_open + 1 + mm.start():m_close + 1]
            m_calls = extract_calls_from_text(m_code, m_name)
            methods.append({
                "name": m_name,
                "parent_class": cls_name,
                "start_line": m_start_l,
                "end_line": m_end_l,
                "code": m_code[:4000],
                "docstring": "",
                "calls": m_calls
            })

        entities["classes"].append({
            "name": cls_name,
            "start_line": start_line,
            "end_line": end_line,
            "code": code_text[m.start():brace_close + 1][:4000],
            "docstring": "",
            "bases": bases,
            "base_classes": bases,
            "methods": methods
        })
        entities["functions"].extend(methods)

    # Parse top-level functions (outside of any class range)
    re_standalone_fn = re.compile(
        r'^\s*(?:export\s+|default\s+|async\s+|public\s+|private\s+|static\s+)*'
        r'(?:function|func|fn|fun|def)\s+([A-Za-z0-9_]+)\s*\([^)]*\)\s*(?::\s*[A-Za-z0-9_<>[\]]+\s*)?\{',
        re.MULTILINE
    )
    for fm in re_standalone_fn.finditer(code_text):
        f_start = fm.start()
        inside_cls = any(c_start <= f_start <= c_end for c_start, c_end, _ in class_ranges)
        if inside_cls:
            continue

        fn_name = fm.group(1)
        f_open = fm.end() - 1
        f_close = find_closing_brace(f_open)
        fn_start_l = offset_to_line(f_start)
        fn_end_l = offset_to_line(f_close)
        fn_code = code_text[f_start:f_close + 1]
        fn_calls = extract_calls_from_text(fn_code, fn_name)

        test_target = None
        if fn_name.startswith("test") or fn_name.startswith("Test"):
            t_cand = re.sub(r'^[tT]est_?', '', fn_name)
            for c in fn_calls:
                if c in t_cand:
                    t_cand = c
                    break
            if t_cand:
                test_target = t_cand

        entities["functions"].append({
            "name": fn_name,
            "parent_class": None,
            "start_line": fn_start_l,
            "end_line": fn_end_l,
            "code": fn_code[:4000],
            "docstring": "",
            "calls": fn_calls,
            "test_target": test_target
        })

    return entities

def _parse_with_universal_engine(file_path: str, lang: str, code_text: str) -> dict:
    """
    Universal Polyglot extractor supporting ANY programming language.
    Extracts classes, functions, imports, docstrings, and line bounds.
    """
    lines = code_text.splitlines()
    entities = {
        "file": file_path,
        "language": lang,
        "classes": [],
        "functions": [],
        "imports": [],
        "imported_symbols": {}
    }

    # Extract Imports
    for match in RE_IMPORT.finditer(code_text):
        entities["imports"].append(match.group(0).strip())

    # Extract Classes
    for match in RE_CLASS.finditer(code_text):
        cls_name = match.group(1)
        raw_bases = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        bases = []
        if raw_bases:
            bases = [b.strip().split(".")[-1] for b in re.split(r'[,:\s]+', raw_bases) if b.strip() and b.strip() not in ['{', 'extends', 'implements', 'public', 'private', 'protected']]

        start_idx = match.start()
        start_line = code_text[:start_idx].count("\n") + 1
        end_line = min(start_line + 60, len(lines))
        chunk = "\n".join(lines[start_line - 1:end_line])

        entities["classes"].append({
            "name": cls_name,
            "start_line": start_line,
            "end_line": end_line,
            "code": chunk[:4000],
            "docstring": "",
            "bases": bases,
            "methods": []
        })

    # Extract Functions
    for match in RE_FUNCTION.finditer(code_text):
        fn_name = match.group(1)
        if fn_name in ["if", "while", "for", "switch", "catch", "return"]:
            continue

        start_idx = match.start()
        start_line = code_text[:start_idx].count("\n") + 1
        end_line = min(start_line + 40, len(lines))
        chunk = "\n".join(lines[start_line - 1:end_line])

        calls = list(set(re.findall(r'([A-Za-z0-9_]+)\s*\(', chunk)))
        calls = [c for c in calls if c != fn_name and c not in ["if", "for", "while", "print", "println", "console"]]

        entities["functions"].append({
            "name": fn_name,
            "parent_class": None,
            "start_line": start_line,
            "end_line": end_line,
            "code": chunk[:4000],
            "docstring": "",
            "calls": calls[:15]
        })

    return entities

# --- Public Multi-Language Parser Entrypoint ---

def parse_source_file(file_path: str, code: bytes) -> dict:
    """
    Universal Polyglot & Deep AST Parser:
    1. For Python: Standard library AST parser for 100% precision.
    2. For Tree-sitter supported grammars: Uses Tree-sitter if grammar is installed.
    3. For Polyglot languages (JS/TS, Go, Java, C#, etc.): Scoped block parser with brace matching.
    4. Resilient fallback to universal regex engine.
    """
    lang = detect_language(file_path)
    text, code_utf8 = decode_source_code(code)
    entities = None

    # 1. Native Python AST (Fastest, zero-dependency, 100% accurate)
    if lang == "python":
        entities = _parse_python_ast(file_path, text)

    # 2. Tree-sitter if installed and grammar available
    if not entities:
        grammar_configs = {
            "python": ("tree-sitter-python", "tree_sitter_python"),
            "javascript": ("tree-sitter-javascript", "tree_sitter_javascript"),
            "typescript": ("tree-sitter-typescript", "tree_sitter_typescript", "language_typescript"),
            "go": ("tree-sitter-go", "tree_sitter_go"),
            "java": ("tree-sitter-java", "tree_sitter_java"),
            "c": ("tree-sitter-c", "tree_sitter_c"),
            "cpp": ("tree-sitter-cpp", "tree_sitter_cpp"),
            "c_sharp": ("tree-sitter-c-sharp", "tree_sitter_c_sharp"),
            "rust": ("tree-sitter-rust", "tree_sitter_rust")
        }
        if lang in grammar_configs:
            cfg = grammar_configs[lang]
            func_name = cfg[2] if len(cfg) > 2 else "language"
            p = _init_parser(lang, cfg[1], func_name)
            if p:
                try:
                    entities = _parse_with_treesitter(p, lang, file_path, code_utf8)
                except Exception as e:
                    print(f"Tree-sitter parse notice on {file_path}: {e}")

    # 3. Scoped Block Parser (Brace-aware for JS/TS/Go/Java/C#/C++)
    if not entities:
        try:
            entities = _parse_scoped_code(file_path, lang, text)
        except Exception as e:
            print(f"Scoped parser notice on {file_path}: {e}")

    # 4. Universal Polyglot Fallback
    if not entities:
        entities = _parse_with_universal_engine(file_path, lang, text)

    # Cross-Service Traceability: Extract API Routes and Clients via Polyglot Regex
    entities.setdefault("api_routes", [])
    entities.setdefault("api_clients", [])
    
    # Matches: @app.get('/path'), router.post("/path"), app.put(...)
    route_pattern = re.compile(r'(?:@app\.|@router\.|app\.|router\.)(get|post|put|delete|patch)\s*\(\s*[\'"]([^\'"]+)[\'"]')
    for match in route_pattern.finditer(text):
        method, path = match.groups()
        entities["api_routes"].append({"method": method.upper(), "path": path})

    # Matches: fetch('/path'), axios.get('/path'), requests.post('/path')
    client_pattern = re.compile(r'(?:fetch|axios\.(?:get|post|put|delete|patch)|requests\.(?:get|post|put|delete|patch))\s*\(\s*[\'"]([^\'"]+)[\'"]')
    for match in client_pattern.finditer(text):
        path = match.group(1)
        entities["api_clients"].append({"path": path})

    # Test Coverage Detection
    entities["is_test"] = "test" in file_path.lower() or "spec" in file_path.lower()
    
    return entities

# Backward-compatible alias
parse_python_file = parse_source_file

def get_supported_extensions() -> set:
    """Returns all supported code file extensions."""
    return set(EXTENSION_MAP.keys())
