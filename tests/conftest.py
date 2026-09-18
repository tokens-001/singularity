"""共享 fixtures — Singularity 单元测试。"""
import atexit, os, shutil, tempfile, pytest
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# 🔴 **收集期也要隔离**（2026-09-18 补）—— 下面那个 autouse 夹具**只覆盖用例执行期**
# ═══════════════════════════════════════════════════════════════
# pytest **导入测试模块**（收集）发生在任何 fixture 生效**之前**，而测试模块顶层往往就是
# 一句 `from singularity.scheduler.dispatcher import ...` ⇒ 那一刻里面任何
# `witness.warn` / `tracker` 落盘**直接写进真 `.qidian/`**。
#
# 实测（不是推断）：2026-09-18 一次 `pytest tests/` 就往真的 `.qidian/alerts.jsonl`
# 追加 **1 条 `lazy_spoke_import_failed`**（根因见 `dispatcher.__getattr__` 那段）。
# 危害不在这一条，而在**那个文件是查故障用的账本** —— 我当天差点拿它算出
# "后端空转时反复失败"这种错误结论（那几条全是我自己跑测试写的）。
# 同一个形状下面那段注释里已经记过一次（"整套测试追加 12 行"），**这是第二次**。
#
# ⇒ 在**导入期**先把 `QIDIAN_DIR` 指到临时目录；用例再各自用 `tmp_path` 覆盖，
#   用例结束 `monkeypatch` 撤销时**回到这里设的值**，不会再漏到真目录。
_COLLECT_TMP = Path(tempfile.mkdtemp(prefix="qidian-collect-"))
atexit.register(lambda: shutil.rmtree(_COLLECT_TMP, ignore_errors=True))
from singularity.scheduler import config as _cfg  # noqa: E402
_cfg.QIDIAN_DIR = _COLLECT_TMP
for _n in ("SNAPSHOT_DIR", "PATCH_DIR", "TRACE_DIR", "HOLD_DIR", "CANCEL_DIR",
           "PAUSE_DIR", "PARKED_DIR", "PARTIAL_USAGE_DIR"):
    setattr(_cfg, _n, _COLLECT_TMP / getattr(_cfg, _n).name)


@pytest.fixture(autouse=True)
def _isolate_qidian_dir(tmp_path, monkeypatch):
    """把 `config.QIDIAN_DIR` 及**所有从它派生的路径**指到临时目录。

    以前不隔离：被测代码里的 witness.warn / tracker 落盘会真写生产目录，
    实测整套测试往 `.qidian/alerts.jsonl` 追加 12 行（跑测试污染排查数据 ——
    alerts.jsonl 正是拿来查故障的，混进测试噪声就废了）。

    **只改 QIDIAN_DIR 是不够的** —— `config` 里那几个派生目录在**导入时**就算好了，
    属性改了它们不变，用到它们的代码照样写真实目录：
      · config.{SNAPSHOT,PATCH,TRACE,HOLD,CANCEL,PAUSE,PARKED,PARTIAL_USAGE}_DIR
    ⚠️ **加了新的 `config.X_DIR` 就必须回来往下面那个元组里加一行** ——
    2026-09-12 加 `PARTIAL_USAGE_DIR` 时漏了，`test_exec_run.py` 一跑就把真
    `.qidian/partial_usage/` 建了出来（§56 的同一个坑，第 N 次）。
    （内存模块与 `route_learner` 原来也要在这里单独补一刀，**2026-09-11 已改成
    读时现算**，见各自的 `_events_path()` / `_learner_path()` —— 补丁撤了。）
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    for name in ("SNAPSHOT_DIR", "PATCH_DIR", "TRACE_DIR", "HOLD_DIR",
                 "CANCEL_DIR", "PAUSE_DIR", "PARKED_DIR", "PARTIAL_USAGE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)


@pytest.fixture
def tmp_project_root(tmp_path):
    """提供隔离的项目根目录。"""
    old = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(old)


@pytest.fixture
def tmp_workdir():
    """提供临时工作目录 (含文件写入)。"""
    d = tempfile.TemporaryDirectory()
    yield d.name
    d.cleanup()


def make_task(tid, priority=0, starvation=0, level="any", children=None):
    """快捷构造 task 对象。"""
    return type("T", (), {
        "id": tid,
        "priority": priority,
        "starvation_score": starvation,
        "route_level": level,
        "children": children or [],
        "status": "pending",
    })()
