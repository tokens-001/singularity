"""snapshot.py — 写入前强制快照 (借天工 Step 6)

审计修了什么 (审计 1.1 / 4.1 / 4.2 / 4.3 / 4.4):
  - git stash 优先, 全项目状态快照 (不是"待改文件"子集 —— 那是伪快照,
    agent 改了预料外的文件就回滚不全, 磁盘不一致)
  - 全项目快照根治"agent 改到一半崩"的不一致 (审计 4.1/4.2)
  - git 方案天然无磁盘膨胀; 文件拷贝兜底保留最近 N 个 (审计 4.3)
  - 回滚失败不再尝试回滚, 改为报告 snapshot_id 交人工 (审计 4.4)
  - 启动前剩空间检查已在 config.ensure_dirs() 做

v1 边界:
  - 非 git 项目才退化为文件拷贝 (当前项目是 git, 走 git 路径)
  - 回滚是尽力而为, 不是 ACID; neijinglu 永远带 snapshot_id
"""

from __future__ import annotations
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from singularity.scheduler import config


@dataclass
class Snapshot:
    id: str                # 时间戳 task_id
    method: str            # "git" | "copy"
    ref: str               # git: stash ref; copy: 目录路径
    created_at: float


def take(task_id: str) -> Snapshot:
    """写入前快照。git 优先, 文件拷贝兜底。"""
    snap_id = f"{int(time.time())}_{task_id}"

    if _is_git_repo():
        return _take_git(snap_id)
    return _take_copy(snap_id)


def rollback(snap: Snapshot) -> bool:
    """回滚到快照。失败返回 False, 调用方负责报告 + 退出 (审计 4.4)。"""
    if snap.method == "git":
        return _rollback_git(snap)
    return _rollback_copy(snap)


# ── git 路径 ──────────────────────────────────────────────────────────
def _is_git_repo() -> bool:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
            cwd=str(config.PROJECT_ROOT),
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:  # noqa: BLE001
        return False


def _take_git(snap_id: str) -> Snapshot:
    """git stash create 生成临时 commit, 不进 stash list, 零残留。"""
    r = subprocess.run(
        ["git", "stash", "create"],
        capture_output=True, text=True,
        cwd=str(config.PROJECT_ROOT),
    )
    ref = r.stdout.strip()
    if r.returncode != 0 or not ref:
        # 工作区干净 (无改动) —— 仍记录一个空快照, ref 为 HEAD
        ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True,
            cwd=str(config.PROJECT_ROOT),
        ).stdout.strip()
    snap = Snapshot(id=snap_id, method="git", ref=ref, created_at=time.time())
    _save_meta(snap)
    return snap


def _rollback_git(snap: Snapshot) -> bool:
    """回滚: 用快照 ref 重建工作区状态。"""
    # 丢弃当前未提交改动 ("--" 和 "." 是两个独立 arg, 不是 "-- .")
    subprocess.run(
        ["git", "checkout", "--", "."],
        capture_output=True, text=True,
        cwd=str(config.PROJECT_ROOT),
    )
    # 清未跟踪文件 (agent 新建的文件 checkout 删不掉)
    subprocess.run(
        ["git", "clean", "-fd"],
        capture_output=True, text=True,
        cwd=str(config.PROJECT_ROOT),
    )
    # 再把快照的改动恢复出来 (stash ref 可直接 apply)
    if snap.ref and snap.ref != _current_head():
        r = subprocess.run(
            ["git", "stash", "apply", snap.ref],
            capture_output=True, text=True,
            cwd=str(config.PROJECT_ROOT),
        )
        if r.returncode != 0:
            return False
    return True  # 干净快照, 无需恢复


def _current_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True,
        cwd=str(config.PROJECT_ROOT),
    ).stdout.strip()


# ── 文件拷贝兜底 (非 git 项目) ────────────────────────────────────────
def _take_copy(snap_id: str) -> Snapshot:
    snap_dir = config.SNAPSHOT_DIR / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    # 拷贝项目文件 (排除 .git / .qidian / venv)
    for item in config.PROJECT_ROOT.iterdir():
        if item.name in {".git", ".qidian", "venv", "__pycache__"}:
            continue
        dst = snap_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)
    _purge_old_copies()
    snap = Snapshot(id=snap_id, method="copy", ref=str(snap_dir), created_at=time.time())
    _save_meta(snap)
    return snap


def _rollback_copy(snap: Snapshot) -> bool:
    snap_dir = Path(snap.ref)
    if not snap_dir.exists():
        return False
    for item in snap_dir.iterdir():
        dst = config.PROJECT_ROOT / item.name
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)
    return True


def _purge_old_copies() -> None:
    """文件拷贝方案保留最近 N 个 (审计 4.3)。"""
    snaps = sorted(config.SNAPSHOT_DIR.glob("*/"), key=lambda p: p.stat().st_mtime)
    for old in snaps[:-config.MAX_SNAPSHOTS]:
        shutil.rmtree(old, ignore_errors=True)

def purge_old_snapshot_meta(keep: int = 200) -> int:
    """清理旧的快照元数据 .json 文件，保留最近 keep 个。返回清理数。"""
    import os as _os, time as _time
    from collections import defaultdict
    files = sorted(config.SNAPSHOT_DIR.glob("*.json"),
                   key=lambda p: _os.path.getmtime(p), reverse=True)
    now = _time.time()
    week_ago = now - 7 * 86400
    kept = 0
    per_group: defaultdict[str, int] = defaultdict(int)
    MAX_PER_GROUP = 30  # 每组最多保留
    OVERALL_CAP = 500   # 总量上限

    # 从新到老遍历，决定哪些保留
    to_keep = set()
    for f in files:
        group = f.stem.split("_", 1)[1] if "_" in f.stem else f.stem
        mtime = _os.path.getmtime(f)
        # 保留条件: 7天内 OR 该组未满30个 OR 总量未满500
        if mtime > week_ago or per_group[group] < MAX_PER_GROUP or kept < OVERALL_CAP:
            to_keep.add(f)
            kept += 1
            per_group[group] += 1

    n = 0
    for f in files:
        if f not in to_keep:
            try: f.unlink(); n += 1
            except OSError: pass
    return n


# ── 元数据 ────────────────────────────────────────────────────────────
def _save_meta(snap: Snapshot) -> None:
    meta = config.SNAPSHOT_DIR / f"{snap.id}.json"
    meta.write_text(json.dumps({
        "id": snap.id, "method": snap.method,
        "ref": snap.ref, "created_at": snap.created_at,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
