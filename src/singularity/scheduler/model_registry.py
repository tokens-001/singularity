"""model_registry.py — 模型权威目录。

读取 models.toml，提供按推荐阶段/能力/价格的查询接口。
与 api_store 联动: 查询时自动过滤 API 不可用的模型。

两档后: tiers → recommended_for (权威推荐, 不强制路由)。
"""

from __future__ import annotations
import json
from singularity.scheduler._io import load_toml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from singularity.scheduler import config


@dataclass
class ModelEntry:
    id: str                      # deepseek-v4-flash
    provider: str                # deepseek (对应 api_store)
    display: str                 # DeepSeek V4 Flash
    recommended_for: list[str] = field(default_factory=list)  # 权威推荐: ["定义","实现","交付"] 等
    speed: str = "medium"        # fast | medium | slow
    cost: str = "standard"       # budget | standard | premium
    rating: str = ""             # SSS/SS/S/A 评级
    reasoning: bool = False      # 推理模型 (用 reasoning_content)
    max_turns: int = 5
    strengths: list[str] = field(default_factory=list)
    notes: str = ""

    # backward compat
    @property
    def tiers(self) -> list[str]:
        return self.recommended_for

    def to_dict(self) -> dict:
        return {
            "id": self.id, "provider": self.provider, "display": self.display,
            "recommended_for": self.recommended_for, "speed": self.speed, "cost": self.cost,
            "rating": self.rating, "reasoning": self.reasoning,
            "max_turns": self.max_turns, "strengths": self.strengths,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelEntry":
        # backward compat: read old "tiers" key or new "recommended_for"
        rf = _safe_list(d.get("recommended_for") or d.get("tiers"))
        return cls(
            id=_safe_str(d.get("id")), provider=_safe_str(d.get("provider")), display=_safe_str(d.get("display"), _safe_str(d.get("id")) or ""),
            recommended_for=rf, speed=_safe_str(d.get("speed"),"medium"), cost=_safe_str(d.get("cost"),"standard"),
            rating=_safe_str(d.get("rating")), reasoning=_safe_bool(d.get("reasoning")),
            max_turns=_safe_int(d.get("max_turns")), strengths=_safe_list(d.get("strengths")),
            notes=_safe_str(d.get("notes")),
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
        raw = load_toml(path)
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
    # 2. 用户自定义覆盖 / 新增 (空 tiers 视为已删除, 不显示)
    custom = _load_custom()
    for mid, m in custom.items():
        if m.recommended_for:  # 有推荐 → 覆盖/新增
            if mid in models:
                toml = models[mid]
                if not m.rating: m.rating = toml.rating
                if not m.provider: m.provider = toml.provider
                if not m.display or m.display == mid: m.display = toml.display
                if not m.strengths: m.strengths = toml.strengths
                if not m.notes: m.notes = toml.notes
            models[mid] = m
        elif mid in models:  # 空推荐 → 删除标记
            del models[mid]
    return models


def _safe_str(v, default=""):
    return v if isinstance(v, str) else default

def _safe_bool(v, default=False):
    return v if isinstance(v, bool) else default

def _safe_int(v, default=5):
    return v if isinstance(v, int) else default

def _safe_list(v, default=None):
    return v if isinstance(v, list) else (default or [])

def _entry_from_raw(mid: str, data: dict) -> ModelEntry:
    return ModelEntry(
        id=mid,
        provider=_safe_str(data.get("provider")),
        display=_safe_str(data.get("display"), mid),
        recommended_for=_safe_list(data.get("recommended_for") or data.get("tiers")),
        speed=_safe_str(data.get("speed"), "medium"),
        cost=_safe_str(data.get("cost"), "standard"),
        rating=_safe_str(data.get("rating")),
        reasoning=_safe_bool(data.get("reasoning")),
        max_turns=_safe_int(data.get("max_turns")),
        strengths=_safe_list(data.get("strengths")),
        notes=_safe_str(data.get("notes")),
    )


# ── CRUD (写入自定义 JSON) ──

def add_model(model_id: str, provider: str, display: str = "",
              recommended_for: list[str] = None, speed: str = "medium",
              cost: str = "standard", rating: str = "",
              reasoning: bool = False, max_turns: int = 5,
              notes: str = "") -> ModelEntry:
    """添加或更新自定义模型。推荐阶段为空=不限制。"""
    custom = _load_custom()
    entry = ModelEntry(
        id=model_id, provider=provider,
        display=display or model_id,
        recommended_for=recommended_for or [],
        speed=speed, cost=cost, rating=rating,
        reasoning=reasoning, max_turns=max_turns, notes=notes,
    )
    custom[model_id] = entry
    _save_custom(custom)
    return entry


def remove_model(model_id: str) -> bool:
    """删除模型：自定义的直接删，内置的标记 disabled 隐藏。"""
    custom = _load_custom()
    if model_id in custom:
        del custom[model_id]
        _save_custom(custom)
        return True
    # 内置模型：在 custom 中标记 disabled
    all_models = load_models()
    if model_id in all_models:
        m = all_models[model_id]
        custom[model_id] = ModelEntry(
            id=model_id, provider=m.provider,
            display=m.display,
            recommended_for=[], speed=m.speed, cost=m.cost,
            rating=m.rating, reasoning=m.reasoning,
            max_turns=m.max_turns,
            strengths=m.strengths, notes=m.notes,
        )
        _save_custom(custom)
        return True
    return False


def for_phase(phase: str = "", available_only: bool = True) -> list[ModelEntry]:
    """获取某阶段推荐的模型。phase 为空时返回全部。

    phase: "定义" | "架构" | "实现" | "审查" | "验收" | "交付" 或留空
    """
    from . import api_store
    models = load_models()
    result = []
    for m in models.values():
        if phase and phase not in m.recommended_for:
            continue
        if available_only and not api_store.is_available(m.provider):
            continue
        result.append(m)
    cost_order = {"budget": 0, "standard": 1, "premium": 2}
    result.sort(key=lambda m: cost_order.get(m.cost, 1))
    return result


# backward compat
def for_tier(tier: str = "", available_only: bool = True) -> list[ModelEntry]:
    return for_phase(tier, available_only)


def get(model_id: str) -> Optional[ModelEntry]:
    return load_models().get(model_id)


def provider_for_model(model_id: str) -> str:
    """模型→API provider id。用于 api_store 查状态。"""
    m = get(model_id)
    return m.provider if m else ""


def models_for_provider(provider: str) -> list[ModelEntry]:
    """某个 API 旗下的所有模型。"""
    return [m for m in load_models().values() if m.provider == provider]


def fallback_for_tier(phase: str = "", exclude_providers: set[str] = None) -> list[ModelEntry]:
    """获取容灾备选链。phase 为空=全池。

    按成本排序，排除指定 provider。
    """
    from . import api_store
    exclude = exclude_providers or set()
    models = load_models()
    result = []
    for m in models.values():
        if phase and phase not in m.recommended_for:
            continue
        if m.provider in exclude:
            continue
        if not api_store.is_available(m.provider):
            continue
        result.append(m)
    cost_order = {"budget": 0, "standard": 1, "premium": 2}
    result.sort(key=lambda m: cost_order.get(m.cost, 1))
    return result
