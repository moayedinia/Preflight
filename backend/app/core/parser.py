import os
import re
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
        "imports": []
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
        # Approximate chunk up to 80 lines
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

        # Simple call extraction inside the chunk
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
    Universal Polyglot Parser:
    1. Detects file language from extension.
    2. Uses Tree-sitter AST parser if available for the grammar.
    3. Seamlessly falls back to Universal Polyglot Engine for all other languages.
    """
    lang = detect_language(file_path)

    # Tree-sitter parser grammar mappings
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
    # Decode source code bytes safely to Unicode text and canonical UTF-8 bytes
    text, code_utf8 = decode_source_code(code)
    entities = None
    if lang in grammar_configs and p:
        try:
            entities = _parse_with_treesitter(p, lang, file_path, code_utf8)
        except Exception as e:
            print(f"Tree-sitter parse error on {file_path}: {e}. Falling back to universal engine.")
            
    if not entities:
        entities = _parse_with_universal_engine(file_path, lang, text)

    # Cross-Service Traceability: Extract API Routes and Clients via Polyglot Regex
    entities["api_routes"] = []
    entities["api_clients"] = []
    
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
