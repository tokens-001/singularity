"""api_store.py — API Key 库。

独立于 agent 配置，管理 API 的生命周期: 可用/欠费/限流/停用。
模型→API 的映射由 model_registry 管，这里只管 key 的状态。

持久化: .qidian/api_store.json
"""

from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from singularity.scheduler import config


@dataclass
class APIEntry:
    id: str                      # deepseek | zhipu | kimi | anthropic
    provider: str                # DeepSeek | 智谱 | Moonshot | Anthropic
    base_url: str                # https://api.deepseek.com/v1
    api_key_env: str             # 环境变量名，不存明文 key
    status: str = "active"       # active | quota_exhausted | rate_limited | disabled
    notes: str = ""              # 备注: "充了 65，省着用"
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "provider": self.provider,
            "base_url": self.base_url, "api_key_env": self.api_key_env,
            "status": self.status, "notes": self.notes,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "APIEntry":
        return cls(**{k: d.get(k, "" if k not in ("created_at", "updated_at") else 0.0)
                       for k in ["id", "provider", "base_url", "api_key_env", "status", "notes",
                                  "created_at", "updated_at"]})


def _store_path() -> Path:
    config.QIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    return config.QIDIAN_DIR / "api_store.json"


def _load() -> dict[str, APIEntry]:
    """读 API 库。不存在则用内置种子数据初始化。"""
    path = _store_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return {k: APIEntry.from_dict(v) for k, v in data.items()}
        except (json.JSONDecodeError, KeyError):
            pass
    return _seed()


def _seed() -> dict[str, APIEntry]:
    """首次运行: 从 agents.toml 和已知环境变量探活，自动建库。"""
    entries = {}
    now = time.time()

    # 扫描 agents.toml 收集所有 API
    try:
        from .dispatcher import load_agents
        agents = load_agents()
        seen = set()
        for level in ["E", "E+", "D"]:
            for a in agents.get(level, []):
                env_key = a.get("api_key_env", "")
                if not env_key or env_key in seen:
                    continue
                seen.add(env_key)
                # 根据 URL 推断 provider
                entry_url = a.get("entry", "")
                provider = _guess_provider(entry_url, env_key)
                api_id = _provider_id(provider)
                has_key = bool(os.environ.get(env_key, ""))
                entries[api_id] = APIEntry(
                    id=api_id, provider=provider,
                    base_url=_guess_base_url(entry_url),
                    api_key_env=env_key,
                    status="active" if has_key else "disabled",
                    notes="自动发现" if has_key else "自动发现 — 需要配置 key",
                    created_at=now, updated_at=now,
                )
    except Exception as e:
        try:
            from . import witness
            witness.heartbeat("api_store", f"warn:discovery:{e}"[:80])
        except Exception as e:
            witness.heartbeat('api_store', f'warn:{e}')
        pass

    # 补充已知但 agents.toml 里没配的 (如 Anthropic via Claude CLI)
    if "anthropic" not in entries:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
        entries["anthropic"] = APIEntry(
            id="anthropic", provider="Anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key_env="ANTHROPIC_API_KEY",
            status="active" if has_key else "disabled",
            notes="Claude Code CLI 使用" if has_key else "Claude Code CLI — 需要配置 key",
            created_at=now, updated_at=now,
        )

    _save(entries)
    return entries


def _guess_provider(entry_url: str, env_key: str) -> str:
    url_lower = entry_url.lower()
    if "deepseek" in url_lower:
        return "DeepSeek"
    if "bigmodel" in url_lower or "zhipu" in url_lower:
        return "智谱"
    if "moonshot" in url_lower or "kimi" in url_lower:
        return "Moonshot"
    if "anthropic" in url_lower:
        return "Anthropic"
    # fallback: guess from env var name
    if "DEEPSEEK" in env_key:
        return "DeepSeek"
    if "ZHIPU" in env_key or "GLM" in env_key:
        return "智谱"
    if "KIMI" in env_key or "MOONSHOT" in env_key:
        return "Moonshot"
    return env_key


def _provider_id(provider: str) -> str:
    return {"DeepSeek": "deepseek", "智谱": "zhipu", "Moonshot": "kimi",
            "Anthropic": "anthropic"}.get(provider, provider.lower())


def _guess_base_url(entry_url: str) -> str:
    """从 entry URL 提取 base URL: .../chat/completions → .../"""
    if not entry_url or entry_url.startswith("/"):
        return ""
    # strip /chat/completions or similar
    for suffix in ["/chat/completions", "/v1/chat/completions", "/v1/responses"]:
        if entry_url.endswith(suffix):
            return entry_url[:-len(suffix)]
    return entry_url.rsplit("/", 1)[0]


def _save(entries: dict[str, APIEntry]) -> None:
    data = {k: v.to_dict() for k, v in entries.items()}
    _store_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── CRUD ──

def list_all() -> dict[str, APIEntry]:
    """列出所有 API，含当前状态。"""
    return _load()


def get(api_id: str) -> Optional[APIEntry]:
    return _load().get(api_id)


def add(api_id: str, provider: str, base_url: str, api_key_env: str,
        notes: str = "") -> APIEntry:
    entries = _load()
    now = time.time()
    has_key = bool(os.environ.get(api_key_env, ""))
    entry = APIEntry(
        id=api_id, provider=provider, base_url=base_url,
        api_key_env=api_key_env,
        status="active" if has_key else "disabled",
        notes=notes, created_at=now, updated_at=now,
    )
    entries[api_id] = entry
    _save(entries)
    return entry


def remove(api_id: str) -> bool:
    entries = _load()
    if api_id not in entries:
        return False
    del entries[api_id]
    _save(entries)
    return True


def set_status(api_id: str, status: str, notes: str = "") -> Optional[APIEntry]:
    """更新 API 状态: active | quota_exhausted | rate_limited | disabled"""
    entries = _load()
    entry = entries.get(api_id)
    if not entry:
        return None
    entry.status = status
    entry.updated_at = time.time()
    if notes:
        entry.notes = notes
    _save(entries)
    return entry


def is_available(api_id: str) -> bool:
    """检查 API 是否可用 (active 且有 key)。"""
    entry = get(api_id)
    if not entry or entry.status != "active":
        return False
    return bool(os.environ.get(entry.api_key_env, ""))


def available_apis() -> list[APIEntry]:
    """返回所有当前可用的 API。"""
    return [e for e in _load().values() if is_available(e.id)]


def _infer_model_provider(model_id: str, fallback: str = "") -> str:
    """从模型 ID 推断真实 provider（处理模型网关代理多家的情况）。"""
    m = model_id.lower().replace("_", "").replace("/", "")
    if "qwen" in m: return "阿里通义千问"
    if "glm" in m or "zhipu" in m: return "智谱"
    if "kimi" in m or "moonshot" in m: return "Moonshot/Kimi"
    if "deepseek" in m or "vanchin" in m: return "DeepSeek"
    if "siliconflow" in m: return "SiliconFlow"
    if "gpt" in m or "openai" in m or model_id.startswith("o") and model_id[1:].isdigit(): return "OpenAI"
    if "claude" in m or "anthropic" in m: return "Anthropic"
    return fallback


def scan_models(api_id: str, include_capabilities: bool = True) -> list[dict]:
    """扫描 API 厂商的 /models 接口，返回可用模型列表。

    返回: [{"id": "model-name", "display": "...", "provider": "...",
            "rating": "?", "speed": "?", "cost": "?", "strengths": [], "notes": ""}, ...]
    """
    import httpx
    entry = get(api_id)
    if not entry or not entry.base_url:
        return []
    api_key = os.environ.get(entry.api_key_env, "")
    if not api_key:
        return []

    # 从 base_url 推导 models 接口
    base = entry.base_url.rstrip("/")
    for suffix in ["/chat/completions", "/v1/chat/completions", "/responses"]:
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    models_url = base.rstrip("/") + "/models"

    # 加载已知模型能力数据
    known = {}
    if include_capabilities:
        try:
            from singularity.scheduler.model_registry import load_models
            known = load_models()
        except Exception:
            pass

    try:
        with httpx.Client(timeout=httpx.Timeout(15)) as client:
            r = client.get(models_url, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            })
            if r.status_code != 200:
                return []
            data = r.json()
            items = data.get("data", [])
            if not items and isinstance(data, list):
                items = data
            models = []
            for item in items:
                mid = item.get("id", "")
                # Filter: exclude non-text models + test/dev garbage
                skip_prefixes = ("o1-", "test-", "sre-", "vanchin/", "_")
                skip_keywords = ("audio", "embedding", "tts", "dall-e", "whisper", "moderation",
                                "livetranslate", "ocr", "image", "asr", "fun-asr", "auto-handle")
                skip = any(mid.startswith(p) for p in skip_prefixes) or any(k in mid.lower() for k in skip_keywords)
                if mid and not skip:
                    # 从已知模型库查能力数据
                    cap = known.get(mid) if isinstance(known, dict) else None
                    # 推断真实 provider（处理模型网关情况, 如 DashScope 代理多家模型）
                    provider = _infer_model_provider(mid, entry.provider)
                    models.append({
                        "id": mid,
                        "display": item.get("id", mid),
                        "provider": provider,
                        "rating": cap.rating if cap else "?",
                        "speed": cap.speed if cap else "medium",
                        "cost": cap.cost if cap else "standard",
                        "strengths": cap.strengths if cap else [],
                        "notes": cap.notes if cap else "",
                        "known": cap is not None,
                    })
            return models
    except Exception:
        return []


# ═══════════════════════════════════════════
# 自定义模型扩展 (扫描发现后存入)
# ═══════════════════════════════════════════

def _custom_models_path():
    from . import config
    return config.QIDIAN_DIR / "models_custom.json"


def load_custom_models() -> dict:
    """加载自动发现的自定义模型。"""
    p = _custom_models_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_custom_model(model_id: str, provider: str, display: str = "",
                      tiers: list[str] = None, speed: str = "", cost: str = "",
                      rating: str = "", strengths: list[str] = None, notes: str = "") -> dict:
    """保存一个扫描发现的模型到自定义注册表。已有条目保留原有字段。"""
    custom = load_custom_models()
    existing = custom.get(model_id, {})
    # ── 类型安全防护 ──
    def _safe_str(v, default=""): return v if isinstance(v, str) and v else default
    def _safe_bool(v, default=False): return bool(v) if not isinstance(v, bool) else v
    def _safe_int(v, default=5): return v if isinstance(v, int) else default
    def _safe_list(v, default=None): return v if isinstance(v, list) else (default or [])
    custom[model_id] = {
        "id": model_id,
        "provider": provider,
        "display": display or _safe_str(existing.get("display")) or model_id,
        "tiers": tiers or _safe_list(existing.get("tiers")) or _guess_tiers(model_id),
        "speed": speed or _safe_str(existing.get("speed"), "medium"),
        "cost": cost or _safe_str(existing.get("cost"), _guess_cost(model_id)),
        "rating": _safe_str(rating) or _safe_str(existing.get("rating")),
        "reasoning": _safe_bool(existing.get("reasoning")),
        "max_turns": _safe_int(existing.get("max_turns")),
        "strengths": strengths if strengths is not None else _safe_list(existing.get("strengths")),
        "notes": notes or _safe_str(existing.get("notes")),
    }
    _custom_models_path().parent.mkdir(parents=True, exist_ok=True)
    _custom_models_path().write_text(json.dumps(custom, ensure_ascii=False, indent=2))
    return custom[model_id]


def _guess_tiers(model_id: str) -> list[str]:
    """根据模型名猜测适合的层级。"""
    mid = model_id.lower()
    if any(k in mid for k in ["opus", "gpt-5", "pro", "ultra", "o3", "o4"]):
        return ["D"]
    if any(k in mid for k in ["sonnet", "gpt-4", "k2", "flash"]):
        return ["E+", "E"]
    return ["E"]


def _guess_cost(model_id: str) -> str:
    mid = model_id.lower()
    if any(k in mid for k in ["opus", "pro", "ultra", "o3", "o4"]):
        return "premium"
    if any(k in mid for k in ["gpt-5", "sonnet", "k2"]):
        return "standard"
    return "budget"


# ═══════════════════════════════════════════
# Provider 健康探测
# ═══════════════════════════════════════════

def probe(api_id: str = "") -> dict:
    """探测 API provider 健康状态。对每个 active provider ping 一次。

    429/503 → 自动标记 rate_limited/disabled。
    返回 {provider_id: {"status": "ok"|"error", "latency_ms": ...}}
    """
    import httpx, time as _time
    entries = list_all() if not api_id else {api_id: get(api_id)}
    results = {}
    for eid, entry in entries.items():
        if not entry or entry.status != "active":
            continue
        if not os.environ.get(entry.api_key_env):
            results[eid] = {"status": "error", "reason": "api_key not set"}
            continue
        try:
            t0 = _time.time()
            # 尝试 GET /models 端点（轻量探测）
            base = entry.base_url.rstrip("/")
            resp = httpx.get(f"{base}/models", timeout=15)
            latency = int((_time.time() - t0) * 1000)
            if resp.status_code in (429, 503):
                set_status(eid, "rate_limited" if resp.status_code == 429 else "disabled")
                results[eid] = {"status": "error", "code": resp.status_code, "latency_ms": latency,
                                "action": "auto-marked rate_limited" if resp.status_code == 429 else "auto-marked disabled"}
            elif resp.status_code < 500:
                results[eid] = {"status": "ok", "latency_ms": latency}
            else:
                results[eid] = {"status": "error", "code": resp.status_code, "latency_ms": latency}
        except Exception as e:
            results[eid] = {"status": "error", "reason": str(e)[:120]}
    return results
