"""模型范围纪律表：谁写的方案"超出需求范围"少，融合时就让谁定稿。

`execution_judge._pick_writer` 读这张表选**定稿人**，而实测**定稿人是乘法器还是
过滤器直接决定产物的范围纪律** —— 同一批稿子同一需求，glm 当定稿人时把两家的
超范围内容都收进来（ledger 23 + RabbitMQ 7），deepseek 当定稿人时连自己的
RabbitMQ 都砍了（ledger 1 + RabbitMQ 1）。所以这张表不能是旧的。

**原来只有 `tests/integration/coverage_audit.py`（人手动跑的离线实验脚本）会写它**
—— 后果是表常年停在那儿：2026-09-12 实测停在 9/10，而且**缺了当前主力模型**
（`deepseek-flash` 压根不在表里 → `_pick_writer` 直接走回退规则）。

## 一条铁律

**量不出来就不记。** 没有"声明范围"这个尺子时记 0，等于编造"这次很干净" ——
那张表会越用越假，而它还决定选谁定稿。**宁可表小，不可表假。**

（同族：防御模式 §32「静默兜底 = 编造数字」。）
"""
from __future__ import annotations

import json
import os
import threading
import time

from singularity.scheduler import config as sched_config
from singularity.scheduler._io import atomic_write_json

_LOCK = threading.Lock()      # 并发任务会同时记 —— 读-改-写必须串起来（§13/#46）


def _path():
    """**读时现算**，不冻在模块级（§34：冻住的路径会写进生产）。"""
    return sched_config.QIDIAN_DIR / "model_discipline.json"


def load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def record(model: str, violations: int) -> bool:
    """给某个模型累加**一次审计**和它的违例数。返回 True = 记了。

    原子写 + 锁：并发任务同时记时，读-改-写不串起来会互相覆盖（§13）。

    ⚠️ `violations` 必须是**真数出来的**。量不出来别调这个函数 —— 见模块头铁律。
    """
    model = (model or "").strip()
    if not model or violations is None or violations < 0:
        return False
    try:
        with _LOCK:
            disc = load()
            d = disc.setdefault(model, {"violations": 0, "audits": 0, "last_ts": 0})
            d["violations"] = int(d.get("violations", 0)) + int(violations)
            d["audits"] = int(d.get("audits", 0)) + 1
            d["last_ts"] = time.time()
            _path().parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(_path(), disc)
        return True
    except Exception:
        return False       # 记账失败不能把任务带崩


def record_scope(model: str, changed: list[str], declared: list[str]) -> bool:
    """按"改了声明范围外的文件"记一次范围违例。**量不出来返回 False，不记。**

    declared 为空 = **没有尺子**（架构师没给 `estimated_files`）→ 不记，别编。
    实测探路2 的架构任务全都没给这个字段 —— 所以这条路的覆盖率取决于架构师，
    框架层面只能说清"为什么没记"，不能假装记了。
    """
    if not model or not declared:
        return False
    declared_norm = {os.path.normpath(str(p)).strip() for p in declared if str(p).strip()}
    if not declared_norm:
        return False
    changed_norm = {os.path.normpath(str(p)).strip() for p in (changed or []) if str(p).strip()}
    # 只管**改了的不在声明里**（少改不算违例：声明是上限不是配额）
    extra = {p for p in changed_norm - declared_norm
             if p and not p.endswith(".pyc") and "__pycache__" not in p}
    return record(model, len(extra))


def stats() -> dict:
    """给排障看的：表里有什么、每条纪律分多少。"""
    disc = load()
    out = {}
    for m, d in disc.items():
        audits = max(int(d.get("audits", 0) or 0), 1)
        out[m] = {"violations": d.get("violations", 0), "audits": d.get("audits", 0),
                  "per_audit": round(d.get("violations", 0) / audits, 2)}
    return out
