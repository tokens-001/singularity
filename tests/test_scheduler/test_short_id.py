"""短 id 必须取**后** 8 位（2026-09-15 真机坐实）。

现场：越界告警原文是「**17894825** 改了本属 **17894825** 的文件 fizzbuzz.py」——
两个 id 撞成一个，人审页上读起来像"自己改了自己"，**等于没有这条信息**
（而它长得像有）。真身是 `1789482513688` 和 `…691`，规划器一次拆出来的。

根因：任务号/项目号是**毫秒时间戳**，`[:8]` 砍掉的正好是后 5 位毫秒 ⇒
留下的前 8 位**每 100 秒才进一位**，同一批任务截出来全长一个样。
"""
from types import SimpleNamespace

from singularity.scheduler import tracker, workflow


# ── 判据本身 ──────────────────────────────────────────────

def test_同一批任务的短id必须不同_这正是旧写法坏掉的地方():
    a, b = "1789482513688", "1789482513691"     # 真机里同一批的两个任务号
    assert a[:8] == b[:8], "前提：旧写法在这两个上确实撞车 —— 撞不了这条用例就没意义"
    assert tracker.short_id(a) != tracker.short_id(b), "新写法必须能把它们分开"


def test_短串原样返回不炸():
    assert tracker.short_id("abc") == "abc"
    assert tracker.short_id("") == ""


# ── 接线：那条告警真的用上了它 ─────────────────────────────

def test_越界告警里两个任务号读得出来(monkeypatch):
    """删掉 `_flag_file_overlap` 里那两处 `short_id`，这条必须变红。"""
    a, b = "1789482513688", "1789482513691"
    fake = {
        a: SimpleNamespace(description="编写 test_fizzbuzz.py 覆盖五类用例"),
        b: SimpleNamespace(description="实现 fizzbuzz.py 全部 CLI 契约"),
    }
    monkeypatch.setattr(tracker, "read_task", lambda tid: fake.get(tid))
    # a 改了 b 点名、a 自己没点名的文件 ⇒ 越界
    monkeypatch.setattr(workflow, "_changed_files_of",
                        lambda tid: {"fizzbuzz.py"} if tid == a else set())
    # `witness` 是在函数里就地 import 的，所以打模块属性（打 workflow.witness 没用）
    from singularity.scheduler import witness as _witness
    monkeypatch.setattr(_witness, "warn", lambda *a_, **k_: None)

    proj = SimpleNamespace(id="1789480000000", task_ids=[a, b], issues=[])
    workflow._flag_file_overlap(proj)

    assert len(proj.issues) == 1, "该报没报 —— 用例前提塌了"
    detail = proj.issues[0]["detail"]
    assert tracker.short_id(a) in detail and tracker.short_id(b) in detail, \
        f"两个任务号必须都在告警里：{detail}"
    assert detail.index(tracker.short_id(a)) != detail.index(tracker.short_id(b)), \
        f"两个号必须是不同的两串，否则读不出谁改了谁的：{detail}"


# ── 钉子：不许再有 `xxxid[:8]` ────────────────────────────

def test_全仓不再有_id截前8位的写法():
    """`[:8]` 对**毫秒时间戳**是错的（见模块头）。

    ⚠️ 只禁 `…id[:8]` 这一种形状 —— `new_head[:8]`（git sha）、分支名、模型名
    截前 8 位都是对的，别一刀切。要写就写 `tracker.short_id(x)`。
    """
    import re
    from pathlib import Path
    root = Path(workflow.__file__).resolve().parents[2]      # src/singularity
    bad = []
    for p in root.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\w*id\[:8\]", code) and "short_id" not in code:
                bad.append(f"{p.relative_to(root)}:{i}: {line.strip()}")
    assert not bad, "又出现截前 8 位的 id 了：\n" + "\n".join(bad)
