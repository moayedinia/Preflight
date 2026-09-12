import tree_sitter_python as tspython
from tree_sitter import Language, Parser

# Initialize Tree-sitter for Python
PY_LANGUAGE = Language(tspython.language(), "python")
parser = Parser()
parser.set_language(PY_LANGUAGE)

def extract_docstring(node, code_bytes: bytes) -> str:
    """Extracts docstring from a class or function body if present."""
    for child in node.children:
        if child.type == "block":
            for block_child in child.children:
                if block_child.type == "expression_statement":
                    for expr_child in block_child.children:
                        if expr_child.type == "string":
                            return code_bytes[expr_child.start_byte:expr_child.end_byte].decode("utf-8", errors="ignore").strip("\"'")
                elif block_child.type not in ["comment"]:
                    break
    return ""

def extract_calls(node, code_bytes: bytes) -> list[str]:
    """Finds all function/method call identifiers inside an AST node."""
    calls = set()
    def _find_calls(curr):
        if curr.type == "call":
            func_node = curr.child_by_field_name("function")
            if func_node:
                if func_node.type == "identifier":
                    call_name = code_bytes[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="ignore")
                    calls.add(call_name)
                elif func_node.type == "attribute":
                    # e.g. self.do_something() or service.process()
                    attr_name = func_node.child_by_field_name("attribute")
                    if attr_name:
                        call_name = code_bytes[attr_name.start_byte:attr_name.end_byte].decode("utf-8", errors="ignore")
                        calls.add(call_name)
        for child in curr.children:
            _find_calls(child)

    _find_calls(node)
    return list(calls)

def parse_python_file(file_path: str, code: bytes) -> dict:
    """
    Parses a python source file into structured entities & relationships:
    - Classes & their methods
    - Top-level functions
    - Imports
    - Outgoing function/method calls
    - Raw code chunks for vector embeddings
    """
    tree = parser.parse(code)
    root_node = tree.root_node
    
    entities = {
        "file": file_path,
        "classes": [],
        "functions": [],
        "imports": []
    }

    def process_function(node, parent_class: str = None) -> dict:
        name = ""
        for child in node.children:
            if child.type == "identifier":
                name = code[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
                break
        
        raw_code = code[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
        docstring = extract_docstring(node, code)
        calls = extract_calls(node, code)
        
        return {
            "name": name,
            "parent_class": parent_class,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "code": raw_code,
            "docstring": docstring,
            "calls": calls
        }

    def process_class(node) -> dict:
        class_name = ""
        for child in node.children:
            if child.type == "identifier":
                class_name = code[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
                break

        raw_code = code[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
        docstring = extract_docstring(node, code)
        methods = []

        # Find methods inside class block
        for child in node.children:
            if child.type == "block":
                for item in child.children:
                    if item.type == "function_definition":
                        methods.append(process_function(item, parent_class=class_name))

        return {
            "name": class_name,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "code": raw_code,
            "docstring": docstring,
            "methods": methods
        }

    # Top level traversal
    for child in root_node.children:
        if child.type == "class_definition":
            cls_data = process_class(child)
            entities["classes"].append(cls_data)
        elif child.type == "function_definition":
            func_data = process_function(child)
            entities["functions"].append(func_data)
        elif child.type in ["import_statement", "import_from_statement"]:
            import_text = code[child.start_byte:child.end_byte].decode("utf-8", errors="ignore").strip()
            entities["imports"].append(import_text)

    return entities
