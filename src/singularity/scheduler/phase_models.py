"""phase_models.py — 全局「阶段 → 模型」。

智能体页面上那个「激活」开关，看起来只管"哪些模型可用 + 思考强度"，实际身兼三职：
候选链（取第 1 个当主力）、**架构委员会名单（全池）**、审查员池（全池减写手取前 2）。
用户的原话是「怎么和架构扯上关系了」—— 界面上确实看不出来。

这个模块把两者拆开：激活池回答"谁能用"，这里回答"哪个阶段用谁"。

存 ``.qidian/phase_models.json``，**直接是 map，没有 wrapper 键**（对齐 ``phases.json``）::

    {"planning": ["glm-5.3", "kimi-k3"], "extract": ["glm-5.2"]}

**没配置 = 回退到当前行为。** 这是本模块最重要的性质：文件不存在时，全仓行为与加这个
模块之前逐字节一致 —— 包括 route_learner 重排、按 strengths 提链首、委员会拿全池。
"""

from __future__ import annotations

import json
import logging

from singularity.scheduler import config
from singularity.scheduler._io import atomic_write_json

#: 可配置的阶段。key 与 Phase 枚举值对齐（extract 例外：它是融合提取，不在 Phase 里）。
PHASES = ("researching", "planning", "executing", "reviewing", "extract")

#: 各阶段的语义 + 界面提示。顺序 = 界面上从上到下的顺序。
PHASE_HINTS: dict[str, str] = {
    "researching": "第 1 个 = 主力，其余兜底",
    "planning": "全部 = 委员会席位（只选 1 个等于关掉多模型碰撞）",
    "executing": "第 1 个 = 主力，其余兜底",
    "reviewing": "全部 = 审查员（写手会被自动剔除）",
    "extract": "第 1 个 = 融合提取员",
}


def _path():
    """**每次现算，不要在模块级缓存。**

    tests/conftest.py 的 `_isolate_qidian_dir` 是 monkeypatch `config.QIDIAN_DIR`，
    导入时算好的派生路径不跟着改 —— 缓存了就会把测试数据写进生产目录。
    """
    return config.QIDIAN_DIR / "phase_models.json"


def _clean(data) -> dict[str, list[str]]:
    """过滤 + 去重 + 保序。load 和 save 共用，保证"存进去什么就能读回什么"。

    丢弃脏条目（未知阶段 / 非 list / 非 str / 空串）而不是让它们传播：一个坏条目会让
    整个阶段静默用错模型，而这是**决定跑哪个模型**的地方。
    空列表不留键 —— 它等价于"没配"，留着反而让 save/load 往返对不上。
    """
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, val in data.items():
        if key not in PHASES or not isinstance(val, list):
            continue
        seen: set[str] = set()
        models: list[str] = []
        for m in val:
            if not isinstance(m, str):
                continue
            m = m.strip()
            if m and m not in seen:
                seen.add(m)
                models.append(m)
        if models:
            out[key] = models
    return out


def load() -> dict[str, list[str]]:
    """读全部阶段配置。文件缺失 / 损坏 / 结构不对一律返回空 dict，不抛。"""
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logging.getLogger(__name__).warning("phase_models load failed: %s", e)
        return {}
    return _clean(data)


def for_phase(phase: str) -> list[str]:
    """某阶段指定的模型。没配 → 空列表，调用方据此回退到旧行为。"""
    return list(load().get(str(phase), []))


def save(data: dict) -> None:
    """整份写入。空列表 = 删键，保证界面上清空某项就是"恢复默认"。"""
    atomic_write_json(_path(), _clean(data))


def purge_model(model_id: str) -> bool:
    """模型从模型库删除时，同步清掉它在阶段配置里的引用。返回是否真清到了。

    对齐 `dispatcher.purge_disabled`：留着的话，配置里有个既调不动（没有 agent）
    又看着像配过了的名字。
    """
    data = load()
    hit = False
    for models in data.values():
        if model_id in models:
            models.remove(model_id)
            hit = True
    if hit:
        save(data)          # _clean 会顺手把被清空的 key 删掉
    return hit


def selection(phase: str, project=None, level: str = "any") -> tuple[dict | None, bool]:
    """某阶段该用哪些模型。返回 ``(lineup, restrict_to_lineup)``。

    三态，顺序即优先级：

    1. **项目级 `agent_lineup` 非空** → ``(项目名单, False)``。不限制是**刻意的** ——
       它有自己的语义（"偏爱谁打头，其余仍作兜底"），改成限制会让已有项目静默丢掉兜底模型。
    2. **全局阶段配置非空** → ``(阶段名单, True)``。这是"真限制"：委员会席位、
       审查员名单都靠它，指定之外的模型不该再被追加进来。
    3. **都没有** → ``(None, False)``，调用方完全按旧行为走。
    """
    lineup = (getattr(project, "agent_lineup", None) or {}).get(level)
    if lineup:
        return {level: list(lineup)}, False
    models = for_phase(phase)
    if models:
        return {level: models}, True
    return None, False
