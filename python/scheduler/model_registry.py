"""model_registry.py — 模型能力注册表。

读取 models.toml，提供按层级/能力/价格的查询接口。
与 api_store 联动: 查询时自动过滤 API 不可用的模型。
"""

from __future__ import annotations
import json
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

    def to_dict(self) -> dict:
        return {
            "id": self.id, "provider": self.provider, "display": self.display,
            "tiers": self.tiers, "speed": self.speed, "cost": self.cost,
            "reasoning": self.reasoning, "max_turns": self.max_turns,
            "strengths": self.strengths, "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelEntry":
        return cls(
            id=d.get("id",""), provider=d.get("provider",""), display=d.get("display",""),
            tiers=d.get("tiers",[]), speed=d.get("speed","medium"), cost=d.get("cost","standard"),
            reasoning=d.get("reasoning",False), max_turns=d.get("max_turns",5),
            strengths=d.get("strengths",[]), notes=d.get("notes",""),
        )


def _models_toml_path() -> Path:
    return config.SCHEDULER_DIR / "models.toml"


def _custom_path() -> Path:
    return config.QIDIAN_DIR / "models_custom.json"


def _load_custom() -> dict[str, ModelEntry]:
    """加载用户自定义的模型 (JSON overlay)。"""
    path = _custom_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {k: ModelEntry.from_dict(v) for k, v in data.items()}
    except (json.JSONDecodeError, KeyError):
        return {}


def _save_custom(models: dict[str, ModelEntry]) -> None:
    config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    data = {k: v.to_dict() for k, v in models.items()}
    _custom_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_models() -> dict[str, ModelEntry]:
    """加载所有模型 — TOML 内置 + JSON 自定义覆盖。"""
    models = {}
    # 1. 内置 TOML
    path = _models_toml_path()
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        items = raw.get("models", [])
        if isinstance(items, list):
            for data in items:
                mid = data.get("id", "")
                if mid:
                    models[mid] = _entry_from_raw(mid, data)
        elif isinstance(items, dict):
            for mid, data in items.items():
                if isinstance(data, dict):
                    models[mid] = _entry_from_raw(mid, data)
    # 2. 用户自定义覆盖 / 新增
    custom = _load_custom()
    models.update(custom)
    return models


def _entry_from_raw(mid: str, data: dict) -> ModelEntry:
    return ModelEntry(
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


# ── CRUD (写入自定义 JSON) ──

def add_model(model_id: str, provider: str, display: str = "",
              tiers: list[str] = None, speed: str = "medium",
              cost: str = "standard", reasoning: bool = False,
              max_turns: int = 5, notes: str = "") -> ModelEntry:
    """添加或更新自定义模型。"""
    custom = _load_custom()
    entry = ModelEntry(
        id=model_id, provider=provider,
        display=display or model_id,
        tiers=tiers or ["E"],
        speed=speed, cost=cost, reasoning=reasoning,
        max_turns=max_turns, notes=notes,
    )
    custom[model_id] = entry
    _save_custom(custom)
    return entry


def remove_model(model_id: str) -> bool:
    """删除自定义模型 (只能删自定义的，不能删内置的)。"""
    custom = _load_custom()
    if model_id not in custom:
        return False
    del custom[model_id]
    _save_custom(custom)
    return True


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
