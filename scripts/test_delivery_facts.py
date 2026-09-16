#!/usr/bin/env python3
"""delivery_facts 的成绩单 —— 用合成现场，不碰真 .qidian。

跑法（仓库根下）:
    .venv/bin/python scripts/test_delivery_facts.py

钉三件事：
  · ① **两把尺同框**：任务全 done ＋ 清单三项全空 ＋ 仓库里产物齐全 —— 这份输出里
    要能**同时**看见"账簿说零"和"磁盘说有"，脚本不许替用户选一个。
  · ② **「没有」和「读不出来」必须分得开**（本仓最恨的形状）。qa_report 文件不在
    ⇒ 打印"没跑过 QA"；文件在但 JSON 坏了 ⇒ 打印 ⚠️ 并说"不是没跑过"。
    **这两句一旦合并，出事时看到的就是一句"未产出"，跟真没跑过一模一样。**
  · ③ 产物那栏**不混进 .DS_Store / __pycache__** —— 混进来会把"有没有产物"读成有。

对照：正常项目不报 ⚠️（别把判据改宽成"凡读不出都报警"）。
"""
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from singularity.scheduler import config, project as proj_mod  # noqa: E402
import importlib.util                                          # noqa: E402

_spec = importlib.util.spec_from_file_location("df", ROOT / "scripts" / "delivery_facts.py")
df = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(df)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), capture_output=True, text=True, timeout=20)


def make_scene(tmp: Path, *, qa_report="ok", junk=False):
    """合成一个项目：真 git 仓 + 任务 json + qa_report + manifest + 账本。"""
    qd = tmp / ".qidian"
    (qd / "tasks").mkdir(parents=True, exist_ok=True)
    (qd / "projects").mkdir(parents=True, exist_ok=True)
    (qd / "deliverables" / "P1").mkdir(parents=True, exist_ok=True)
    repo = tmp / "项目仓库" / "演示项目"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "fizzbuzz.py").write_text("print(1)\n", encoding="utf-8")
    (repo / "test_fizzbuzz.py").write_text("assert 1\n", encoding="utf-8")
    if junk:                                    # ③ 的样本
        (repo / ".DS_Store").write_bytes(b"\x00junk")
        (repo / "__pycache__").mkdir(exist_ok=True)
        (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "init")
    _git(repo, "tag", "release/P1-202609160000")

    # 项目本体（`project.load` 读的就是它）
    json.dump({"id": "P1", "name": "演示项目", "phase": "done",
               "auto_mode": False, "task_ids": ["T1"],
               "description": "演示项目"},
              open(qd / "projects" / "P1.json", "w", encoding="utf-8"), ensure_ascii=False)
    json.dump({"id": "T1", "project_id": "P1", "status": "done", "error": "",
               "description": "演示任务"},
              open(qd / "tasks" / "T1.json", "w", encoding="utf-8"), ensure_ascii=False)

    if qa_report == "ok":
        json.dump({"passed": "[]", "issues": json.dumps([{
            "id": "c1", "severity": "critical", "fix_route": "design",
            "description": "测试文件是空的"}]),
            "summary": json.dumps({"total_checks": 16, "passed": 8, "failed": 8,
                                   "verdict": "no_go"})},
                  open(qd / "projects" / "P1.qa_report.json", "w", encoding="utf-8"),
                  ensure_ascii=False)
    elif qa_report == "corrupt":
        (qd / "projects" / "P1.qa_report.json").write_text("{坏掉的 json", encoding="utf-8")
    # qa_report == "missing" ⇒ 什么都不写

    # 清单三项全空 —— 就是真机上那个样子
    json.dump({"code_ref": "release/P1-202609160000", "artifacts": [], "docs": [], "reports": []},
              open(qd / "deliverables" / "P1" / "delivery_manifest.json", "w", encoding="utf-8"))

    json.dump({"daily": [{"project_id": "P1", "model": "deepseek-flash", "tokens": 12345}]},
              open(qd / "token_usage.json", "w", encoding="utf-8"))
    return repo


def run_facts(tmp: Path) -> str:
    """把现场指到临时目录，跑一遍 facts()，收回 stdout。

    ⚠️ **只用内存 patch**，不调 `project.set_projects_root` —— 那个会写
    `settings.json`（`_settings_path` 就是 `config.QIDIAN_DIR/settings.json`）。
    虽然这里 QIDIAN_DIR 已经先指到 temp 了，但少一次持久化就少一个"跑测试把生产改了"的机会
    （本仓栽过，见 §56 同族）。
    """
    old_qd, old_repo_dir = config.QIDIAN_DIR, proj_mod.repo_dir
    config.QIDIAN_DIR = tmp / ".qidian"
    proj_mod.repo_dir = lambda pid: tmp / "项目仓库" / "演示项目"
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            df.facts("P1")
    finally:
        config.QIDIAN_DIR = old_qd
        proj_mod.repo_dir = old_repo_dir
    return buf.getvalue()


# 🔴 **守卫**：这个脚本曾经把生产的 `projects_root` 覆盖掉过（2026-09-16 晚）。
# 当时的错在 `run_facts` 的 `finally`：先把 `config.QIDIAN_DIR` **还原成生产**，
# 再调 `project.set_projects_root(old_root)` —— 而 `_settings_path()` 就是
# `config.QIDIAN_DIR/settings.json` ⇒ **那一笔写进了生产**，且 `old_root` 还是
# 在临时环境里算出来的默认值（`~/qidian-projects`）。后果：奇点从此去一个空目录找项目仓。
# 教训同 §56：**"跑测试"和"改生产"之间只差一个还原顺序**。
# 所以这里不是"记得别写"，是**每次跑完都比指纹**。
_PROD_SETTINGS = (config.QIDIAN_DIR / "settings.json").resolve()


def _fingerprint() -> str | None:
    try:
        return hashlib.sha256(_PROD_SETTINGS.read_bytes()).hexdigest()
    except OSError:
        return None


if __name__ == "__main__":
    _fp_before = _fingerprint()
    print("── ① 两把尺同框：账簿说零、磁盘说有，两个都要看得见 ──")
    tmp = Path(tempfile.mkdtemp())
    try:
        make_scene(tmp)
        out = run_facts(tmp)
        check("任务栏打出来了", "① 任务" in out and "done" in out)
        check("清单三项写明是空的（账簿说零）",
              out.count("← 空") == 3, f"空了 {out.count('← 空')} 项")
        check("产物栏列出了真文件（磁盘说有）",
              "fizzbuzz.py" in out and "test_fizzbuzz.py" in out)
        check("产物栏报出了 release 标签（交付动作真发生过）",
              "release 标签 1 个" in out, out[-400:])
        check("账本栏读到了那一行", "12,345 tokens" in out)

        print("── ③ 产物栏不吃系统垃圾 ──")
        tmp2 = Path(tempfile.mkdtemp())
        make_scene(tmp2, junk=True)
        out2 = run_facts(tmp2)
        check("`.DS_Store` 没混进产物", ".DS_Store" not in out2)
        check("`__pycache__` 没混进产物", "__pycache__" not in out2)
        check("真文件照常在", "fizzbuzz.py" in out2)

        print("── ② 「没有」和「读不出来」必须长得不一样 ──")
        tmp3 = Path(tempfile.mkdtemp())
        make_scene(tmp3, qa_report="missing")
        out_missing = run_facts(tmp3)
        tmp4 = Path(tempfile.mkdtemp())
        make_scene(tmp4, qa_report="corrupt")
        out_corrupt = run_facts(tmp4)

        check("缺文件 ⇒ 说「没跑过 QA」", "没跑过 QA" in out_missing, out_missing[:400])
        check("坏文件 ⇒ 报 ⚠️ 并否认「没跑过」",
              "⚠️" in out_corrupt and "不是" in out_corrupt, out_corrupt[:400])
        check("两种情形**输出不同**（合并了就等于没分开）",
              out_missing != out_corrupt and "没跑过 QA" not in out_corrupt,
              "坏文件被当成了「没跑过」—— 这正是要防的那个形状")

        print("── 对照：正常项目不许报 ⚠️ ──")
        tmp5 = Path(tempfile.mkdtemp())
        make_scene(tmp5)
        out_ok = run_facts(tmp5)
        check("正常项目没有 ⚠️", "⚠️" not in out_ok, out_ok[:400])
        check("正常项目读得出 verdict", "no_go" in out_ok)
    finally:
        for d in (tmp, tmp2, tmp3, tmp4, tmp5):
            shutil.rmtree(d, ignore_errors=True)

    print("── 守卫：生产 settings.json 一个字都没动 ──")
    check("生产 settings.json 指纹不变",
          _fingerprint() == _fp_before,
          f"跑了测试却改到了 {_PROD_SETTINGS} —— 这正是 09-16 那次事故的形状")

    print("\n" + "=" * 48)
    print(f"{'✅ 全通过!' if FAIL == 0 else '❌ 有失败'}  通过 {PASS} / 失败 {FAIL}")
    print("=" * 48)
    sys.exit(0 if FAIL == 0 else 1)
