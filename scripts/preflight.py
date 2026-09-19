#!/usr/bin/env python3
"""preflight —— 两种病灶形状的静态预检 + 真机现场自查（只读）。

⚠️ 「只读」有一个例外：`shapes --write-baseline <path>` 会写那个基线文件，
   它是人看完命中之后**主动**更新棘轮的动作，不在 CI 里跑。

用法:
    python3 scripts/preflight.py shapes [--rev <git-rev>] [--root DIR] [--json] [--debug SUBSTR]
    python3 scripts/preflight.py shapes --baseline scripts/preflight-baseline.json
    python3 scripts/preflight.py live   [--root DIR] [--json] [--since-hours H]

    shapes  静态扫源码（不跑代码）。--rev 时用 `git archive <rev>` 把那棵树取到
            临时目录再扫 —— 不 checkout、不碰工作区。不给 --rev 就扫当前工作区
            （默认仓库根）。
            --baseline 是**给门用的形态**：只有**新出现**的形状才红（棘轮），
            不是「有命中就红」—— 后者是恒红，恒红的门会被 `|| true` 掉。
    live    真机跑完一轮之后的现场自查。只读：扫 .qidian/ 下的 *.corrupt 隔离备份、
            alerts.jsonl 里的静默降级类告警，并给出人工恢复的下一步。绝不写任何文件。

退出码: 0 = 干净（无命中 / 相对基线无新增）；1 = 有命中（或有新增）；2 = 环境/用法错误。
（命中不代表一定是 bug —— 每条都要人判，这是工具的边界，不是缺陷的借口。）

形状判据（详细论证见 ~/Desktop/ZCode审阅/预检工具-01.md §1）:
  A   guard-before-read   守卫读状态元 S，守卫之后那次调用的摘要对 S 是
                          "仅失败路径写"（写只发生在 except / 判坏早退分支），
                          且 F 之后还有写盘动作。懒加载（无条件置位）不在此列。
  B1  deny-list 家族分歧  变量名像 deny-list 的字符串集合字面量，归一化后共享
                          ≥2 条目的算一族，族内互报条目差集。
  B2a dead-word           字面量只出现在匹配位（==/in/startswith…）而无任何
                          生产者，且存在同族（共享 ≥4 字符 token）的现役词作见证。
  B2b narrow-prefix       startswith/endswith/in 匹配位上的字面量 L 有生产者，
                          但同族词 P（首 token 相同）按该匹配语义接不住。
  B3  duplicated-wrapper  同文件两个函数都调 subprocess.*，归一化 token 袋
                          相似度 ≥ 阈值 ⇒ 同一件事的两份实现；列出 sink 实参差。
  B4  store-asymmetry     同文件里调用同一资源读取器的一组函数，兄弟触达了
                          单例存储（get_* 并在其上调方法），F 只动配置面且 F 有写。

零第三方依赖。证据一律写符号名，不写行号（行号会漂）。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# 通用
# ═══════════════════════════════════════════════════════════════

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".qidian",
             "singularity.egg-info", ".idea", ".vscode", "htmlcov", ".mypy_cache",
             ".pytest_cache", "build", "dist", ".codegraph", ".agents", ".claude"}

WRITE_RE = re.compile(
    r"(write|save|store|persist|dump|replace|commit|update|flush)", re.IGNORECASE)
MUTATOR_RE = re.compile(r"^(add|update|append|extend|insert|remove|discard|pop|clear|setdefault)$")
DENYLIST_NAME_RE = re.compile(
    r"(?i)(blocked|blacklist|blocklist|deny|denied|forbidden|sensitive|secret|banned|disallow)")
READER_RE = re.compile(r"^(load|get|read)_\w+$")
SINGLETON_GET_RE = re.compile(r"^get_\w+$")
WRITER_RE = re.compile(r"^(save|write|store|update|drop)_\w+$")

_SINK_CALLS = {"subprocess.run", "subprocess.Popen", "subprocess.check_output",
               "subprocess.check_call", "subprocess.call", "os.system", "os.popen"}

MATCH_KINDS = ("startswith", "endswith", "in", "eq", "ne")


def is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    if "tests" in parts or "testing" in parts:
        return True
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def find_repo_root(start: Path) -> Path | None:
    p = start.resolve()
    for cand in (p, *p.parents):
        if (cand / ".git").exists():
            return cand
    return None


def collect_py_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git"))
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                out.append(Path(dirpath) / fn)
    return out


@dataclass
class Finding:
    shape: str
    file: str
    symbol: str
    message: str
    evidence: list[str] = field(default_factory=list)

    def as_dict(self):
        return {"shape": self.shape, "file": self.file, "symbol": self.symbol,
                "message": self.message, "evidence": self.evidence}


# ═══════════════════════════════════════════════════════════════
# 树的取法：--rev 用 git archive 进临时目录（不 checkout）
# ═══════════════════════════════════════════════════════════════

def materialize_rev(rev: str, repo: Path, keep_dir: str | None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="preflight-tree-")) if not keep_dir else Path(keep_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["git", "-C", str(repo), "archive", rev], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            try:
                tf.extractall(tmp, filter="data")   # py3.12+
            except TypeError:
                tf.extractall(tmp)
    except Exception:
        proc.kill()
        raise
    if proc.wait() != 0:
        raise RuntimeError(f"git archive {rev} 失败（rev 存在吗？）")
    return tmp


# ═══════════════════════════════════════════════════════════════
# 模块索引 + 函数摘要（形状 A 的数据流底座）
# ═══════════════════════════════════════════════════════════════

@dataclass
class CallSite:
    node: ast.Call
    exc: bool          # 是否处于失败路径（except 块 / 判坏早退分支）内


@dataclass
class FuncInfo:
    module: str
    qualname: str
    name: str
    cls: str | None
    node: ast.AST
    local_imports: dict = field(default_factory=dict)   # local name -> ("module", dotted) | ("modulealias", dotted)
    calls: list = field(default_factory=list)           # [CallSite]
    events: list = field(default_factory=list)          # (kind, payload, exc)  kind: ref/assign/mutate/subscript/attr_assign
    direct_reads: set = field(default_factory=set)      # 本函数体直接读到的 cell
    global_decl: set = field(default_factory=set)
    local_assigned: set = field(default_factory=set)


class Module:
    def __init__(self, relpath: str, modname: str, tree: ast.Module, is_test: bool):
        self.relpath = relpath
        self.modname = modname
        self.tree = tree
        self.is_test = is_test
        self.globals: set[str] = set()
        self.funcs: dict[str, FuncInfo] = {}       # name 或 "Cls.method" -> FuncInfo
        self.imports: dict[str, tuple] = {}        # local name -> ("module", dotted) | ("modulealias", dotted)


def modname_for(root: Path, pyfile: Path) -> str:
    rel = pyfile.relative_to(root)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    i = 0
    while i < len(parts) - 1 and not (root / Path(*parts[:i + 1]) / "__init__.py").exists():
        i += 1
    return ".".join(parts[i:])


class Index:
    """全仓 AST 索引：模块、函数、import 映射、状态元。"""

    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, Module] = {}
        self.by_file: dict[str, Module] = {}
        self._build()

    # ---------- 构建 ----------

    def _build(self):
        pyfiles = collect_py_files(self.root)
        for pf in pyfiles:
            rel = str(pf.relative_to(self.root))
            try:
                tree = ast.parse(pf.read_text(encoding="utf-8"), filename=rel)
            except SyntaxError as e:
                print(f"[preflight] 语法解析失败，跳过 {rel}: {e}", file=sys.stderr)
                continue
            m = Module(rel, modname_for(self.root, pf), tree, is_test_path(rel))
            self.modules[m.modname] = m
            self.by_file[rel] = m
        for m in self.modules.values():
            self._collect_module(m)

    def _collect_module(self, m: Module):
        for node in m.tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._bind_import(m, node, m.imports)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            m.globals.add(n.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                m.globals.add(node.target.id)
        # 函数（含方法、嵌套函数都登记，解析时按名找）
        for node in ast.walk(m.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls = self._enclosing_class(m, node)
                qual = f"{cls}.{node.name}" if cls else node.name
                fi = FuncInfo(module=m.modname, qualname=qual, name=node.name,
                              cls=cls, node=node)
                self._scan_function(m, fi)
                m.funcs[qual] = fi

    @staticmethod
    def _enclosing_class(m: Module, fn) -> str | None:
        for parent in ast.walk(m.tree):
            if isinstance(parent, ast.ClassDef):
                for child in parent.body:
                    if child is fn or (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                                       and any(g is fn for g in ast.walk(child))):
                        return parent.name
        return None

    def _bind_import(self, m: Module, node, table: dict):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                table[a.asname or top] = ("modulealias", a.name if a.asname else top)
        else:
            mod = node.module or ""
            if node.level:   # 相对导入
                pkg = m.modname.split(".")[:-1]
                for _ in range(node.level - 1):
                    pkg = pkg[:-1]
                mod = ".".join(pkg + ([mod] if mod else []))
            for a in node.names:
                if a.name == "*":
                    continue
                table[a.asname or a.name] = ("module", f"{mod}.{a.name}" if mod else a.name)

    # ---------- 函数体扫描 ----------

    def _scan_function(self, m: Module, fi: FuncInfo):
        node = fi.node
        # 事件收集只走这一遍：每个调用/引用恰好收录一次，带正确的失败路径标注。
        self._collect_events(m, fi, node.body, exc=False)
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                self._bind_import(m, sub, fi.local_imports)
            elif isinstance(sub, ast.Global):
                fi.global_decl.update(sub.names)
        # 局部赋值名（用于区分"本地变量"和"模块级状态元"）
        for sub in ast.walk(node):
            if isinstance(sub, ast.arg):
                fi.local_assigned.add(sub.arg)
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            fi.local_assigned.add(n.id)
            elif isinstance(sub, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                t = getattr(sub, "target", None) or getattr(sub, "value", None)
                if t is not None:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            fi.local_assigned.add(n.id)
            elif isinstance(sub, (ast.For, ast.AsyncFor, ast.comprehension)):
                for n in ast.walk(sub.target):
                    if isinstance(n, ast.Name):
                        fi.local_assigned.add(n.id)
            elif isinstance(sub, ast.withitem) and sub.optional_vars is not None:
                for n in ast.walk(sub.optional_vars):
                    if isinstance(n, ast.Name):
                        fi.local_assigned.add(n.id)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                fi.local_assigned.add(sub.name)

    def _collect_events(self, m: Module, fi: FuncInfo, stmts, exc: bool):
        """按源码顺序走语句块；except 块与"判坏早退分支"内的东西标 exc=True。"""
        for st in stmts:
            if isinstance(st, ast.ExceptHandler):
                if st.type is not None:
                    self._emit_subtree(m, fi, st.type, exc)
                self._collect_events(m, fi, st.body, exc=True)
                continue
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for d in getattr(st, "decorator_list", []):
                    self._emit_subtree(m, fi, d, exc)
                continue  # 嵌套定义体不并入外层（保守：不算外层的读写）
            if isinstance(st, ast.If):
                self._emit_subtree(m, fi, st.test, exc)
                branch_has_exit = _has_exit(st.body)
                self._collect_events(m, fi, st.body, exc=exc or branch_has_exit)
                orelse_exc = exc or (_has_exit(st.orelse) and not branch_has_exit)
                self._collect_events(m, fi, st.orelse, exc=orelse_exc)
                continue
            if isinstance(st, (ast.For, ast.AsyncFor)):
                self._emit_subtree(m, fi, st.iter, exc)
                self._collect_events(m, fi, st.body, exc=exc)
                self._collect_events(m, fi, st.orelse, exc=exc)
                continue
            if isinstance(st, ast.While):
                self._emit_subtree(m, fi, st.test, exc)
                self._collect_events(m, fi, st.body, exc=exc)
                self._collect_events(m, fi, st.orelse, exc=exc)
                continue
            if isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    self._emit_subtree(m, fi, item.context_expr, exc)
                    if item.optional_vars is not None:
                        self._emit_target_events(m, fi, item.optional_vars, exc)
                self._collect_events(m, fi, st.body, exc=exc)
                continue
            if isinstance(st, ast.Try):
                self._collect_events(m, fi, st.body, exc=exc)
                for h in st.handlers:
                    if h.type is not None:
                        self._emit_subtree(m, fi, h.type, exc)
                    self._collect_events(m, fi, h.body, exc=True)
                self._collect_events(m, fi, st.orelse, exc=exc)
                self._collect_events(m, fi, st.finalbody, exc=exc)
                continue
            if isinstance(st, ast.Assign):
                self._emit_subtree(m, fi, st.value, exc)
                for t in st.targets:
                    self._emit_target_events(m, fi, t, exc)
                continue
            if isinstance(st, ast.AnnAssign):
                if st.value is not None:
                    self._emit_subtree(m, fi, st.value, exc)
                self._emit_target_events(m, fi, st.target, exc)
                continue
            if isinstance(st, ast.AugAssign):
                self._emit_subtree(m, fi, st.value, exc)
                self._emit_target_events(m, fi, st.target, exc)
                continue
            if isinstance(st, ast.Delete):
                for t in st.targets:
                    self._emit_target_events(m, fi, t, exc)
                continue
            if isinstance(st, ast.Return):
                if st.value is not None:
                    self._emit_subtree(m, fi, st.value, exc)
                continue
            if isinstance(st, (ast.Expr, ast.Assert, ast.Raise)):
                for f in ("value", "test", "msg", "exc", "cause"):
                    v = getattr(st, f, None)
                    if v is not None:
                        self._emit_subtree(m, fi, v, exc)
                continue
            body = getattr(st, "body", None)
            if isinstance(body, list):
                # 没专门处理的复合语句：把非语句表达式字段抖出来，语句列表递归
                for fname in st._fields:
                    v = getattr(st, fname, None)
                    if isinstance(v, ast.AST) and not isinstance(v, ast.stmt):
                        self._emit_subtree(m, fi, v, exc)
                    elif isinstance(v, list) and v and isinstance(v[0], ast.expr):
                        for e in v:
                            self._emit_subtree(m, fi, e, exc)
                    elif isinstance(v, list) and v and isinstance(v[0], ast.stmt):
                        self._collect_events(m, fi, v, exc=exc)
                continue

    def _emit_subtree(self, m: Module, fi: FuncInfo, node, exc: bool):
        """把一个表达式里的调用/引用各收录一次。"""
        if node is None:
            return
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                fi.calls.append(CallSite(n, exc=exc))
            elif isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Store):
                    continue
                fi.events.append(("ref", n.id, exc))
            elif isinstance(n, ast.Attribute):
                v = n.value
                if isinstance(v, ast.Name) and v.id == "self":
                    if isinstance(n.ctx, ast.Load):
                        fi.events.append(("attr_ref", n.attr, exc))
                    else:
                        fi.events.append(("attr_assign", n.attr, exc))
                elif isinstance(v, ast.Name) and isinstance(n.ctx, ast.Load):
                    # _io._QUARANTINED 这类跨模块直接引用：记 (模块别名, 属性名)
                    fi.events.append(("xattr_ref", (v.id, n.attr), exc))

    def _emit_target_events(self, m: Module, fi: FuncInfo, t, exc: bool):
        if isinstance(t, ast.Name):
            fi.events.append(("assign", t.id, exc))
        elif isinstance(t, ast.Attribute):
            if isinstance(t.value, ast.Name) and t.value.id == "self":
                fi.events.append(("attr_assign", t.attr, exc))
        elif isinstance(t, ast.Subscript):
            base = t.value
            if isinstance(base, ast.Name):
                fi.events.append(("subscript", base.id, exc))
            elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) \
                    and base.value.id == "self":
                fi.events.append(("attr_subscript", base.attr, exc))
            else:
                self._emit_subtree(m, fi, base, exc)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._emit_target_events(m, fi, e, exc)
        elif isinstance(t, ast.Starred):
            self._emit_target_events(m, fi, t.value, exc)

    # ---------- 解析 ----------

    def resolve_module(self, dotted: str) -> Module | None:
        if dotted in self.modules:
            return self.modules[dotted]
        parts = dotted.split(".")
        for i in range(len(parts)):
            cand = ".".join(parts[i:])
            if cand in self.modules:
                return self.modules[cand]
        return None

    def resolve_import(self, table: dict, name: str):
        return table.get(name) or None

    def resolve_call(self, m: Module, fi: FuncInfo, call: ast.Call):
        """返回 ("func", module, qualname) | ("unknown",)。只解到函数级，够形状 A 用。"""
        f = call.func
        if isinstance(f, ast.Name):
            nm = f.id
            if nm in m.funcs:
                return ("func", m.modname, nm)
            imp = fi.local_imports.get(nm) or m.imports.get(nm)
            if imp and imp[0] == "module":
                # from mod import fn  →  imp[1] = "mod.fn"，fn 不是模块；
                # 先按整体试模块，失败就剥掉末段按"父模块里的函数"解。
                tm = self.resolve_module(imp[1])
                base = imp[1].split(".")[-1]
                if tm is None and "." in imp[1]:
                    parent = imp[1].rsplit(".", 1)[0]
                    tm = self.resolve_module(parent)
                    if tm and base in tm.funcs:
                        return ("func", tm.modname, base)
                if tm:
                    if base in tm.funcs:
                        return ("func", tm.modname, base)
                    # from pkg import mod 形式：mod.func
                    return ("modulealias", tm.modname, None)
            return ("unknown",)
        if isinstance(f, ast.Attribute):
            base = f.value
            if isinstance(base, ast.Name):
                nm = base.id
                imp = fi.local_imports.get(nm) or m.imports.get(nm)
                target_mod = None
                if imp:
                    if imp[0] == "modulealias":
                        target_mod = self.resolve_module(imp[1])
                    elif imp[0] == "module":
                        tm = self.resolve_module(imp[1])
                        if tm:
                            last = imp[1].split(".")[-1]
                            if last in tm.funcs:
                                return ("unknown",)  # 借名函数的属性，不追
                            target_mod = tm
                elif nm in m.globals:
                    return ("unknown",)
                if target_mod is not None:
                    attr = f.attr
                    if f"{attr}" in target_mod.funcs:
                        return ("func", target_mod.modname, attr)
                    return ("unknown",)
            if isinstance(base, ast.Name) and base.id == "self" and fi.cls:
                qual = f"{fi.cls}.{f.attr}"
                if qual in m.funcs:
                    return ("func", m.modname, qual)
            return ("unknown",)
        return ("unknown",)


def _ordered_walk(node):
    """近似源码顺序的遍历（generic_visit 按字段顺序，足够排前后）。"""
    return list(ast.walk(node))


def _has_exit(stmts) -> bool:
    """这个分支是不是"失败收口"（bail）：里面有 raise、裸 return、return 常量/简单值。

    ⚠️ `return f(...)` 这种**委派型 return 不算 bail** —— 它只是把结果交上去
    （`if http: return self._connect_http()`），把它标成失败路径会把被委派函数的
    无条件写全污染成"仅失败路径写"（MCPClient.connect 就是这么误报出来的）。
    """
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(n, ast.Raise):
                return True
            if isinstance(n, ast.Return):
                v = n.value
                if v is None or not isinstance(v, (ast.Call, ast.Await)):
                    return True
            # 嵌套在 if/try 里的 return 也算（分支整体以退出收口才算"判坏早退"，
            # 但保守起见：任何 bail 都先算，误标方向是"多报 exc"，安全侧）
    return False


# ═══════════════════════════════════════════════════════════════
# 状态元与读写摘要
# ═══════════════════════════════════════════════════════════════

@dataclass
class Summary:
    reads: set = field(default_factory=set)          # 直接读（本函数体）
    writes_uncond: set = field(default_factory=set)  # 存在全无条件路径的写
    writes_cond: set = field(default_factory=set)    # 仅失败路径可达的写
    direct_reads: set = field(default_factory=set)


class Summarizer:
    def __init__(self, index: Index):
        self.idx = index
        self.cells: dict[tuple, str] = {}      # cell -> 描述
        self._build_cells()
        self.memo: dict[tuple, Summary] = {}

    def _build_cells(self):
        for m in self.idx.modules.values():
            if m.is_test:
                continue
            for g in m.globals:
                self.cells[("g", m.modname, g)] = f"{m.modname}.{g}"
            # 类属性 cell：类的方法里 self.X 出现过即视为一个元
            classes = defaultdict(set)
            for qual, fi in m.funcs.items():
                if fi.cls:
                    for ev in fi.events:
                        if ev[0] in ("attr_assign", "attr_subscript", "attr_ref"):
                            classes[fi.cls].add(ev[1])
            for cls, attrs in classes.items():
                for a in attrs:
                    self.cells[("a", m.modname, cls, a)] = f"{m.modname}.{cls}.self.{a}"

    def cell_of_name(self, m: Module, fi: FuncInfo, name: str):
        if name in fi.global_decl or (name in m.globals and name not in fi.local_assigned):
            return ("g", m.modname, name)
        return None

    def cell_of_self_attr(self, m: Module, fi: FuncInfo, attr: str):
        if fi.cls:
            cell = ("a", m.modname, fi.cls, attr)
            if cell in self.cells:
                return cell
        return None

    def direct_ops(self, m: Module, fi: FuncInfo):
        """本函数体直接读写到的 cell（读不带路径语义，写按事件 exc 分档）。"""
        reads, wu, wc = set(), set(), set()
        for ev in fi.events:
            kind = ev[0]
            cell = None
            if kind in ("ref", "assign"):
                cell = self.cell_of_name(m, fi, ev[1])
            elif kind in ("attr_ref", "attr_assign", "attr_subscript"):
                cell = self.cell_of_self_attr(m, fi, ev[1])
            elif kind == "subscript":
                cell = self.cell_of_name(m, fi, ev[1])
            elif kind == "xattr_ref":
                alias, attr = ev[1]
                imp = fi.local_imports.get(alias) or m.imports.get(alias)
                if imp:
                    tm = self.idx.resolve_module(imp[1])
                    if tm and attr in tm.globals:
                        cell = ("g", tm.modname, attr)
            if cell is None:
                continue
            if kind in ("ref", "attr_ref", "xattr_ref"):
                reads.add(cell)
                fi.direct_reads.add(cell)
            else:
                (wc if ev[2] else wu).add(cell)
        # 变更方法调用：recv.add(...) / recv.update(...)
        for cs in fi.calls:
            f = cs.node.func
            if isinstance(f, ast.Attribute) and MUTATOR_RE.match(f.attr or ""):
                recv = f.value
                cell = None
                if isinstance(recv, ast.Name):
                    cell = self.cell_of_name(m, fi, recv.id)
                elif isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name) \
                        and recv.value.id == "self":
                    cell = self.cell_of_self_attr(m, fi, recv.attr)
                if cell:
                    (wc if cs.exc else wu).add(cell)
        # 下标/属性读之外，变更也算"写"侧证据 → 从 reads 里去掉纯写目标
        return reads, wu, wc

    def summary(self, modname: str, qual: str, _stack: frozenset = frozenset()) -> Summary:
        key = (modname, qual)
        if key in self.memo:
            return self.memo[key]
        if key in _stack:
            return Summary()
        m = self.idx.modules.get(modname)
        if m is None or qual not in m.funcs:
            return Summary()
        fi = m.funcs[qual]
        reads, wu, wc = self.direct_ops(m, fi)
        stack = _stack | {key}
        for cs in fi.calls:
            res = self.idx.resolve_call(m, fi, cs.node)
            if not res or res[0] != "func":
                continue
            sub = self.summary(res[1], res[2], stack)
            tgt, wu2, wc2 = res, sub.writes_uncond, sub.writes_cond
            for c in wu2:
                (wc if cs.exc else wu).add(c)
            wc |= wc2
        s = Summary(reads=reads, writes_uncond=wu, writes_cond=wc, direct_reads=fi.direct_reads)
        self.memo[key] = s
        return s


# ═══════════════════════════════════════════════════════════════
# 形状 A：护栏查在读之前
# ═══════════════════════════════════════════════════════════════

def _calls_desc(cs: CallSite, idx: Index) -> str:
    return ast.unparse(cs.node)[:110] if hasattr(ast, "unparse") else "<call>"


def detect_guard_before_read(idx: Index, summ: Summarizer) -> list[Finding]:
    out = []
    seen = set()
    for m in idx.modules.values():
        if m.is_test:
            continue
        for qual, fi in sorted(m.funcs.items()):
            for node in ast.walk(fi.node):
                if not isinstance(node, ast.If):
                    continue
                guard_cells = _test_read_cells(idx, summ, m, fi, node.test)
                if not guard_cells:
                    continue
                if not _has_exit(node.body):
                    continue
                for cell in guard_cells:
                    # 守卫之后：找"仅失败路径写 cell"的调用
                    loaders = []
                    for cs in fi.calls:
                        if (cs.node.lineno, cs.node.col_offset) <= (node.lineno, node.col_offset):
                            continue
                        res = idx.resolve_call(m, fi, cs.node)
                        if not res or res[0] != "func":
                            continue
                        sub = summ.summary(res[1], res[2])
                        if cell in sub.writes_cond and cell not in sub.writes_uncond:
                            loaders.append((cs, res))
                    if not loaders:
                        continue
                    cs, res = loaders[0]
                    # 读之后要有写盘动作（"读改写"的写那一半）
                    if not _has_write_after(fi, cs):
                        continue
                    gname = summ.cells.get(cell, str(cell))
                    key = (m.relpath, qual, cell, res[1], res[2])
                    if key in seen:
                        continue
                    seen.add(key)
                    ev = [
                        f"守卫: {ast.unparse(node.test)[:100]} （判定函数读 {gname}）",
                        f"守卫之后的读: {_calls_desc(cs, idx)} —— 其内部仅在失败路径写 {gname}",
                        f"读后写盘: {_find_write_after(fi, cs)}（函数 {qual}）",
                    ]
                    out.append(Finding(
                        shape="A-guard-before-read",
                        file=m.relpath, symbol=qual,
                        message=f"守卫查 {gname} 在读之前，而真值要等后面这次读（失败路径）才置上 —— "
                                f"第一次触碰时守卫恒假，整份重建照写。",
                        evidence=ev))
    return out


def _test_read_cells(idx: Index, summ: Summarizer, m: Module, fi: FuncInfo, test) -> set:
    cells = set()
    for n in ast.walk(test):
        if isinstance(n, ast.Call):
            res = idx.resolve_call(m, fi, n)
            if res and res[0] == "func":
                cells |= summ.summary(res[1], res[2]).direct_reads
        elif isinstance(n, ast.Name):
            c = summ.cell_of_name(m, fi, n.id)
            if c:
                cells.add(c)
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id == "self":
            c = summ.cell_of_self_attr(m, fi, n.attr)
            if c:
                cells.add(c)
    return cells


WRITE_CALL_RE = WRITE_RE


def _func_label(res) -> str:
    return f"{res[1]}.{res[2]}"


def _has_write_after(fi: FuncInfo, cs: CallSite) -> bool:
    return _find_write_after(fi, cs) is not None


def _find_write_after(fi: FuncInfo, cs: CallSite):
    for c2 in fi.calls:
        if (c2.node.lineno, c2.node.col_offset) > (cs.node.lineno, cs.node.col_offset):
            f = c2.node.func
            nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if nm and WRITE_CALL_RE.search(nm):
                return nm
    return None


# ═══════════════════════════════════════════════════════════════
# 形状 B 的共用底座：字符串字面量的"匹配位 / 取值位"普查
# ═══════════════════════════════════════════════════════════════

@dataclass
class StrSite:
    file: str
    symbol: str
    kind: str            # match: startswith/endswith/in/eq/ne；value: value；doc: doc
    channel: str = ""    # 匹配位：被匹配的变量/字段名；取值位：赋值目标名


def survey_strings(idx: Index) -> dict:
    """prod 代码里每个字符串字面量的出现位置分类。

    匹配位（kind: eq/ne/in/startswith/endswith）只标"针"那一侧的字面量，
    通道记的是"草垛"那一侧的变量/字段名；取值位（value）剪掉嵌套在
    Compare / startswith 里的部分（那是匹配位的领地，不算生产）。
    """
    match_sites: dict[str, list[StrSite]] = defaultdict(list)
    value_sites: dict[str, list[StrSite]] = defaultdict(list)
    collections: list[tuple[str, str, list[str], bool]] = []   # (file, var, items, deny_named)

    def sym(stack):
        return ".".join(reversed(stack)) if stack else "<module>"

    def mark(kind: str, needle_nodes, channel: str, file: str, stack):
        for nd in needle_nodes:
            for c in _str_consts_in(nd):
                match_sites[c].append(StrSite(file, sym(stack), kind, channel))

    def _is_needle_call(n) -> bool:
        return isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
            and n.func.attr in ("startswith", "endswith")

    def value_consts(node):
        """取值位字面量：不下钻 Compare 和 startswith/endswith 调用。"""
        out = []

        def go(n):
            if isinstance(n, ast.Compare) or _is_needle_call(n):
                return
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
                return
            if isinstance(n, ast.JoinedStr):
                for v in n.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        out.append(v.value)
                return
            for ch in ast.iter_child_nodes(n):
                go(ch)

        go(node)
        return out

    for m in idx.modules.values():
        if m.is_test:
            continue
        file = m.relpath

        class V(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def _body(self, n):
                if n.body and isinstance(n.body[0], ast.Expr) \
                        and isinstance(n.body[0].value, ast.Constant) \
                        and isinstance(n.body[0].value.value, str):
                    for st in n.body[1:]:
                        self.visit(st)
                else:
                    for st in n.body:
                        self.visit(st)

            def visit_FunctionDef(self, n):
                self.stack.append(n.name)
                self._body(n)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n):
                self.stack.append(n.name)
                self._body(n)
                self.stack.pop()

            def visit_Compare(self, n):
                sides = [n.left] + list(n.comparators)
                for i, op in enumerate(n.ops):
                    left, right = sides[i], sides[i + 1]
                    if isinstance(op, (ast.Eq, ast.NotEq)):
                        kind = "eq" if isinstance(op, ast.Eq) else "ne"
                        lc, rc = _str_consts_in(left), _str_consts_in(right)
                        if lc and not rc:
                            mark(kind, [left], _channel_of(right), file, self.stack)
                        elif rc and not lc:
                            mark(kind, [right], _channel_of(left), file, self.stack)
                        elif lc and rc:
                            mark(kind, [left, right], "", file, self.stack)
                    elif isinstance(op, (ast.In, ast.NotIn)):
                        # "针 in 草垛"：针在左；草垛是元组/列表时元素是针
                        if _str_consts_in(left) and not isinstance(right, (ast.List, ast.Tuple, ast.Set)):
                            mark("in", [left], _channel_of(right), file, self.stack)
                        if isinstance(right, (ast.List, ast.Tuple, ast.Set)) \
                                and all(isinstance(e, ast.Constant) for e in right.elts):
                            mark("in", [right], _channel_of(left), file, self.stack)
                for s in sides:
                    self.visit(s)

            def visit_Call(self, n):
                f = n.func
                if isinstance(f, ast.Attribute) and f.attr in ("startswith", "endswith"):
                    if n.args:
                        mark(f.attr, [n.args[0]], _channel_of(f.value), file, self.stack)
                self.generic_visit(n)

            def _nested(self, node):
                """值表达式里嵌套的 Compare / startswith 调用也要走一遍匹配位标注。"""
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Compare) or _is_needle_call(sub):
                        self.visit(sub)

            def visit_Assign(self, n):
                if len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                        and isinstance(n.value, (ast.List, ast.Tuple, ast.Set)):
                    items = [e.value for e in n.value.elts
                             if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    if len(items) >= 3 and len(items) == len(n.value.elts):
                        collections.append((file, n.targets[0].id, items,
                                            bool(DENYLIST_NAME_RE.search(n.targets[0].id))))
                tgts = [t.id for t in n.targets if isinstance(t, ast.Name)]
                chan = tgts[0] if tgts else ""
                for c in value_consts(n.value):
                    value_sites[c].append(StrSite(file, sym(self.stack), "value", chan))
                # ⚠️ **下标赋值也是产出**（2026-09-14 补）：`resp.headers["Content-Encoding"] = "gzip"`
                # 这种写法原来**不算"产出了这个字面量"** ⇒ B2a 把它报成"死词：消费侧在等一个
                # 不会再来的词"。实测误报过 `Content-Encoding`（`gzip_response` 里明明就在写）。
                # 通道取被下标那个表达式的名字（`response.headers` → `headers`），与匹配位同口径。
                for t in n.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.slice, ast.Constant)
                            and isinstance(t.slice.value, str)):
                        value_sites[t.slice.value].append(
                            StrSite(file, sym(self.stack), "value", _channel_of(t.value)))
                self._nested(n.value)
                for t in n.targets:
                    self.visit(t)

            def visit_AnnAssign(self, n):
                if n.value is not None:
                    chan = n.target.id if isinstance(n.target, ast.Name) else ""
                    for c in value_consts(n.value):
                        value_sites[c].append(StrSite(file, sym(self.stack), "value", chan))
                    self._nested(n.value)
                self.visit(n.target)

            def visit_keyword(self, n):
                for c in value_consts(n.value):
                    value_sites[c].append(StrSite(file, sym(self.stack), "value", n.arg or ""))
                self._nested(n.value)
                self.visit(n.value)

            def visit_Return(self, n):
                if n.value is not None:
                    for c in value_consts(n.value):
                        value_sites[c].append(StrSite(file, sym(self.stack), "value", ""))
                    self._nested(n.value)
                    self.visit(n.value)

            def visit_Dict(self, n):
                for k in n.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        value_sites[k.value].append(StrSite(file, sym(self.stack),
                                                            "value", "dictkey"))
                self.generic_visit(n)

        V().visit(m.tree)

    return {"match": match_sites, "value": value_sites, "collections": collections}


def _str_consts_in(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.append(v.value)
    return out


def _channel_of(node) -> str:
    """被匹配对象的"通道名"：term_reason / batch.term_reason → term_reason。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _channel_of(node.func)
    return ""


def _tokens(s: str) -> set:
    return {t for t in re.split(r"[^0-9a-zA-Z]+", s.lower()) if t}


def _first_token(s: str) -> str:
    t = s.strip().lower().split("_")[0]
    return t


# ═══════════════════════════════════════════════════════════════
# B1：deny-list 家族分歧
# ═══════════════════════════════════════════════════════════════

def _norm_item(s: str) -> str:
    n = s.strip().lower().replace("**/", "")
    if n.startswith("*."):
        n = n[2:]
    if n.startswith("."):
        n = n[1:]
    if n.endswith("/*"):
        n = n[:-2]
    return n


def _fileish(s: str) -> bool:
    """条目长得像文件名/路径模式（有点、星或斜杠）—— 用来排除环境变量子串表
    之类的非文件名单混进同一族。"""
    return any(ch in s for ch in ".*")

def detect_b1(survey) -> list[Finding]:
    """deny-list 家族分歧：**按文件合并**该文件里所有像 deny-list 的字符串集合，
    只做跨文件配对 —— 同一张黑名单在 web 侧常拆成 FILES/PREFIXES/SUFFIXES 三张，
    按文件合并才能对着"对面整张表"报差集。"""
    by_file: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for (f, v, items, named) in survey["collections"]:
        if named:
            by_file[f][v] = items
    files = sorted(by_file)
    out = []
    seen = set()
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            f1, f2 = files[i], files[j]
            items1 = [x for vs in by_file[f1].values() for x in vs]
            items2 = [x for vs in by_file[f2].values() for x in vs]
            # 共享条目里，至少 2 条在"两边原文"都像文件模式 —— 同类名单的硬门槛
            n2map = defaultdict(list)
            for x in items2:
                n2map[_norm_item(x)].append(x)
            shared_pairs = set()
            for x in items1:
                for y in n2map.get(_norm_item(x), ()):
                    if _fileish(x) and _fileish(y):
                        shared_pairs.add((x, y))
            if len(shared_pairs) < 2:
                continue
            n1 = {_norm_item(x) for x in items1}
            n2 = set(n2map)
            if n1 == n2:
                continue
            key = (f1, f2)
            if key in seen:
                continue
            seen.add(key)
            only1 = [x for x in items1 if _norm_item(x) not in n2]
            only2 = [x for x in items2 if _norm_item(x) not in n1]
            vars1 = ", ".join(sorted(by_file[f1]))
            vars2 = ", ".join(sorted(by_file[f2]))
            out.append(Finding(
                shape="B1-denylist-family-divergence",
                file=f1, symbol=vars1,
                message=f"与 {f2}（{vars2}）是同一族名单（两侧都是文件模式的共享条目: "
                        f"{sorted(shared_pairs)[:4]}），但条目已经不一致。",
                evidence=[
                    f"{f1} 独有: {only1[:10]}",
                    f"{f2} 独有: {only2[:10]}",
                ]))
    return out


# ═══════════════════════════════════════════════════════════════
# B2a：死词；B2b：窄前缀判据
# ═══════════════════════════════════════════════════════════════

def _token_overlap(L: str, P: str) -> set:
    return {t for t in _tokens(L) & _tokens(P) if len(t) >= 4}


def detect_b2a(survey) -> list[Finding]:
    """死词：匹配位上的字面量，按它自己的匹配语义（==/in/startswith/endswith）
    对**任何**生产出来的值都永远不命中；且要有一条**同通道**的同族现役词作见证。

    通道 = 匹配位"草垛"侧的变量/字段名 == 生产位的赋值目标/关键字名。
    没有通道见证的死词不报（CLI 旗标、HTTP 方法、外部产出物全在这里挡掉）。
    """
    match, value = survey["match"], survey["value"]
    out = []
    produced = [p for p in value if len(p) <= 200]

    def could_match(kind: str, L: str) -> bool:
        if kind == "ne":
            return True
        for P in produced:
            if kind == "eq" and P == L:
                return True
            if kind == "in" and L in P:
                return True
            if kind == "startswith" and P.startswith(L):
                return True
            if kind == "endswith" and P.endswith(L):
                return True
        return False

    prod_by_channel: dict[str, dict[str, StrSite]] = defaultdict(dict)
    for p, sites in value.items():
        for s in sites:
            if s.channel and len(p) <= 80 and "\n" not in p:
                prod_by_channel[s.channel].setdefault(p, s)

    for L, sites in sorted(match.items()):
        if L in value or len(L) < 5:
            continue
        dead = [s for s in sites if not could_match(s.kind, L)]
        if not dead:
            continue
        by_chan = defaultdict(list)
        for s in dead:
            if s.channel:
                by_chan[s.channel].append(s)
        best = None   # (channel, witness, psite)
        for chan, ss in by_chan.items():
            for P, psite in prod_by_channel.get(chan, {}).items():
                if P == L or not _token_overlap(L, P):
                    continue
                if best is None or len(P) < len(best[1]):
                    best = (chan, P, psite)
        if best is None:
            continue
        chan, w, wsite = best
        hit_sites = by_chan[chan]
        out.append(Finding(
            shape="B2a-dead-word",
            file=hit_sites[0].file, symbol=hit_sites[0].symbol,
            message=f"字面量 {L!r} 按匹配语义（{hit_sites[0].kind}）对全仓任何产出值都永不命中 —— "
                    f"消费侧在等一个不会再来的词（同通道的现役词是 {w!r}）。",
            evidence=[f"匹配位: {s.file}::{s.symbol}（通道 {chan}）" for s in hit_sites[:6]] +
                     [f"同通道现役词的生产位: {wsite.file}::{wsite.symbol}"
                      f"（通道 {wsite.channel}）"]))
    return out


def detect_b2b(survey) -> list[Finding]:
    """窄判据：匹配位字面量 L 有生产者，但同族词 P（首 token 相同、且与 L 不同通道
    不算——要求同通道）也有生产者，且按该匹配位的语义 P 接不住。"""
    match, value = survey["match"], survey["value"]
    out = []
    seen = set()
    value_by_channel: dict[str, set] = defaultdict(set)
    for p, sites in value.items():
        for s in sites:
            if s.channel:
                value_by_channel[s.channel].add(p)
    for L, sites in sorted(match.items()):
        if L not in value:
            continue
        ft = _first_token(L)
        if len(ft) < 4:
            continue
        for s in sites:
            if s.kind not in ("startswith", "endswith", "in") or not s.channel:
                continue
            chan_P = value_by_channel.get(s.channel, set())
            for P in sorted(chan_P):
                if P == L or P.lower() == L.lower() or _first_token(P) != ft:
                    continue
                if s.kind == "startswith" and P.startswith(L):
                    continue
                if s.kind == "endswith" and P.endswith(L):
                    continue
                if s.kind == "in" and L in P:
                    continue
                key = (s.file, s.symbol, L, P)
                if key in seen:
                    continue
                seen.add(key)
                psite = next((s2 for s2 in value[P] if s2.channel == s.channel), value[P][0])
                out.append(Finding(
                    shape="B2b-narrow-prefix-match",
                    file=s.file, symbol=s.symbol,
                    message=f"判据只认 {L!r}，但同族词 {P!r} 也在同一通道（{s.channel}）上产出"
                            f"—— 按这个匹配语义 P 会被漏掉。",
                    evidence=[f"匹配位: {s.file}::{s.symbol}（通道 {s.channel}）",
                              f"同族词的生产位: {psite.file}::{psite.symbol}"
                              f"（通道 {psite.channel}）"]))
    return out


# ═══════════════════════════════════════════════════════════════
# B3：同一件事写了两遍（subprocess 包装族）
# ═══════════════════════════════════════════════════════════════

def _body_tokens(fn) -> Counter_:
    from collections import Counter as C
    bag = C()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                base = ast.unparse(f.value) if hasattr(ast, "unparse") else ""
                if base.startswith("self"):
                    base = ""
                bag["call:" + ((base + ".") if base else "") + f.attr] += 1
            elif isinstance(f, ast.Name):
                bag["call:" + f.id] += 1
            for kw in n.keywords:
                bag["kw:" + (kw.arg or "**")] += 1
        elif isinstance(n, ast.Constant):
            v = n.value
            if isinstance(v, (str, int, float, bool)):
                bag[f"c:{v!r}"] += 1
    return bag


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def detect_b3(idx: Index, threshold: float) -> list[Finding]:
    out = []
    seen = set()
    for m in idx.modules.values():
        if m.is_test:
            continue
        fns = [(q, fi) for q, fi in m.funcs.items()]
        for i in range(len(fns)):
            for j in range(i + 1, len(fns)):
                q1, f1 = fns[i]
                q2, f2 = fns[j]
                if _sinks(f1.node) is None and _sinks(f2.node) is None:
                    continue
                t1, t2 = _body_tokens(f1.node), _body_tokens(f2.node)
                sim = _jaccard(set(t1), set(t2))
                if sim < threshold:
                    continue
                key = tuple(sorted((q1, q2)))
                if key in seen:
                    continue
                seen.add(key)
                a, b = key
                s1, s2 = _sinks(f1.node), _sinks(f2.node)
                sa, sb = (s1, s2) if q1 == a else (s2, s1)
                diffs = _sink_kwarg_diffs(sa, sb, a, b)
                out.append(Finding(
                    shape="B3-duplicated-wrapper",
                    file=m.relpath, symbol=f"{a} / {b}",
                    message=f"同一件事的两份实现（归一化相似度 {sim:.2f}）——"
                            f"subprocess 实参差：{diffs or '（kwarg 无差，见证据里的常量/调用差）'}",
                    evidence=[f"similarity={sim:.2f}",
                              f"{a} sinks: {_sink_brief(sa)}",
                              f"{b} sinks: {_sink_brief(sb)}"]))
    return out


def _sinks(fn):
    """函数体里的 subprocess/os 命令 sink 调用。"""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            dotted = ""
            if isinstance(f, ast.Attribute):
                base = ast.unparse(f.value) if hasattr(ast, "unparse") else ""
                dotted = f"{base}.{f.attr}" if base else f.attr
            elif isinstance(f, ast.Name):
                dotted = f.id
            dotted_tail = dotted.split(".")[-1]
            if dotted in _SINK_CALLS or (dotted == "os." + dotted_tail and dotted_tail in ("system", "popen")):
                out.append(n)
    return out or None


def _sink_brief(sinks) -> str:
    if not sinks:
        return "（无）"
    parts = []
    for s in sinks:
        kws = sorted(k.arg for k in s.keywords if k.arg)
        parts.append(f"{ast.unparse(s.func)}({', '.join(kws)})")
    return "; ".join(parts)


def _sink_kwarg_diffs(s1, s2, n1, n2) -> str:
    if not s1 or not s2:
        return ""
    msgs = []
    k1 = {k.arg for k in s1[0].keywords if k.arg}
    k2 = {k.arg for k in s2[0].keywords if k.arg}
    if k1 - k2:
        msgs.append(f"{n1} 传了 {sorted(k1 - k2)}、{n2} 没传")
    if k2 - k1:
        msgs.append(f"{n2} 传了 {sorted(k2 - k1)}、{n1} 没传")
    c1 = {ast.unparse(k.value) for k in s1[0].keywords if k.arg}
    c2 = {ast.unparse(k.value) for k in s2[0].keywords if k.arg}
    if c1 - c2:
        msgs.append(f"{n1} 实参值多出 {sorted(c1 - c2)[:4]}")
    if c2 - c1:
        msgs.append(f"{n2} 实参值多出 {sorted(c2 - c1)[:4]}")
    return "；".join(msgs)


# ═══════════════════════════════════════════════════════════════
# B4：同一资源的成组管理函数，存储面不一致
# ═══════════════════════════════════════════════════════════════

def _touches_singleton(fn) -> bool:
    """函数里有没有 get_* 单例访问，并在拿到的对象上做调用/属性操作。"""
    get_vars = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            if isinstance(n.value, ast.Call):
                f = n.value.func
                nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                if SINGLETON_GET_RE.match(nm or ""):
                    for t in n.targets:
                        for x in ast.walk(t):
                            if isinstance(x, ast.Name):
                                get_vars.add(x.id)
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            nm = ""
            if isinstance(f, ast.Attribute):
                nm = f.attr
                base = f.value
                base_nm = base.id if isinstance(base, ast.Name) else ""
                if SINGLETON_GET_RE.match(base_nm) or base_nm in get_vars:
                    return True
                # m.get_registry().load_configs(...) 链式
                if isinstance(base, ast.Call):
                    bf = base.func
                    bnm = bf.attr if isinstance(bf, ast.Attribute) else \
                        (bf.id if isinstance(bf, ast.Name) else "")
                    if SINGLETON_GET_RE.match(bnm or ""):
                        return True
            elif isinstance(f, ast.Name) and SINGLETON_GET_RE.match(f.id or ""):
                return True
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                and n.value.id in get_vars:
            return True
    return False


def _accessor_name(idx: Index, m: Module, fi: FuncInfo, cs: CallSite) -> str | None:
    """取调用名，但只认"模块级访问器"：裸名调用，或模块别名上的属性调用。
    `request.get_json(...)` 这种 from-import 进来的**对象**上的方法不算
    （否则 Flask 的取请求体助手会把整个 web 层并成一个假资源组）。"""
    f = cs.node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        base = f.value.id
        imp = fi.local_imports.get(base) or m.imports.get(base)
        if imp:
            if imp[0] == "modulealias":
                return f.attr
            if imp[0] == "module" and idx.resolve_module(imp[1]) is not None:
                return f.attr
    return None


def detect_b4(idx: Index) -> list[Finding]:
    out = []
    for m in idx.modules.values():
        if m.is_test:
            continue
        groups = defaultdict(list)
        meta = {}
        for q, fi in m.funcs.items():
            readers, writers, singleton = set(), set(), _touches_singleton(fi.node)
            for cs in fi.calls:
                nm = _accessor_name(idx, m, fi, cs)
                if not nm:
                    continue
                if READER_RE.match(nm):
                    readers.add(nm)
                if WRITER_RE.match(nm):
                    writers.add(nm)
            meta[q] = (readers, writers, singleton)
            for r in readers:
                groups[r].append(q)
        for r, qs in groups.items():
            if len(qs) < 2:
                continue
            for q in qs:
                readers, writers, singleton = meta[q]
                if singleton or not writers:
                    continue
                sibs = [g for g in qs if g != q and meta[g][2]]
                if not sibs:
                    continue
                out.append(Finding(
                    shape="B4-store-asymmetry",
                    file=m.relpath, symbol=q,
                    message=f"同资源组（都调 {r}）：兄弟 {', '.join(sorted(sibs)[:3])} "
                            f"会触达单例存储（get_* 并在其上操作），本函数只动了配置面 "
                            f"（写: {sorted(writers)[:3]}）——同一处状态改了一半。",
                    evidence=[f"本函数 readers={sorted(readers)[:4]} writers={sorted(writers)[:4]} "
                              f"singleton_touch=False",
                              f"触达单例的兄弟: {sorted(sibs)[:3]}"]))
    return out


from collections import Counter as Counter_  # noqa: E402  (B3 用)


# ═══════════════════════════════════════════════════════════════
# live：真机现场自查（只读）
# ═══════════════════════════════════════════════════════════════

LIVE_ALERT_TOKENS = ["record_skip", "save_skipped", "rollback_skipped", "corrupt", "degraded"]
# 指定五键之外、"同一类"（被跳过/被隔离/被降级）的同族键 —— 只看 key 字段，避免正文误咬。
LIVE_ALERT_FAMILY_RE = re.compile(r"(?:^|_)(?:skip|skipped|corrupt|degraded|quarantin)(?:e?d?)(?:$|_)", re.IGNORECASE)
CORRUPT_SUFFIX_RE = re.compile(r"\.corrupt(\.\d+)?$")


def _find_alerts_for(alert_lines, filename: str, limit=5):
    hits = []
    for rec in alert_lines:
        msg = rec.get("msg", "")
        key = rec.get("key", "")
        if filename in msg or filename in key or f"json_corrupt:{filename}" in key:
            hits.append(rec)
            if len(hits) >= limit:
                break
    return hits


def cmd_live(args) -> int:
    root = Path(args.root).resolve() if args.root else _default_root()
    qidian = root / ".qidian"
    report = {"root": str(root), "qidian": str(qidian), "corrupt": [], "alerts": [],
              "notes": []}
    if not qidian.is_dir():
        report["notes"].append(f"没有找到 {qidian} —— 现场没有 .qidian 状态目录"
                               f"（没跑过真机，或仓库根判断错了）。只读扫描，什么都没改。")
        _print_live(report, args.json)
        return 0

    # ---- 1. *.corrupt 隔离备份 ----
    corrupt_files = sorted(qidian.rglob("*.corrupt")) + \
                    sorted(p for p in qidian.rglob("*.corrupt.*") if CORRUPT_SUFFIX_RE.search(p.name))
    alert_lines = _read_alerts(qidian / "alerts.jsonl", report)
    import datetime
    for c in corrupt_files:
        orig_name = CORRUPT_SUFFIX_RE.sub("", c.name)
        orig = c.with_name(orig_name)
        entry = {
            "backup": str(c),
            "original": str(orig),
            "original_exists": orig.exists(),
            "backup_size": c.stat().st_size if c.exists() else None,
            "backup_mtime": _iso(c.stat().st_mtime),
        }
        if orig.exists():
            entry["original_size"] = orig.stat().st_size
            entry["original_mtime"] = _iso(orig.stat().st_mtime)
            try:
                json.loads(orig.read_bytes())
                entry["original_parses"] = True
            except Exception as e:
                entry["original_parses"] = False
                entry["original_parse_error"] = f"{type(e).__name__}: {e}"[:120]
        # 同名历史备份（.bak-*)
        baks = sorted(orig.parent.glob(orig.name + ".bak-*"))
        if baks:
            entry["sibling_baks"] = [str(b.name) for b in baks][-3:]
        alerts_for = _find_alerts_for(alert_lines, c.name) + \
                     _find_alerts_for(alert_lines, orig.name)
        dedup = []
        for a in alerts_for:
            if a not in dedup:
                dedup.append(a)
        entry["related_alerts"] = [
            {"ts": _iso(a.get("ts", 0)), "scope": a.get("scope", ""), "key": a.get("key", ""),
             "msg": a.get("msg", "")} for a in dedup[:3]]
        report["corrupt"].append(entry)

    # ---- 2. 静默降级类告警 ----
    cutoff = None
    if args.since_hours:
        import time as _t
        cutoff = _t.time() - args.since_hours * 3600
    for rec in alert_lines:
        msg, key = rec.get("msg", ""), rec.get("key", "")
        toks = [t for t in LIVE_ALERT_TOKENS if t in key or t in msg]
        fam = bool(key) and bool(LIVE_ALERT_FAMILY_RE.search(key)) and not toks
        if not toks and not fam:
            continue
        ts = rec.get("ts", 0)
        if cutoff and ts < cutoff:
            continue
        report["alerts"].append({"ts": _iso(ts), "scope": rec.get("scope", ""),
                                 "key": key, "tokens": toks,
                                 "family_only": fam, "msg": msg})
    report["alerts"].sort(key=lambda r: r["ts"], reverse=True)
    report["alerts"] = report["alerts"][: int(args.limit)]
    report["alerts_total_lines"] = len(alert_lines)
    report["scanned_corrupt"] = len(corrupt_files)
    _print_live(report, args.json)
    return 1 if (report["corrupt"] or report["alerts"]) else 0


def _default_root() -> Path:
    here = Path.cwd()
    r = find_repo_root(here)
    return r or here


def _iso(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"


def _read_alerts(path: Path, report) -> list[dict]:
    out = []
    if not path.exists():
        report["notes"].append(f"没有告警文件 {path} —— witness 还没写过任何告警。")
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    report["notes"].append(f"alerts.jsonl 第 {i} 行不是合法 JSON，原样跳过"
                                           f"（内容截断: {line[:80]!r}）")
    except OSError as e:
        report["notes"].append(f"告警文件读不了（只读尝试）: {type(e).__name__}: {e}")
    return out


def _print_live(report, as_json: bool):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"现场: {report['qidian']}")
    for n in report.get("notes", []):
        print(f"  ⚠️ {n}")
    cs = report.get("corrupt", [])
    print(f"\n== *.corrupt 隔离备份: {len(cs)} 个 ==")
    for c in cs:
        print(f"  备份: {c['backup']}  ({c['backup_size']} B, {c['backup_mtime']})")
        print(f"  原文件: {c['original']}  存在={c['original_exists']}")
        if c.get("original_exists"):
            print(f"    现大小 {c.get('original_size')} B, mtime {c.get('original_mtime')}, "
                  f"可解析={c.get('original_parses', '?')}")
            if c.get("original_parses") is False:
                print(f"    解析错误: {c.get('original_parse_error')}")
        if c.get("sibling_baks"):
            print(f"  同名历史备份: {c['sibling_baks']}")
        for a in c.get("related_alerts", []):
            print(f"  当时告警[{a['ts']} {a['scope']} {a['key']}]: {a['msg']}")
        print(f"  ⮕ 下一步（按 _io 的隔离契约）:")
        if c.get("original_exists") and c.get("original_parses"):
            print(f"     1) 原文件现在能解析 —— 多半已被后续写入重建；")
            print(f"     2) 拿 {c['backup']}（原样损坏字节）对比现在的内容，把丢掉的键人工摘回来；")
            print(f"     3) 处理完把 .corrupt 挪走/删掉，重启进程（_QUARANTINED 是内存集合，重启才清）。")
        elif c.get("original_exists"):
            print(f"     1) 原文件仍然解析失败 —— 备份 {c['backup']} 里是最后一次完好的损坏现场；")
            print(f"     2) 判断损坏原因（截断/写竞态），能救的数据从备份里摘；")
            print(f"     3) 修好后重启进程；没把握就整文件用备份恢复。")
        else:
            print(f"     1) 原文件已不在 —— {c['backup']} 是唯一副本；")
            print(f"     2) 检查备份可解析性，能解析就直接恢复原名（先看内容有没有被写坏半截）；")
            print(f"     3) 恢复后重启进程。")
        print()
    al = report.get("alerts", [])
    print(f"== 静默降级类告警: {len(al)} 条（共扫 {report.get('alerts_total_lines', 0)} 行 alerts.jsonl）==")
    for a in al:
        tag = f"tokens={a['tokens']}" if a.get("tokens") else "同族键(按形状扩)"
        print(f"  [{a['ts']}] {a['scope']} key={a['key'] or '-'} {tag}")
        print(f"    原文: {a['msg']}")
    if not al:
        print("  （无）")


# ═══════════════════════════════════════════════════════════════
# shapes 主流程
# ═══════════════════════════════════════════════════════════════

def _finding_key(f) -> str:
    return f"{f.shape} | {f.file} | {f.symbol}"


def _apply_baseline(findings, path: Path) -> int:
    """对着基线比：**只有新出现的命中才红**。

    为什么要基线：`shapes` 设计上「命中不代表是 bug，每条都要人判」⇒ 直接当门就是**恒红**
    （当前工作区 4 条），而恒红的门马上会被 `|| true` 掉（§78 假门比没门坏）。
    基线把它变成**棘轮**：已知那几条不红，**再多一条就红**。

    · 新键 → 红（打印出来，附基线里没有的原因）
    · 已知键但 message 变了 → 红（内容漂了就要人看一眼再更新，别让它悄悄过期）
    · 基线里有、现在没了 → 不红，提示可以收窄（修好了）
    """
    old = json.loads(path.read_text(encoding="utf-8"))
    old_map = {e["key"]: e["message"] for e in old["findings"]}
    cur_map = {_finding_key(f): f.message for f in findings}
    judged = sum(1 for e in old["findings"] if (e.get("verdict") or "").strip())

    new = sorted(set(cur_map) - set(old_map))
    changed = sorted(k for k in (set(cur_map) & set(old_map)) if cur_map[k] != old_map[k])
    gone = sorted(set(old_map) - set(cur_map))

    for k in new:
        print(f"🔴 新增命中: {k}\n    {cur_map[k]}")
    for k in changed:
        print(f"🔴 已知命中但内容变了: {k}\n    基线: {old_map[k]}\n    现在: {cur_map[k]}")
    if gone:
        print(f"🔵 基线里有 {len(gone)} 条现在没了（修好了就收窄基线）:")
        for k in gone:
            print(f"    {k}")

    if new or changed:
        print(f"\n合计 {len(new)} 新增 / {len(changed)} 内容变了 / 基线 {len(old_map)} 条。"
              f"\n确认无误后更新基线: {Path(__file__).name} shapes --write-baseline {path}")
        return 1
    # ⚠️ 把"判过几条"一并报出来：基线存在的意义是「这些已经有人判过了」，
    # 而不是「这些被静音了」。判过 0 条 = 这扇门在替一堆没人看过的命中背书。
    print(f"✅ 相对基线无新增（基线 {len(old_map)} 条，命中 {len(findings)} 条，"
          f"其中 {judged} 条有人判过）")
    if judged < len(old_map):
        print(f"   🔵 还有 {len(old_map) - judged} 条没人判 —— 每条都该有人判，"
              f"在 {path} 的对应 `verdict` 里写结论")
    return 0


def _write_baseline(findings, path: Path) -> int:
    """写基线。**已有的 `verdict` 会按 key 带过来** —— 那是人判过的结论，不能丢。

    工具自己的边界是「命中不代表是 bug，每条都要人判」；判完的结论如果只活在
    某个人的脑子里，下一个人（或下一轮的我）会把同一件事**重新查一遍** ——
    `docs/防御模式.md` §86.5 那条「格式对、内容旧比缺失更坏」的同族。
    """
    old = {}
    try:
        old = {e["key"]: e.get("verdict", "") for e in json.loads(
            path.read_text(encoding="utf-8")).get("findings", [])}
    except Exception:
        old = {}                       # 第一次写 / 文件坏了 —— 没得带，不是错
    payload = {
        "note": "已知命中基线 —— 只有新增/内容变了的才让门变红。人看过后手动更新。",
        "verdict": "每条都该有人判过：'verdict' 写结论 + 依据（空 = 还没人判，等于欠一笔账）。",
        "findings": [{"key": _finding_key(f), "message": f.message,
                      "verdict": old.get(_finding_key(f), "")} for f in findings]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入基线 {len(findings)} 条 -> {path}"
          f"（带过来 {sum(1 for e in payload['findings'] if e['verdict'])} 条已判结论）")
    return 0


def cmd_shapes(args) -> int:
    repo = find_repo_root(Path.cwd())
    if args.rev:
        if not repo:
            print("错误：--rev 需要在 git 仓库里运行（要 git archive）", file=sys.stderr)
            return 2
        root = materialize_rev(args.rev, repo, args.keep)
        rev_label = args.rev
    else:
        root = Path(args.root).resolve() if args.root else (repo or Path.cwd())
        rev_label = "WORKTREE"
    try:
        idx = Index(root)
        summ = Summarizer(idx)
        findings = []
        findings += detect_guard_before_read(idx, summ)
        survey = survey_strings(idx)
        findings += detect_b1(survey)
        findings += detect_b2a(survey)
        findings += detect_b2b(survey)
        findings += detect_b3(idx, args.b3_threshold)
        findings += detect_b4(idx)
        meta = {"rev": rev_label, "root": str(root), "modules": len(idx.modules),
                "findings": len(findings)}
        if args.debug:
            _debug_dump(idx, summ, args.debug)
        if args.write_baseline:
            return _write_baseline(findings, Path(args.write_baseline))
        if args.json:
            print(json.dumps({"meta": meta,
                              "findings": [f.as_dict() for f in findings]},
                             ensure_ascii=False, indent=2))
        else:
            print(f"== preflight shapes @ {rev_label} "
                  f"({meta['modules']} modules, {len(findings)} findings) ==")
            by_shape = defaultdict(list)
            for f in findings:
                by_shape[f.shape].append(f)
            for shape in sorted(by_shape):
                print(f"\n-- {shape} ({len(by_shape[shape])}) --")
                for f in by_shape[shape]:
                    print(f"  {f.file} :: {f.symbol}")
                    print(f"    {f.message}")
                    for e in f.evidence:
                        print(f"      · {e}")
        if args.baseline:
            return _apply_baseline(findings, Path(args.baseline))
        return 1 if findings else 0
    finally:
        if args.rev and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


def _debug_dump(idx: Index, summ: Summarizer, substr: str):
    print(f"[debug] 函数摘要（符号含 {substr!r}）", file=sys.stderr)
    for m in idx.modules.values():
        for q, fi in m.funcs.items():
            if substr not in q:
                continue
            s = summ.summary(m.modname, q)
            fmt = lambda st: sorted(summ.cells.get(c, str(c)) for c in st)
            print(f"  {m.relpath} :: {q}", file=sys.stderr)
            print(f"    direct_reads={fmt(s.direct_reads)}", file=sys.stderr)
            print(f"    writes_uncond={fmt(s.writes_uncond)}", file=sys.stderr)
            print(f"    writes_cond={fmt(s.writes_cond)}", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="preflight", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("shapes", help="静态形状扫（不跑代码）")
    ps.add_argument("--rev", help="扫这个 git rev 的树（git archive 到临时目录，不 checkout）")
    ps.add_argument("--root", help="扫这个目录（默认：仓库根 / 工作区）")
    ps.add_argument("--json", action="store_true", help="JSON 输出")
    ps.add_argument("--keep", help="--rev 时把临时树留在指定目录（调试用）")
    ps.add_argument("--b3-threshold", type=float, default=0.45,
                    help="B3 近似重复的相似度阈值（默认 0.45）")
    ps.add_argument("--debug", help="打印符号含该子串的函数摘要到 stderr")
    ps.add_argument("--baseline", help="对着基线比：只有**新增/内容变了**的命中才红（棘轮，给门用）")
    ps.add_argument("--write-baseline", help="把当前命中写成基线（唯一会写盘的分支）")

    pl = sub.add_parser("live", help="真机现场自查（只读）")
    pl.add_argument("--root", help=".qidian 所在的仓库根（默认：当前 git 仓库根）")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--since-hours", type=float, default=None,
                    help="只看最近 N 小时的告警（默认全部）")
    pl.add_argument("--limit", type=int, default=50, help="最多列多少条告警")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "shapes":
            return cmd_shapes(args)
        return cmd_live(args)
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
