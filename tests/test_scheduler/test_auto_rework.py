"""`_auto_rework_allowed` —— 验收不过时，要不要自己回一次实现层。

方案全文 `docs/最小闭环方案-20260918.md`（2026-09-20 路地，`4f7971ed` 同批）。

⚠️ **单测只验得了这个纯函数。** 方案自己的 §五 写着："必须真机，单测证明不了这套" ——
四条通过判据里最重的那条是「**回到 executing 之后，任务真的被派下去了**」，
那要靠调度循环，这里证明不了。**别拿本文件全绿当"这套成了"。**

钉五条（每条对应一次踩过的坑）+ 两条边界：

  ① 验收没真跑 ⇒ 不自动（回执行层修不了「架构没产出约束」那件事，纯空转）
  ② 路由不是 impl ⇒ 不自动（design 要重跑委员会 621 秒，且"理由进提示词 ≠ 模型照做"已证伪）
  ③ 已经自动返过一次 ⇒ 不自动（防无限循环；"第二次会成"没有证据）
  ④ 🔴 **开关默认关**（2026-09-21 起）—— 要开得显式设 `QIDIAN_AUTO_REWORK=1`。
     原来这里是"出事一句话关掉"，但**它自己就是那个"事"**：回炉一次 = 一次完整执行层
     （上一轮 1.2M token 量级），换来的只是"再摇一次骰子"。
  ⑤ 🔴 **调度循环没在跑 ⇒ 不自动**（护栏，2026-09-20 用户拍板加的）
"""
import pytest

from singularity.scheduler import _hooks
from singularity.scheduler import workflow as W


class _Proj:
    """只带这个判据读的两个字段 —— 不构造真 ProjectState（那要碰盘）。"""

    def __init__(self, issues=None, lineage=None):
        self.issues = issues or []
        self.lineage = lineage or []


def _ran():
    """验收真跑过时的 issues 形状（`workflow._flag_verification_ran` 写的那条）。"""
    return [{"type": "verification_ran", "detail": "QA + 安全审计已执行"}]


@pytest.fixture(autouse=True)
def _循环在跑(monkeypatch):
    """把调度循环设成"在跑"、并且**显式把开关打开** —— ④⑤ 各自单独测，
    别让它们在其他用例里挡道。

    ⚠️ 开关这里必须**显式设成 1**：2026-09-21 起默认是**关**的（见
    `test_默认是关的`），本文件其余用例讲的是"其余四条判据"，得先把这道闸门让开。
    """
    monkeypatch.setattr(_hooks, "loop_status", lambda: {"running": True, "concurrent": 2})
    monkeypatch.setenv("QIDIAN_AUTO_REWORK", "1")


# ═══════════════════════════════════════════════════════════════
# ① 正题：五条全真 ⇒ 放行
# ═══════════════════════════════════════════════════════════════

def test_五条全真就放行():
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran()), "impl")
    assert ok is True, why


# ═══════════════════════════════════════════════════════════════
# ② 每一条刹车单独验（**一条一条掐**，别一次掐全部）
# ═══════════════════════════════════════════════════════════════

def test_验收没真跑就不自动():
    """「验收跳过」不是「验收过」—— 架构没产出约束清单时回执行层修不了它，纯空转烧钱。

    ⚠️ 刻意**不用** `has_verification_evidence()`：它把 `verification_skipped`
    也算"有结论"。变异：判据换成那个 ⇒ 本条红。
    """
    skipped = [{"type": "verification_skipped", "detail": "验收跳过: 架构没产出约束清单"}]
    ok, why = W._auto_rework_allowed(_Proj(issues=skipped), "impl")
    assert ok is False and "验收" in why


def test_没有验收标记也不自动():
    ok, why = W._auto_rework_allowed(_Proj(issues=[]), "impl")
    assert ok is False and "验收" in why


@pytest.mark.parametrize("route", ["design", "note", ""])
def test_不是_impl_就不自动(route):
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran()), route)
    assert ok is False and "实现层" in why


def test_已经自动返工过就不再来一次():
    """防无限循环。**"同一件事第二次会成"没有证据支持**（方案 §三 的 N=1 依据）。"""
    once = [{"action": "gate3_rejected", "auto": True}]
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran(), lineage=once), "impl")
    assert ok is False and "返工" in why


def test_人工打回不算进自动的次数():
    """⚠️ **命门**：数的是 `auto: True` 的那几条，不是所有 `gate3_rejected`。

    人点过三次打回、一次都没自动过 ⇒ 这次该允许自动。
    变异：把 `and e.get("auto")` 去掉 ⇒ 本条红（人工的账把自动的口子堵死了）。
    """
    human = [{"action": "gate3_rejected", "feedback": "人点的"}] * 3
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran(), lineage=human), "impl")
    assert ok is True, why


def test_默认是关的(monkeypatch):
    """🔴 **2026-09-21 起默认不自动** —— 用户原话「这合理吗，白烧 token」。

    回炉一次 = 一次完整执行层（上一轮 1.2M token 量级），而换来的只是
    **"再摇一次骰子"**（重跑的输入跟第一次一字不差，见 `_build_effective_task`）。
    ⇒ 默认关；要开得**显式**设 `QIDIAN_AUTO_REWORK=1`。
    """
    monkeypatch.delenv("QIDIAN_AUTO_REWORK", raising=False)
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran()), "impl")
    assert ok is False, "默认必须是关的 —— 开着就是每轮白烧一次执行层"
    assert "默认关" in why, why


def test_显式设成0也不自动(monkeypatch):
    """**对照**：显式写 0 当然也不自动（旧写法继续有效，别把老配置弄坏）。"""
    monkeypatch.setenv("QIDIAN_AUTO_REWORK", "0")
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran()), "impl")
    assert ok is False and "开关" in why


def test_显式设成1才恢复自动(monkeypatch):
    """**对照**：这条路没被堵死 —— 显式开就能回到旧行为（测试夹具走的就是这条）。"""
    monkeypatch.setenv("QIDIAN_AUTO_REWORK", "1")
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran()), "impl")
    assert ok is True, why


# ═══════════════════════════════════════════════════════════════
# ③ 护栏（2026-09-20 用户拍板加的那条）
# ═══════════════════════════════════════════════════════════════

def test_调度循环没在跑就不自动(monkeypatch):
    """🔴 **本方案最大的一条风险，这条就是它的刹车。**

    回 EXECUTING 只是改了个 phase，**真正派活得靠调度循环**。
    循环没开 ⇒ 自动返工 = 把项目扔在一个不动的地方，
    而且比"停在 GATE3 等人"**更糟** —— 人在 GATE3 至少看得见，停在 EXECUTING 看不见。

    变异：把 `_hooks.loop_status()` 那一支删掉 ⇒ 本条红。
    """
    monkeypatch.setattr(_hooks, "loop_status", lambda: {"running": False, "concurrent": 0})
    ok, why = W._auto_rework_allowed(_Proj(issues=_ran()), "impl")
    assert ok is False, "循环没开还自动回 —— 项目会被扔在一个没人看得见的地方"
    assert "循环" in why


def test_查不到循环状态也按没跑处理(monkeypatch):
    """**保守一侧**：`_hooks` 没注册（无头 / CLI）时 `loop_status()` 回
    `{"running": False}` —— 那条路本来就没人派活，所以"不知道"和"没在跑"同一个结论。"""
    monkeypatch.setattr(_hooks, "loop_status", lambda: {"running": False})
    ok, _ = W._auto_rework_allowed(_Proj(issues=_ran()), "impl")
    assert ok is False


# ═══════════════════════════════════════════════════════════════
# ④ 留痕：**自动和人工在账上要分得开**（不测的话，那条 lineage 迟早漂）
# ═══════════════════════════════════════════════════════════════

class _LineageProj:
    """`handle_gate3_reject` 用到的全部（`task_ids` 空 ⇒ 不碰 tracker）。"""

    def __init__(self):
        self.id = "T-reject"
        # ⚠️ 带 `verification_ran`：不然 ① 会先一步拦下，
        # 「③ 已经自动返工过」那条刹车**根本没被走到**（夹具初值够不着判据 —— 第十四次那条）。
        self.issues = _ran()
        self.phase = None
        self.task_ids = []
        self.lineage = []

    def set_phase(self, phase, reason=""):
        self.phase = phase

    def add_lineage(self, entry):
        self.lineage.append(entry)


def _drive_reject(monkeypatch, auto: bool):
    """真调 `handle_gate3_reject`，把它的 lineage 记下来。

    ⚠️ **第一版这条测的是我自己写的夹具**（`p.add_lineage({... "auto": True})`），
    把生产代码里那行 `**({"auto": True} if auto else {})` 删掉它**照样绿** ——
    典型「自检自己掐不断」（记忆里第十三次那条）。必须走真函数。
    """
    monkeypatch.setattr(W, "save", lambda p: None)
    monkeypatch.setattr(W, "_resolve_fix_route", lambda p: ("impl", "qa_report", "", []))
    p = _LineageProj()
    W.handle_gate3_reject(p, {}, feedback="自动返工: QA 判实现层不合格", auto=auto)
    return p


def test_自动的那条_lineage_带_auto_标记(monkeypatch):
    """人点的和它自己回的原来写得**一模一样**（都只是 `gate3_rejected`）——
    查"这轮是谁按的"只能靠猜。变异：去掉 `**({"auto": True} if auto else {})` ⇒ 红。
    """
    p = _drive_reject(monkeypatch, auto=True)
    entry = next(e for e in p.lineage if e.get("action") == "gate3_rejected")
    assert entry.get("auto") is True, f"自动返工没留下可辨认的痕：{entry}"


def test_人工点的那条不带_auto(monkeypatch):
    """反过来 —— 别把人工的账也标成自动（那就成了另一个方向的撒谎）。"""
    p = _drive_reject(monkeypatch, auto=False)
    entry = next(e for e in p.lineage if e.get("action") == "gate3_rejected")
    assert "auto" not in entry, f"人工打回被标成了自动：{entry}"


def test_留痕和判据对同一个约定(monkeypatch):
    """**两处必须认同一件事**：写进去的 `auto: True` 要能被 `_auto_rework_allowed` 数到
    —— 否则"防无限循环"那道刹车永远数是 0，等于没有。"""
    p = _drive_reject(monkeypatch, auto=True)
    ok, why = W._auto_rework_allowed(p, "impl")
    assert ok is False and "返工" in why, "写进去的痕判据数不出来 —— 刹车是空的"


if __name__ == "__main__":            # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ═══════════════════════════════════════════════════════════════
# ⑤ 接线：`run_test_fix_loop` 真的走了这一支吗
#
# ⚠️ 上面 12 条全绿，也**证明不了** `run_test_fix_loop` 里那句 `if _ok:` 存在 ——
# 这正是本仓"函数对 ≠ 接线通"那条（删掉接线，纯函数测试照样绿）。
# ═══════════════════════════════════════════════════════════════

class _LoopProj:
    """`run_test_fix_loop` 用到的全部：issues / phase / id。"""

    def __init__(self):
        self.id = "T-auto"
        self.issues = []
        self.phase = None
        self.task_ids = []

    def set_phase(self, phase, reason=""):
        self.phase = phase

    def add_lineage(self, entry):
        pass


def _drive_loop(monkeypatch, allow: bool):
    """把 `run_test_fix_loop` 跑起来，返回 (项目, 记下来的 handle_gate3_reject 调用)。"""
    calls = []
    monkeypatch.setattr(W, "_collect_changed_files", lambda p: [])
    monkeypatch.setattr(W, "_run_verification", lambda p, a: [])
    monkeypatch.setattr(W, "save", lambda p: None)
    monkeypatch.setattr(W, "_resolve_fix_route",
                        lambda p: ("impl", "qa_report", "", []))
    monkeypatch.setattr(W, "_auto_rework_allowed",
                        lambda p, r: (allow, "假理由" if not allow else "五条全真"))
    monkeypatch.setattr(W, "handle_gate3_reject",
                        lambda p, a, feedback="", auto=False: calls.append(auto) or "已回实现层")
    p = _LoopProj()
    msg = W.run_test_fix_loop(p, {})
    return p, calls, msg


def test_放行时走自动返工_不升_GATE3(monkeypatch):
    """变异：删掉 `run_test_fix_loop` 里那个 `if _ok:` 分支 ⇒ 本条红。"""
    p, calls, msg = _drive_loop(monkeypatch, allow=True)
    assert calls == [True], f"没把 auto=True 传下去 —— 账上分不出是它自己回的：{calls}"
    assert p.phase is not W.Phase.GATE3, "放行了却还是升了 GATE3"


def test_不放行时老实升_GATE3_并且不碰_handle(monkeypatch):
    """**反过来也要对**：不许把"没放行"静默吞掉。"""
    p, calls, msg = _drive_loop(monkeypatch, allow=False)
    assert calls == [], "没放行却调了打回"
    assert p.phase is W.Phase.GATE3


def test_没自动时把为什么写进消息里(monkeypatch):
    """🔴 "没自动"和"没走到这一步"在界面上必须分得开 —— 这也是本仓的老形状。"""
    p, calls, msg = _drive_loop(monkeypatch, allow=False)
    assert "没自动返工" in msg and "假理由" in msg, msg
