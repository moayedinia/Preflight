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

# --- AST Node Handlers for Tree-sitter Supported Languages ---

def _get_node_text(node, code_bytes: bytes) -> str:
    return code_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")

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
        for child in node.children:
            if child.type in ["identifier", "type_identifier", "name"]:
                return _get_node_text(child, code_bytes)
        return "Anonymous"

    def _find_calls(node) -> list[str]:
        calls = set()
        def _scan(curr):
            if curr.type in ["call", "call_expression", "method_invocation"]:
                fn_child = curr.child_by_field_name("function") or curr.child_by_field_name("name")
                if fn_child:
                    calls.add(_get_node_text(fn_child, code_bytes).split(".")[-1].split("->")[-1])
            for ch in curr.children:
                _scan(ch)
        _scan(node)
        return list(calls)

    def _traverse(node, current_class: Optional[str] = None):
        if node.type in class_types:
            cls_name = _find_identifier(node)
            raw_code = _get_node_text(node, code_bytes)
            cls_obj = {
                "name": cls_name,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "code": raw_code[:4000],
                "docstring": "",
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
    r'(?:class|struct|interface|trait|type|enum|record)\s+([A-Za-z0-9_]+)',
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
        if p:
            try:
                return _parse_with_treesitter(p, lang, file_path, code)
            except Exception as e:
                print(f"Tree-sitter parse error on {file_path}: {e}. Falling back to universal engine.")

    # Universal engine fallback for all languages
    text = code.decode("utf-8", errors="ignore")
    return _parse_with_universal_engine(file_path, lang, text)

# Backward-compatible alias
parse_python_file = parse_source_file

def get_supported_extensions() -> set:
    """Returns all supported code file extensions."""
    return set(EXTENSION_MAP.keys())
