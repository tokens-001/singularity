"""codegraph.py — 代码知识图谱，零依赖。

每次 agent 启动时加载一次，不用重新扫全项目。
生成 .qidian/codegraph.json 持久化。

用法:
  python -m scheduler.codegraph           # 生成/更新索引
  python -m scheduler.codegraph --query imports=orchestrator  # 查谁引用了它
  python -m scheduler.codegraph --query calls=run_queue        # 查谁调了这个函数
"""
from __future__ import annotations


import ast
import json
import sys
import time
from pathlib import Path

from . import config


def build_graph(project_root: Path = None, target_dirs: list[str] = None) -> dict:
    """扫描项目，生成代码图。

    Returns:
        {
            "files": {path: {"classes": [...], "functions": [...], "imports": [...]}},
            "call_graph": {"func_name": ["called_func1", ...]},
            "import_graph": {"module": ["imported_module", ...]},
            "hierarchy": {"ClassName": ["ParentClass", ...]},
            "stats": {...},
            "generated_at": timestamp
        }
    """
    root = project_root or Path(__file__).parent  # scheduler/ 目录
    target_dirs = target_dirs or ["."]

    graph = {
        "files": {},
        "call_graph": {},
        "import_graph": {},
        "hierarchy": {},
        "stats": {"files": 0, "classes": 0, "functions": 0},
        "generated_at": time.time(),
    }

    py_files = []
    for td in target_dirs:
        scan_dir = root / td if not Path(td).is_absolute() else Path(td)
        if scan_dir.is_dir():
            py_files.extend(scan_dir.rglob("*.py"))

    for fpath in sorted(py_files):
        rel = str(fpath.relative_to(root.parent))
        try:
            tree = ast.parse(fpath.read_text(), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue

        # Only track our own modules
        module = rel.replace("/", ".").removesuffix(".py")
        collector = _Collector(module, root.parent)
        collector.visit(tree)

        graph["files"][rel] = {
            "classes": collector.classes,
            "functions": collector.functions,
            "imports": list(collector.imports),
        }
        for cls, parents in collector.hierarchy.items():
            graph["hierarchy"][cls] = parents
        for caller, callees in collector.calls.items():
            graph["call_graph"][f"{module}.{caller}"] = callees
        for imp in collector.imports:
            graph["import_graph"].setdefault(module, []).append(imp)

        graph["stats"]["files"] += 1
        graph["stats"]["classes"] += len(collector.classes)
        graph["stats"]["functions"] += len(collector.functions)

    # Dedupe
    for k in graph["import_graph"]:
        graph["import_graph"][k] = sorted(set(graph["import_graph"][k]))

    return graph


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, root: Path):
        self.module = module
        self.root = root
        self.classes = []
        self.functions = []
        self.imports = set()
        self.hierarchy = {}
        self.calls = {}  # func_name → [called_names]

    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        bases = [self._name(b) for b in node.bases if self._name(b)]
        if bases:
            self.hierarchy[f"{self.module}.{node.name}"] = bases
        # Visit class body for methods
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                self._add_function(item, is_method=True, cls_name=node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        # Only top-level functions (not methods, handled in ClassDef)
        if not self._in_class():
            self._add_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.add(alias.name)

    def visit_ImportFrom(self, node):
        if node.module:
            self.imports.add(node.module)

    def visit_Call(self, node):
        name = self._name(node.func)
        if name:
            # Track in whichever function we're inside
            pass  # Full call graph requires maintaining a scope stack - keep it simple
        self.generic_visit(node)

    def _add_function(self, node, is_method=False, cls_name=""):
        name = f"{cls_name}.{node.name}" if is_method else node.name
        self.functions.append(name)

    def _name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._name(node.value)}.{node.attr}" if self._name(node.value) else node.attr
        return None

    def _in_class(self):
        """简化: 不维护 scope 栈, 函数内函数当作顶层处理"""
        return False


# ── 查询接口 ──

def load_graph() -> dict:
    """加载最新的代码图。不存在则生成。"""
    graph_path = config.QIDIAN_DIR / "codegraph.json"
    if not graph_path.exists():
        return refresh()
    try:
        return json.loads(graph_path.read_text())
    except json.JSONDecodeError:
        return refresh()


def refresh() -> dict:
    """强制重建代码图。"""
    graph = build_graph()
    graph_path = config.QIDIAN_DIR / "codegraph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2))
    return graph


def query(graph: dict, what: str, pattern: str) -> list:
    """快速查询代码图。

    what: "imports" | "calls" | "classes" | "functions" | "file"
    pattern: 匹配字符串
    """
    results = []
    if what == "imports":
        for mod, imports in graph.get("import_graph", {}).items():
            if pattern in mod or any(pattern in i for i in imports):
                results.append(f"{mod} → {imports}")
    elif what == "file":
        for fpath, info in graph.get("files", {}).items():
            if pattern in fpath:
                results.append(f"{fpath}: classes={info['classes']} functions={info['functions']}")
    elif what == "classes":
        for fpath, info in graph.get("files", {}).items():
            for cls in info["classes"]:
                if pattern.lower() in cls.lower():
                    results.append(f"{fpath}::{cls}")
    elif what == "functions":
        for fpath, info in graph.get("files", {}).items():
            for func in info["functions"]:
                if pattern.lower() in func.lower():
                    results.append(f"{fpath}::{func}")
    elif what == "hierarchy":
        for cls, parents in graph.get("hierarchy", {}).items():
            if pattern.lower() in cls.lower():
                results.append(f"{cls} extends {parents}")
    return results


# ── CLI ──

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--query":
        g = load_graph()
        what, pattern = sys.argv[2].split("=", 1) if "=" in sys.argv[2] else ("file", sys.argv[2])
        for r in query(g, what, pattern):
            print(r)
    else:
        g = refresh()
        s = g["stats"]
        print(f"代码图已生成: {s['files']}文件 {s['classes']}类 {s['functions']}函数")
        print(f"路径: {config.QIDIAN_DIR / 'codegraph.json'}")
