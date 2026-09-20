"""锚定 ref 的生命周期：**只有"产物真进了项目仓"才许松手**（2026-09-18）。

出处：`~/OPEN.md` 🔴「清任务会连带释放这个任务的 pending ref」。
那条记的是我自己踩的一次（删 21 个任务，3 个"判失败但产物还在"的锚当场没了），
但读下去发现**同一个形状有 5 处**，其中 4 处东西根本没进仓：

  · `orchestrator._drain_pending` —— 释放那句挂在 merged/conflict/failed **三个分支外面**
  · `orchestrator._account_salvaged` —— 超时 + worker 异常，**"活干了一半"最多的地方**
    （2026-09-18 一天 62 次被 240s 硬顶掐断全走这儿）
  · `_worktree.cleanup_task_artifacts` —— 藏在"清临时残留"里，被删任务/重试/worker 异常共三条路调用

而 `refs/qidian/pending/{task_id}` 的语义**只有一种读法**，写在
`_api_tasks.salvageable_refs` 的 docstring 里：

    **ref 还在 = 这个任务有可打捞的产物**

⇒ **释放 = 断言"产物已经安全进项目仓了"**。merged 是唯一让这句话成立的分支。

这些测试都**钉接线**：把释放挪回原处（或挪到别的分支），对应的那条必须红。
"""
import subprocess

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import tracker


# ═══════════════════════════════════════════════════════════════
# ① 清临时残留 ≠ 产物进仓 —— `cleanup_task_artifacts` 不许碰锚
# ═══════════════════════════════════════════════════════════════

def _git_repo(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    def run(*a):
        return subprocess.run(["git", *a], cwd=str(d), capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (d / "a.txt").write_text("x", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return d


def _ref_exists(repo, task_id) -> bool:
    r = subprocess.run(["git", "rev-parse", "--verify", "-q",
                        f"refs/qidian/pending/{task_id}"],
                       cwd=str(repo), capture_output=True, text=True)
    return r.returncode == 0


def _salvaged_sha(repo, task_id) -> str:
    r = subprocess.run(["git", "rev-parse", "--verify", "-q",
                        f"refs/qidian/salvaged/{task_id}"],
                       cwd=str(repo), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _obj_reachable(repo, sha) -> bool:
    """对象本身还捞得到吗 —— `git gc` 会不会收走它，就看这个。"""
    r = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                       cwd=str(repo), capture_output=True, text=True)
    return r.returncode == 0


def test_清临时残留时锚必须留下(tmp_path):
    """`cleanup_task_artifacts` 清掉了 patch 残留，**而锚纹丝不动**。

    ⚠️ 前半句（patch 真被删了）是这条测试的**另一半**，不能省：
    少了它，谁把整个 `cleanup_task_artifacts` 调用删掉、或者函数直接 `return 0`，
    这条照样绿 —— "锚还在"就成了"什么都没干"的同义词。

    把 `_release_ref(task_id, ...)` 加回 `_worktree.cleanup_task_artifacts` ⇒ 红。
    """
    from singularity.scheduler import config
    from singularity.scheduler._worktree import cleanup_task_artifacts, _anchor_ref

    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    _anchor_ref("T1", sha, repo_root=repo)
    assert _ref_exists(repo, "T1")

    patch = config.PATCH_DIR / "T1.md"          # 临时残留，**应该**被清掉
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text("patch", encoding="utf-8")

    cleanup_task_artifacts("T1", repo)

    assert not patch.exists(), "临时残留没清掉 —— 那这条测试就证明不了锚是**特意**留下的"
    assert _ref_exists(repo, "T1"), "清临时残留顺手把可打捞产物的锚剪了（2026-09-18 那个事故）"


def test_删任务时产物换桩_不丢(tmp_path, monkeypatch):
    """删任务改挂桩：`pending/` 拆掉、`salvaged/` 接上、**对象还捞得到**。

    🔵 **2026-09-20 加的**。原来是直接 `_release_ref`（产物真丢，09-18 实测掉过 3 个），
    取舍写着"用户要留就先别删任务"—— 可界面上从没说过这句话，用户也没有"先留"的动作。
    改法的完整理由在 `_worktree._salvage_ref` 的 docstring 里。

    ⚠️ 三条断言缺一不可：只验"pending 没了"的话，**把整个换桩删掉也绿**；
    只验"salvaged 在"的话，换了桩却没拆旧绳（任务读不到、界面照旧说有"可打捞产物"）也绿。
    """
    import json

    from singularity.scheduler import _api_tasks as A
    from singularity.scheduler import config
    from singularity.scheduler._worktree import _anchor_ref

    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    _anchor_ref("T1", sha, repo_root=repo)

    tasks_dir = tmp_path / "tasks"; tasks_dir.mkdir()
    (tasks_dir / "T1.json").write_text("{}", encoding="utf-8")
    qidian = tmp_path / ".qidian"
    # 🔴 账本别写进真 `.qidian/` —— 跑测试往真账上写字，以后没人分得清这行是谁写的
    monkeypatch.setattr(config, "QIDIAN_DIR", qidian)
    for name in ("CANCEL_DIR", "PAUSE_DIR", "PARKED_DIR", "HOLD_DIR", "TRACE_DIR"):
        monkeypatch.setattr(config, name, qidian / name.lower())
    monkeypatch.setattr(A, "_cleanup_task_artifacts", lambda *a, **k: 0)
    monkeypatch.setattr(A, "tracker", _FakeTracker(tasks_dir))
    monkeypatch.setattr("singularity.scheduler.project.repo_root_for", lambda t: repo)

    body, code = A.task_delete("T1")

    assert code == 200
    assert not _ref_exists(repo, "T1"), "旧绳没拆 —— 已删的任务还挂着'有可打捞的产物'"
    assert _salvaged_sha(repo, "T1") == sha, "产物没换到 salvaged 桩上"
    assert _obj_reachable(repo, sha), "换了桩却捞不到对象 —— 那这次修法等于没修"
    assert body["salvaged"] == sha, f"返回值没把这件事说出来: {body}"
    rows = [json.loads(x) for x in (qidian / "salvaged.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and rows[-1]["task_id"] == "T1" and rows[-1]["sha"] == sha, rows


def test_换桩建不出来时_旧绳一个字都不许动(tmp_path):
    """**先建新桩、再拆旧桩** —— 反过来中间挂了就两边都没有 = 纯丢。

    故障注入用真 git：往 `refs/qidian/salvaged/T1/` 底下挂一条子 ref，
    那个目录就占住了 `salvaged/T1` 这个名字，`update-ref` 必然失败。
    """
    from singularity.scheduler._worktree import _anchor_ref, _salvage_ref

    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    _anchor_ref("T1", sha, repo_root=repo)
    subprocess.run(["git", "update-ref", "refs/qidian/salvaged/T1/x", sha],
                   cwd=str(repo), capture_output=True, text=True)

    assert _salvage_ref("T1", repo_root=repo) == "", "新桩建不出来时不该说自己搬成了"
    assert _ref_exists(repo, "T1"), "新桩没建成，旧绳却被剪了 —— 产物纯丢"


def test_搬不动时_删任务不许走释放(tmp_path, monkeypatch):
    """**搬不成 ⇒ 旧绳原样留着**，不许"顺手释放"兜底。

    🔵 **2026-09-20 反过来了** —— 这条原来是 `test_删除任务仍然释放锚`，
    钉的是相反的行为。原来那句话（"删任务 = 明说要丢，锚留着是界面看不见的孤儿"）
    现在不成立了：孤儿有 `_api_tasks.orphan_refs()` 报得出来，**而产物丢了就真没了**。

    变异：在 `task_delete` 里给搬不动那条加一句 `_release_ref(...)` 兜底 ⇒ 本条红。
    """
    from singularity.scheduler import _api_tasks as A

    from singularity.scheduler._worktree import _anchor_ref

    repo = _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    _anchor_ref("T1", sha, repo_root=repo)

    tasks_dir = tmp_path / "tasks"; tasks_dir.mkdir()
    (tasks_dir / "T1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(A, "_salvage_ref", lambda tid, repo_root=None: "")   # 搬不动
    monkeypatch.setattr(A, "_cleanup_task_artifacts", lambda *a, **k: 0)
    monkeypatch.setattr(A, "tracker", _FakeTracker(tasks_dir))
    monkeypatch.setattr("singularity.scheduler.project.repo_root_for", lambda t: repo)

    A.task_delete("T1")
    assert _ref_exists(repo, "T1"), "搬不动就顺手剪了 —— 产物纯丢"
    assert _obj_reachable(repo, sha), "锚还在，对象却捞不到了"


def test_重试不释放锚(tmp_path, monkeypatch):
    """**重试 ≠ 产物已进仓**。

    🔵 **2026-09-19 反过来了** —— 这条原来是 `test_重试仍然释放锚`，钉的是相反的行为。

    原来的理由：「重试是"取代" ⇒ 旧锚已被取代 ⇒ 留着界面会说'有可打捞的产物'，
    指的却是上一版」。**它和本文件头的原则是矛盾的** —— 头里刚说完
    「**merged 是唯一**让'产物已经安全进项目仓了'这句话成立的分支」，
    而重试那一刻什么都没进仓。

    为什么反过来：
      · 「被取代」在点下重试那一瞬是**假设、不是事实** —— 要真跑起来、真产出，
        旧锚才真的被取代。而 `_anchor_ref` 用 `git update-ref ref <sha>`
        **无条件覆盖** ⇒ **新尝试一旦产出，旧锚自己就被盖掉，不必提前松手。**
      · ⇒ 提前释放只在「**重试没跑成**」时产生差别（调度循环没开 / 预算耗尽 /
        被取消 / 重启）—— 那时旧产物**既没被取代、又被释放**，纯丢。
        而"产物在、只是没进仓"恰恰是本仓最常见的形态（09-18 一天 62 次被 240s 掐断）。
      · 原来那句"界面会指上一版"**不是误报** —— 产物确实还在，那正是
        `salvageable_refs` 的定义。要消除歧义该改**标签**，不是删产物。

    ⚠️ 状态必须是 FAILED —— 别的一律早退 400，那这条测试就变成"什么都没验"。
    """
    from singularity.scheduler import _api_tasks as A

    released = []
    # `raising=False`（2026-09-20 加）：`_api_tasks` 里现在**没有** `_release_ref` 这个名字了
    # （task_delete 改成换桩之后连 import 都撤了）。这里仍然**注入**一个同名假货 ——
    # 真代码按模块全局名找它，所以哪天有人把这个名字加回 `task_retry`，这条照样当场红。
    monkeypatch.setattr(A, "_release_ref",
                        lambda tid, repo_root=None: released.append(tid), raising=False)
    monkeypatch.setattr(A, "_cleanup_task_artifacts", lambda *a, **k: 0)
    monkeypatch.setattr(A, "_supersede_trace", lambda tid: None)
    monkeypatch.setattr(A, "tracker",
                        _FakeTracker(tmp_path, status=tracker.TaskStatus.FAILED))
    monkeypatch.setattr("singularity.scheduler.project.repo_root_for", lambda t: tmp_path)

    A.task_retry("T1")
    assert released == [], (
        f"重试把锚释放了 —— 那是在断言「产物已进仓」这句假话。"
        f"重试一旦没跑成，旧产物既没被取代、又被释放，纯丢: {released}")


# ═══════════════════════════════════════════════════════════════
# ② 合并队列：只有 merged 那条能松手
# ═══════════════════════════════════════════════════════════════

class _MR:
    def __init__(self, task_id, status="merged"):
        self.task_id = task_id
        self.status = status
        self.new_head = "abcdef123456"
        self.conflict_files = []
        self.reason = ""


class _MQ:
    def __init__(self, results):
        self._results = results

    def drain(self):
        return self._results


class _Task:
    def __init__(self, tid, status=None):
        self.id = tid
        self.status = status or tracker.TaskStatus.RUNNING
        self.project_id = ""
        self.route_level = "any"
        self.description = "t"
        self.depends_on = []
        self.children = []


class _FakeTracker:
    """`task_delete`/`task_retry` 的桩。

    ⚠️ 别只给"看起来用得到的那几下" —— 真代码在释放之后还要一路走下去
    （删任务要遍历 `tasks_dir()` 反引用清理），少一个属性就是 AttributeError
    伪装成的失败，跟被测语义毫无关系。
    """

    TaskStatus = tracker.TaskStatus

    def __init__(self, tasks_dir=None, status=None):
        self._dir = tasks_dir
        self._status = status

    def read_task(self, tid):
        return _Task(tid, self._status)

    def tasks_dir(self):
        return self._dir

    def set_children(self, *a, **k):
        return None

    def transition(self, *a, **k):
        return None


def _drain(monkeypatch, tmp_path, mr):
    """把 `_drain_pending` 跑到"释放那一句"——其余重活全换成哑的。"""
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(orch, "tracker", _FakeTracker())
    monkeypatch.setattr(orch, "_maybe_complete_parents", lambda *a: None)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_strand_guard", lambda *a, **k: None)
    monkeypatch.setattr("singularity.scheduler.project.repo_root_for", lambda t: tmp_path)

    released = []
    monkeypatch.setattr(orch, "_release_ref", lambda tid, repo_root=None: released.append(tid))
    t = _Task(mr.task_id)
    orch._drain_pending({mr.task_id: (t, None, None, _Batch())}, _MQ([mr]), [])
    return released


class _Batch:
    validation = None
    dispatch_result = None
    pre_search_skipped = False
    pre_search_reason = ""
    pre_search_top_decisions = None
    pre_search_memory = None
    tool_events = None


def test_drain_合并成功才释放锚(monkeypatch, tmp_path):
    """`merged` = 产物真进了项目仓 ⇒ 松手。（这句本来就有，挪位置别挪丢了。）"""
    assert _drain(monkeypatch, tmp_path, _MR("T1", "merged")) == ["T1"]


def test_drain_冲突不许释放锚(monkeypatch, tmp_path):
    """`conflict` = **没合进去**。产物只在工作树/锚上 ⇒ 松手就是真丢。

    把释放那句挪回 `if/elif/else` 外面 ⇒ 红（2026-09-18 之前就是这个形状）。
    """
    assert _drain(monkeypatch, tmp_path, _MR("T1", "conflict")) == []


def test_drain_合并失败不许释放锚(monkeypatch, tmp_path):
    """`failed` 同理。"""
    assert _drain(monkeypatch, tmp_path, _MR("T1", "failed")) == []


# ═══════════════════════════════════════════════════════════════
# ③ 异常收尾：超时 / worker 异常那两条路
# ═══════════════════════════════════════════════════════════════

def test_异常收尾不释放锚(monkeypatch, tmp_path):
    """`_account_salvaged`（超时 + worker 异常共用）**不许**松手。

    这两条是"活干了一半"最多的地方 —— 09-18 那天 62 次被 240s 硬顶掐断全走这儿，
    而 `_salvage_timed_out` 刚把"改了哪些文件、提交了哪个 commit"记进 trace，
    下一句就把唯一拴着那份产物的绳子剪了：账上写着"产物在"，盘上却没人引用。

    把 `_release_ref` 加回 `_account_salvaged` 末尾 ⇒ 红。
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    monkeypatch.setattr(tracker.config, "QIDIAN_DIR", tmp_path)

    released = []
    monkeypatch.setattr(orch, "_release_ref", lambda tid, repo_root=None: released.append(tid))
    monkeypatch.setattr(orch, "_best_effort", lambda *a, **k: None)

    class _ER:
        token_count = 0
        changed_files = []
        raw_output = ""

    class _Salvaged:
        executor_result = _ER()
        agent_cfg = {}

    orch._account_salvaged(_Task("T1"), _Salvaged(), 12.0)
    assert released == [], f"异常收尾又松手了: {released}"
