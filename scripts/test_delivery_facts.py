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


def _with_qd(tmp: Path, fn):
    """把现场指到临时目录，跑 `fn()`，原样返回它的结果。

    ⚠️ **只用内存 patch**，不调 `project.set_projects_root` —— 那个会写
    `settings.json`（`_settings_path` 就是 `config.QIDIAN_DIR/settings.json`）。
    虽然这里 QIDIAN_DIR 已经先指到 temp 了，但少一次持久化就少一个"跑测试把生产改了"的机会
    （本仓栽过，见 §56 同族）。

    🔵 `--rounds` 那节也要用同一份 patch —— **两份 patch 就是两个"还原顺序写反"的机会**，
    而那正是下面 `_PROD_SETTINGS` 那个守卫在防的事，所以合并成一个。
    """
    old_qd, old_repo_dir = config.QIDIAN_DIR, proj_mod.repo_dir
    config.QIDIAN_DIR = tmp / ".qidian"
    proj_mod.repo_dir = lambda pid: tmp / "项目仓库" / "演示项目"
    try:
        return fn()
    finally:
        config.QIDIAN_DIR = old_qd
        proj_mod.repo_dir = old_repo_dir


def run_facts(tmp: Path) -> str:
    """跑一遍 `facts()`，收回 stdout。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        _with_qd(tmp, lambda: df.facts("P1"))
    return buf.getvalue()


def run_rounds(tmp: Path) -> str:
    """跑一遍 `rounds_table()`，收回 stdout。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        _with_qd(tmp, df.rounds_table)
    return buf.getvalue()


def make_rounds_scene(tmp: Path) -> None:
    """两轮 + 三个侧车。**照抄 2026-09-20 真机那个形状**：

      轮 1001 —— 进仓的那个提交**只动了 README.md**（样板），真产物 `jsonlstat/stats.py`
                  挂在 `refs/qidian/pending/2002` 上没进仓；
      轮 1002 —— 一个都没进仓，**没有产物文件**。

    这个形状就是「进仓 +N 行」那把假尺子的案发现场：数字看着像好消息，拆开全是样板。
    """
    qd = tmp / ".qidian"
    (qd / "tasks").mkdir(parents=True, exist_ok=True)
    (qd / "projects").mkdir(parents=True, exist_ok=True)
    repo = tmp / "项目仓库" / "演示项目"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "init")

    # 2001：标题是 `_AGENT_COMMIT` 认的那个格式（`agent changes in <数字>_`），但内容是样板
    (repo / "README.md").write_text("hello\nworld\n" * 20, encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "agent changes in 2001_any")

    # 2002 —— **两栏都在**那个形状（2026-09-20 真机任务 `1789836336312` 的实况）：
    # 先一笔样板进 HEAD，**一小时后又交了真产物、锚在 ref 上没进仓**。
    # 真机那个是 `pyproject.toml +36` 进仓 / `jsonlstat/*.py +1217` 留在锚上。
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n" * 5, encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "agent changes in 2002_any")

    # 2002 的真产物 —— 提交完锚在 pending ref 上（`_anchor_ref` 干的事），没合并
    (repo / "jsonlstat").mkdir(exist_ok=True)
    (repo / "jsonlstat" / "stats.py").write_text("def stats():\n    return 1\n" * 10, encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "wip 2002")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    _git(repo, "update-ref", "refs/qidian/pending/2002", sha)

    for pid, name, tids, phase in (("1001", "第一轮", [2001, 2002], "gate2"),
                                   ("1002", "第二轮", [2003], "executing")):
        json.dump({"id": pid, "name": name, "phase": phase, "auto_mode": False,
                   "task_ids": tids, "description": name},
                  open(qd / "projects" / f"{pid}.json", "w", encoding="utf-8"),
                  ensure_ascii=False)
    json.dump({"id": "2001", "project_id": "1001", "status": "done", "description": "任务一"},
              open(qd / "tasks" / "2001.json", "w", encoding="utf-8"), ensure_ascii=False)
    json.dump({"id": "2003", "project_id": "1002", "status": "failed", "description": "任务三"},
              open(qd / "tasks" / "2003.json", "w", encoding="utf-8"), ensure_ascii=False)

    json.dump({"summary": json.dumps({"total_checks": 5, "passed": 0, "failed": 5,
                                      "verdict": "no_go"}), "issues": "[]"},
              open(qd / "projects" / "1001.qa_report.json", "w", encoding="utf-8"))
    # 侧车三件套：**没有一件是项目**，一件都不许被当成一轮摆上桌
    json.dump({}, open(qd / "projects" / "1001.executable_tasks.json", "w", encoding="utf-8"))
    json.dump({}, open(qd / "projects" / "1001.machine-checks.json", "w", encoding="utf-8"))
    (qd / "projects" / "1002.qa_report.json").write_text("{坏掉的 json", encoding="utf-8")


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

    print("── ④ 跨轮次对照：几轮要摆得在一起，且「行数」必须连着「文件名」 ──")
    t6 = Path(tempfile.mkdtemp())
    make_rounds_scene(t6)
    try:
        out_r = run_rounds(t6)
        # ⚠️ 别用 `"进仓" in l` —— **表头那句解释里也有「进仓」**，会把表头当成一轮。
        # 只认正文那些行（正文行是缩进后**以「进仓」开头**的）。
        rows = [l for l in out_r.splitlines() if l.strip().startswith("进仓")]

        # ⚠️ 这条钉的是「每一轮都印出了集成那一行」，**不是**「屏幕上有几轮」——
        # 侧车冒充轮次时它照样是绿的（`load` 返 None，那一类压根不印「进仓」行）。
        # 管"轮数"的是下面那条「名字一个都不许出现」。
        check("每一轮都印出了「进仓」那一行", len(rows) == 2, f"拿到 {len(rows)} 行：\n{out_r}")
        check("侧车没被摆上桌 —— 名字一个都不许出现在输出里",
              not any(s in out_r for s in (".qa_report", ".executable_tasks",
                                           ".machine-checks")),
              f"这些名字冒出来了：\n{out_r}")
        check("越新越靠下（顺序 = 时间）",
              out_r.index("1001") < out_r.index("1002"), out_r)

        # 🔴 **这条是这个脚本存在的理由**：真机上「进仓 +478 行」看着像好消息，
        # 拆开全是 README / pyproject 样板、产物一行没进树。所以行数**不许单独出现**。
        check("进仓那行把文件名印出来了（数字不许单独出现）",
              "README.md" in rows[0], f"第一轮那行是：{rows[0]}")
        check("进仓数字对得上（2/2 任务）", "进仓 2/2 任务" in rows[0], rows[0])
        check("「文件」栏只列进仓的，不许混进没进仓的产物",
              "jsonlstat" not in rows[0], rows[0])
        check("没进仓那半边也在同一行", "没进仓 1 任务" in rows[0], rows[0])

        # 🔴 **两栏的重叠必须印出来**：真机上 `…312` 两栏都在，而屏幕上看着像 `2 + 7 = 9`
        # 的干净划分 —— 读者一相加就得到一个**假的全覆盖**（计数判据掩盖缺口同族）。
        check("两栏重叠的个数和任务号都印出来了",
              "两栏都在 1 个（2002）" in rows[0], rows[0])
        check("没有重叠时也印（哪怕是 0）—— 有条件的字段会让几轮对不齐",
              "两栏都在 0 个" in rows[1], rows[1])

        check("一个都没进仓时印「（无）」，不是留空", "文件 （无）" in rows[1], rows[1])
        check("第二轮进仓 0/1", "进仓 0/1 任务" in rows[1], rows[1])

        # 侧车（`.qa_report` / `.executable_tasks` / `.machine-checks`）**一件都不是项目**
        stems = _with_qd(t6, lambda: [p.stem for p in df._project_files()])
        check("侧车三件套一个都没被当成轮次",
              stems == ["1001", "1002"], f"拿到 {stems}")

        # `_qa_verdict` 是本轮新增的：**「没有」和「读不出来」在这儿也得分开**
        check("QA：没有 ⇒「—」", _with_qd(t6, lambda: df._qa_verdict("9999")) == "—")
        check("QA：文件坏了 ⇒「⚠️坏」且**不是**「—」",
              _with_qd(t6, lambda: df._qa_verdict("1002")) == "⚠️坏",
              "坏文件被当成了「没跑过」—— 这正是要防的那个形状")
    finally:
        shutil.rmtree(t6, ignore_errors=True)

    print("── 守卫：生产 settings.json 一个字都没动 ──")
    check("生产 settings.json 指纹不变",
          _fingerprint() == _fp_before,
          f"跑了测试却改到了 {_PROD_SETTINGS} —— 这正是 09-16 那次事故的形状")

    print("\n" + "=" * 48)
    print(f"{'✅ 全通过!' if FAIL == 0 else '❌ 有失败'}  通过 {PASS} / 失败 {FAIL}")
    print("=" * 48)
    sys.exit(0 if FAIL == 0 else 1)
