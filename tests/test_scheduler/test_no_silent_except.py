"""静默 except 守卫 —— 让「既不出声也不上抛」这个形状**不再新增**（2026-09-13）。

## 它为什么存在

全仓 `except` 里 **433 处（77%）既不出声也不上抛**（普查于 2026-09-13，565 个 handler /
66 个文件）。这个形状是**静默失败**的主要产地 —— 事故库里 §55 / §59 / §63 / §64 / §65
全部或部分是它生的：功能**从来没跑过**、任务被砍**什么都没留下**、名字没导入
**只在告警里留一句**。

⚠️ **但"挨个加告警"是错的**（那会把告警筛子糊死 —— `alert_summary` 当初存在的理由
就是"25 条告警 22 条挤在两个 key 上"）。所以这台守卫的收益**不是"改干净"**，是
**"这个形状不再新增"** —— 存量只按"碰哪个文件顺手清哪个"收敛，**不做批次**。

## 判据（保守化：宁可误报逼人豁免，不可漏报）

一个 handler 算"出声/上抛"，当且仅当它体内**含 `raise`**，或者调用了**已知出声函数**：
`witness.*` / `logging.*` / `log.*`。

**调用其他任何函数都不算出声** —— 包括 `print`、`traceback.*`、自己写的 `_warn_xxx`。
这条是刻意的：判据松一寸，它就钝一分。误报了就往基线上加，加的时候在提交信息里写清理由。

`contextlib.suppress` **同罪**，一并计数（全仓现在是 **0**，所以任何一处新增都会红）。

## ⚠️ 射程边界（写在这儿免得"守卫绿"给人虚假安全感 —— §64 的教训）

它**只看 handler 级的吞噬**。**future 级吞噬（`.result()` 永不被调）明确不在射程**
—— 那是"线程池包一层"那条方案的地盘。

它**也看不出"你出声了但没人看"** —— 判据是"有没有出声"，不是"声音有没有出口"。

## 存量怎么记：不是白名单，是**按函数的棘轮**

433 条按行写理由不可行（写不出 433 个真理由，只会批量糊过去）。改用
`{(文件, 函数): 处数}` 的**计数棘轮**：**任何增减都红**。

- 新增一处 ⇒ 那个函数的计数 +1 ⇒ 红，**且报文直接点到文件与函数**，不用自己找。
- 修好一处 ⇒ 计数 -1 ⇒ **也红**，逼你把基线调小（这就是"棘轮只往一个方向转"）。
- 改动导致行号漂移 ⇒ **不受影响**（判据里没有行号）。

**已知天花板（刻意接受）**：同一个函数里"删一处、加一处"能溜过去（总数不变）。
按行写理由能堵住它，但 433 条的代价是那个名单**没人看**。要升级就在基线里给每条加
`reason` 字段 —— 等存量降到几十条再说。
"""
import ast
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "singularity"
BASELINE = Path(__file__).with_name("silent_except_baseline.json")

# 「算作出声」的根名字。**只认这三个** —— 见模块 docstring 的保守化说明。
NOISY_ROOTS = frozenset({"witness", "logging", "log"})


def _noisy_roots(tree: ast.Module) -> frozenset[str]:
    """本模块里**指向出声根**的名字 —— 含 `import ... as ...` 的别名。

    ⚠️ 原来只认 `witness` / `logging` / `log` 三个**字面**名字（2026-09-14 修）。
    可本仓大量用别名，最典型的是 `mcp.py` 的
    `from singularity.scheduler.log import info as _log_info, warn as _log_warn` ——
    **守卫看不见它**，于是明明调了 `_log_warn` 的 handler 被记成"静默"（**虚增**）。
    这就是模块 docstring 那句"**量尺不准，所有棘轮数字都是假的**"的另一个面：
    上次修的是**虚减**（下钻到嵌套 handler），这次修的是**虚增**。
    ⇒ 别名一律按 import 解析出来，而不是去改业务代码迁就尺子。
    """
    roots = set(NOISY_ROOTS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in NOISY_ROOTS:
                    roots.add(a.asname or top)
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "").split(".")[-1]
            for a in node.names:
                # `from ...log import warn as _w`（base 是根）和
                # `from ... import log as _l`（被导入的名字本身是根）都要认
                if base in NOISY_ROOTS or a.name in NOISY_ROOTS:
                    roots.add(a.asname or a.name)
    return frozenset(roots)


def _call_is_noisy(node: ast.Call, roots: frozenset[str]) -> bool:
    """这个调用算不算"出声"。**只认直接调 `witness` / `logging` / `log`（含 import 别名）。**

    ⚠️ **刻意不跟本地 helper**（2026-09-14 试过、又撤了）：把"调了一个碰巧会出声的helper"
    当成"这个 handler 自己出声了"，实测在 `orchestrator._reap_futures` 上造出**假阴性** ——
    那个外层 handler 的出声其实在**嵌套** handler 里（守卫 docstring 专门记过这条），
    它只是因为调了 `_save_trace` 才算数的。**"调了个会出声的函数" ≠ "报了自己的失败"。**
    代价：出声收在 helper 里的 handler 会被记成静默（虚增），那类按
    "确实无害才更新基线"处理，别为了让尺子好过而改代码。
    """
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f.value.id in roots
    return isinstance(f, ast.Name) and f.id in roots


def _is_noisy(handler: ast.ExceptHandler, roots: frozenset[str] = NOISY_ROOTS) -> bool:
    """这个 handler **自己**有没有出声 / 上抛。

    ⚠️ **不下钻到嵌套的 except 处理体里**（2026-09-13 修的一个洞）。
    原来直接用 `ast.walk`，而它会钻进嵌套 `try/except` 的**内层 handler** ——
    于是"内层出声"会被算成"外层也出声了"。实测：`_reap_futures` 里那个
    `except Exception as e:` 嵌了一个 `try: transition(...) except: pass`，
    我只给**内层**加了 warn，基线却报**外层也少一处** —— **虚减**，
    等于没修也记了功。**量尺不准，所有棘轮数字都是假的。**
    """
    def _calls_noisy(node) -> bool:
        return _call_is_noisy(node, roots)

    def _walk(node) -> bool:
        for child in ast.iter_child_nodes(node):
            # 内层 handler 的出声不算外层的 —— 它们的"债"分开记
            if isinstance(child, ast.ExceptHandler) and child is not handler:
                continue
            if isinstance(child, ast.Raise):
                return True
            if isinstance(child, ast.Call) and _calls_noisy(child):
                return True
            if _walk(child):
                return True
        return False

    return _walk(handler)


class _Scan(ast.NodeVisitor):
    """按**函数限定名**分组数静默 handler —— 不含行号，所以行号漂移不影响判据。"""

    def __init__(self, rel: str, roots: frozenset[str] = NOISY_ROOTS):
        self.rel = rel
        self.roots = roots
        self.stack: list[str] = []
        self.counts: Counter = Counter()
        self.handlers = 0
        self.suppress = 0

    def _qual(self) -> str:
        return ".".join(self.stack) or "<module>"

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_func(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Try(self, node):
        for h in node.handlers:
            if not h.body:
                continue
            self.handlers += 1
            if not _is_noisy(h, self.roots):
                self.counts[(self.rel, self._qual())] += 1
        self.generic_visit(node)

    def visit_With(self, node):
        for item in node.items:
            c = item.context_expr
            if (isinstance(c, ast.Call)
                    and getattr(c.func, "attr", "") == "suppress"
                    and getattr(getattr(c.func, "value", None), "id", "") == "contextlib"):
                self.suppress += 1
                self.counts[(self.rel, self._qual() + "·contextlib.suppress")] += 1
        self.generic_visit(node)


def scan() -> tuple[dict[str, int], int, int]:
    """→（计数表, handler 总数, suppress 总数）。键是 `"文件::函数"`。"""
    counts: Counter = Counter()
    handlers = suppress = 0
    for p in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:                       # 语法错轮不到这台守卫管
            continue
        s = _Scan(str(p.relative_to(SRC)), _noisy_roots(tree))
        s.visit(tree)
        counts.update(s.counts)
        handlers += s.handlers
        suppress += s.suppress
    return {f"{f}::{q}": n for (f, q), n in counts.items()}, handlers, suppress


def _load_baseline() -> dict[str, int]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _write_baseline() -> None:
    counts, handlers, suppress = scan()
    BASELINE.write_text(json.dumps(counts, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
    print(f"已写入 {BASELINE.name}：{sum(counts.values())} 处 / {len(counts)} 个函数 "
          f"（handler 总数 {handlers}，suppress {suppress}）")


def test_scan_is_not_blind():
    """**扫不到东西的守卫比没有更坏** —— 眼看绿，其实什么都没查。

    这两条自检是抄 `test_no_undefined_names.py` 的（它靠一模一样的断言守住自己）。
    数字掉下来说明**扫描逻辑坏了**（不是"存量清完了"）；真清完了请连自检一起改。
    """
    counts, handlers, _ = scan()
    assert handlers > 400, f"只扫到 {handlers} 个 handler —— 扫描逻辑可能坏了"
    assert sum(counts.values()) > 300, f"只扫到 {sum(counts.values())} 处静默 —— 判据可能坏了"


def test_no_new_silent_except():
    """静默 except 的分布必须和基线**逐项相等** —— 多一处少一处都红。

    - **多了** ⇒ 新加了一个既不出声也不上抛的 except，报文会点出**文件和函数**。
    - **少了** ⇒ 好事，但要**把基线调小**（棘轮只往一个方向转），否则这条守卫会
      慢慢变成一张陈旧的清单。两个方向都提示了怎么改。
    """
    cur, _, _ = scan()
    base = _load_baseline()
    added = {k: cur[k] - base.get(k, 0) for k in cur if cur[k] > base.get(k, 0)}
    removed = {k: base[k] - cur.get(k, 0) for k in base if base[k] > cur.get(k, 0)}

    msg = []
    if added:
        msg.append("❌ 新出现了静默 except（既不出声也不上抛）：")
        msg += [f"    {k}  多 {v} 处" for k, v in sorted(added.items())]
        msg.append("  ⇒ 要么加告警（`witness.warn(..., key=…)`）或上抛；"
                   "确实无害才更新基线（提交信息里写清为什么无害）。")
    if removed:
        msg.append("✅ 有地方清干净了 —— 请把基线调小（棘轮只往一个方向转）：")
        msg += [f"    {k}  少 {v} 处" for k, v in sorted(removed.items())]
    if msg:
        msg.append(f"\n重算基线：`.venv/bin/python {Path(__file__).name} --write`")
        pytest.fail("\n".join(msg))


def test_nested_handler_noise_does_not_count_for_the_outer():
    """**内层 handler 出声，不算外层出声** —— 判据的射程要切对。

    2026-09-13 修的一个洞：原来 `_is_noisy` 用 `ast.walk` 下钻，于是给内层加一句
    `witness.warn` 会**顺带把外层的"静默"也消掉** —— 我只改了 2 处，
    棘轮却报 `_reap_futures 少 3 处`。**虚减 = 没修也记功**，棘轮数字全不可信。
    """
    import ast as _ast
    outer = _ast.parse(
        "try:\n"
        "    a()\n"
        "except Exception as e:\n"
        "    try:\n"
        "        b()\n"
        "    except Exception:\n"
        "        witness.warn('x', 'boom')\n"
    ).body[0].handlers[0]
    assert not _is_noisy(outer), "内层出声被算成外层了 —— 棘轮会虚减"

    # 反过来：外层自己出声，当然要算
    outer2 = _ast.parse(
        "try:\n    a()\nexcept Exception:\n    witness.warn('x', 'boom')\n"
    ).body[0].handlers[0]
    assert _is_noisy(outer2)

    # 外层自己上抛，也算
    outer3 = _ast.parse(
        "try:\n    a()\nexcept Exception:\n    raise\n"
    ).body[0].handlers[0]
    assert _is_noisy(outer3)


def test_baseline_keys_are_wellformed():
    """基线文件本身要能被读懂：键必须都长成 `文件::函数`，且值都是正整数。

    防的是"手改基线"时把键写坏 —— 写坏的键**永远不会命中**，等于悄悄放行。
    """
    base = _load_baseline()
    bad = [k for k, v in base.items()
           if "::" not in k or not isinstance(v, int) or v <= 0]
    assert not bad, f"基线里有写坏的键（它们永远不会命中）: {bad[:5]}"


# ═══════════════════════════════════════════════════════════════
# 「心跳陈旧」的阈值必须大于「一次 dispatch 的合法上限」（2026-09-13）
# ═══════════════════════════════════════════════════════════════
# 放在这个文件里是因为它守的是同一类毛病：**判据定得比现实紧 ⇒ 假警报**。
# `witness.heartbeat` 全仓只有一个调用点（`_exec.run` 的外层 turn 循环），
# 所以两次心跳之间**正好夹着一次完整的 dispatch**。

def test_stalled_threshold_exceeds_one_legitimate_dispatch():
    """`check_stalled` 的默认阈值必须大于一次 dispatch 能合法跑的时间。

    钉的是 2026-09-13 量出来的一个**真 bug**：默认原本是 **600 秒**，
    而一次 dispatch 合法能跑满 `TASK_DEADLINE_S − TASK_WRAPUP_MARGIN_S`（=810s）
    —— 于是任何跑过一次长 dispatch 的任务都被报成 `stalled`，
    而 admin 接口和 observer 的 `_list_stalled_tasks` 会把这句**直接讲给用户听**。
    判据取宽不取紧：**假警报比晚报更坏**。
    """
    from singularity.scheduler import config, witness
    one_dispatch = config.TASK_DEADLINE_S - config.TASK_WRAPUP_MARGIN_S
    assert witness.STALLED_AFTER_S > one_dispatch, (
        f"STALLED_AFTER_S={witness.STALLED_AFTER_S} ≤ 一次 dispatch 的上限 {one_dispatch}"
        f" ⇒ 正常的长任务会被报成卡住")
    # 也别宽到没边：超过两倍死线就说明这个判据实际上永远不会响了
    assert witness.STALLED_AFTER_S < 2 * config.TASK_DEADLINE_S, \
        "阈值宽到 2 倍死线以上 —— 它实际上永远不会命中，等于没有这个判据"


def test_no_caller_hardcodes_a_tighter_stalled_threshold():
    """调用方**不许再各写一份更紧的值** —— 四个调用点原来都硬编码了 600。

    这是个纯文本检查（不改运行时代码），防的是"改了一处、别处又写回 600"。
    """
    import re
    src = Path(__file__).resolve().parents[2] / "src" / "singularity"
    bad = []
    for p in src.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "check_stalled(" not in line or "def check_stalled" in line:
                continue
            m = re.search(r"check_stalled\(\s*(?:timeout_seconds\s*=\s*)?(\d[\d_]*\.?\d*)", line)
            if m and float(m.group(1).replace("_", "")) < 900:
                bad.append(f"{p.relative_to(src)}:{i}  {line.strip()}")
    assert not bad, ("这些地方又把 stalled 阈值写紧了（会比一次 dispatch 的上限还小）：\n"
                     + "\n".join("  " + b for b in bad))


if __name__ == "__main__":
    if "--write" in sys.argv:
        _write_baseline()
    else:
        print(__doc__)
