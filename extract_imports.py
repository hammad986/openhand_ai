import os
import ast
import sys

def get_imports(directory):
    imports = set()
    for root, dirs, files in os.walk(directory):
        if '.venv' in root or 'my_env' in root or '__pycache__' in root or '.git' in root:
            continue
        for file in files:
            if file.endswith('.py') and file != os.path.basename(__file__):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        tree = ast.parse(f.read(), filename=filepath)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for n in node.names:
                                imports.add(n.name.split('.')[0])
                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                imports.add(node.module.split('.')[0])
                except Exception as e:
                    print(f"Error parsing {filepath}: {e}")
    return sorted(list(imports))

std_libs = sys.stdlib_module_names if hasattr(sys, 'stdlib_module_names') else set()

found = get_imports('.')
external = [m for m in found if m not in std_libs and not m.startswith('_')]
print("External imports found:", external)
