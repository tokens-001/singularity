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

from . import config


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
    except Exception:
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
