"""model_registry.py — 模型能力注册表。

读取 models.toml，提供按层级/能力/价格的查询接口。
与 api_store 联动: 查询时自动过滤 API 不可用的模型。
"""

from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config


@dataclass
class ModelEntry:
    id: str                      # deepseek-v4-flash
    provider: str                # deepseek (对应 api_store)
    display: str                 # DeepSeek V4 Flash
    tiers: list[str]             # ["E", "E+", "D"]
    speed: str                   # fast | medium | slow
    cost: str                    # budget | standard | premium
    reasoning: bool = False      # 推理模型 (用 reasoning_content)
    max_turns: int = 5
    strengths: list[str] = field(default_factory=list)
    notes: str = ""


def _models_toml_path() -> Path:
    return config.SCHEDULER_DIR / "models.toml"


def load_models() -> dict[str, ModelEntry]:
    """加载所有模型。支持 [[models]] 数组和 [models.xxx] 表两种格式。"""
    path = _models_toml_path()
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    models = {}
    items = raw.get("models", [])
    # [[models]] 数组格式
    if isinstance(items, list):
        for data in items:
            mid = data.get("id", "")
            if not mid:
                continue
            models[mid] = ModelEntry(
                id=mid,
                provider=data.get("provider", ""),
                display=data.get("display", mid),
                tiers=data.get("tiers", []),
                speed=data.get("speed", "medium"),
                cost=data.get("cost", "standard"),
                reasoning=data.get("reasoning", False),
                max_turns=data.get("max_turns", 5),
                strengths=data.get("strengths", []),
                notes=data.get("notes", ""),
            )
    # [models.xxx] 表格式 (向后兼容)
    elif isinstance(items, dict):
        for mid, data in items.items():
            if not isinstance(data, dict):
                continue
            models[mid] = ModelEntry(
                id=mid,
                provider=data.get("provider", ""),
                display=data.get("display", mid),
                tiers=data.get("tiers", []),
                speed=data.get("speed", "medium"),
                cost=data.get("cost", "standard"),
                reasoning=data.get("reasoning", False),
                max_turns=data.get("max_turns", 5),
                strengths=data.get("strengths", []),
                notes=data.get("notes", ""),
            )
    return models


def for_tier(tier: str, available_only: bool = True) -> list[ModelEntry]:
    """获取某一层可用的所有模型。

    tier: "E" | "E+" | "D"
    available_only: True → 只返回 API 可用的模型
    """
    from . import api_store
    models = load_models()
    result = []
    for m in models.values():
        if tier in m.tiers:
            if available_only and not api_store.is_available(m.provider):
                continue
            result.append(m)
    # 按成本排序: budget → standard → premium
    cost_order = {"budget": 0, "standard": 1, "premium": 2}
    result.sort(key=lambda m: cost_order.get(m.cost, 1))
    return result


def get(model_id: str) -> Optional[ModelEntry]:
    return load_models().get(model_id)


def provider_for_model(model_id: str) -> str:
    """模型→API provider id。用于 api_store 查状态。"""
    m = get(model_id)
    return m.provider if m else ""


def models_for_provider(provider: str) -> list[ModelEntry]:
    """某个 API 旗下的所有模型。"""
    return [m for m in load_models().values() if m.provider == provider]


def fallback_for_tier(tier: str, exclude_providers: set[str] = None) -> list[ModelEntry]:
    """获取某层的容灾备选链。

    按成本排序，排除指定 provider（如已欠费的）。
    返回全部可用模型的排序列表，供 dispatcher 建立 fallback 链。
    """
    from . import api_store
    exclude = exclude_providers or set()
    models = load_models()
    result = []
    for m in models.values():
        if tier not in m.tiers:
            continue
        if m.provider in exclude:
            continue
        if not api_store.is_available(m.provider):
            continue
        result.append(m)
    cost_order = {"budget": 0, "standard": 1, "premium": 2}
    result.sort(key=lambda m: cost_order.get(m.cost, 1))
    return result
