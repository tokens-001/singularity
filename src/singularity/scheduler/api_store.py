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

from singularity.scheduler import config, witness
from singularity.scheduler._io import atomic_write_json


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


def _load_raw() -> dict:
    """读 api_store.json 原始 dict（含 _observer 等元数据键）。不存在返回 {}。"""
    path = _store_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def _load() -> dict[str, APIEntry]:
    """读 API 库。不存在则用内置种子数据初始化。跳过 _ 前缀元数据键。"""
    path = _store_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return {k: APIEntry.from_dict(v) for k, v in data.items()
                    if not k.startswith("_") and isinstance(v, dict)}
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
        for level in ["any"]:
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
            witness.warn("api_store", f"discovery:{e}"[:80])
        except Exception as e:
            witness.warn('api_store', f'{e}')
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
        return "deepseek"
    if "bigmodel" in url_lower or "zhipu" in url_lower:
        return "zhipu"
    if "moonshot" in url_lower or "kimi" in url_lower:
        return "kimi"
    if "anthropic" in url_lower:
        return "anthropic"
    if "dashscope" in url_lower or "qwen" in url_lower:
        return "dashscope_api_key"
    # fallback: guess from env var name
    if "DEEPSEEK" in env_key:
        return "deepseek"
    if "ZHIPU" in env_key or "GLM" in env_key:
        return "zhipu"
    if "KIMI" in env_key or "MOONSHOT" in env_key:
        return "kimi"
    if "DASHSCOPE" in env_key or "QWEN" in env_key:
        return "dashscope_api_key"
    return env_key.lower()


def _provider_id(provider: str) -> str:
    return provider.lower()


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
    # 保留元数据键（如 _observer），避免被 API 增删改覆盖丢失
    for k, v in _load_raw().items():
        if k.startswith("_") and k not in data:
            data[k] = v
    p = _store_path()
    atomic_write_json(p, data)


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
    # 同步清理该 API 关联的扫描模型 (否则删了 API 模型还在, 不同步)
    custom = load_custom_models()
    dropped = [mid for mid, m in custom.items() if m.get("provider") == api_id]
    if dropped:
        for mid in dropped:
            del custom[mid]
        _custom_models_path().write_text(json.dumps(custom, ensure_ascii=False, indent=2))
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


# 被标记的 provider 多久之后允许再试一次。
# 没有这个就是"有去无回"：一次 429/欠费把它永久摘出池子，而
#   - 前端没接 setStatus 入口，CLI 也没有 → 只能手敲 curl
#   - 自动恢复也没戏：mark 之后就没人再问它了
# 实测线上：智谱被一次 http429（body 含"余额不足"）标成 quota_exhausted 后一直没恢复。
_RECOVERY_COOLDOWN = 30 * 60      # 30 分钟


def is_available(api_id: str) -> bool:
    """检查 API 是否可用 (active 且有 key)。

    非 active 的 provider 过了冷却期后**放行一次**（半开，与 _model_breaker 同思路）：
    撞上真欠费会被 note_api_error 重新标记、冷却重新计时；已经充值或只是被偶发限流
    误伤的，则自然回到池子里。
    """
    entry = get(api_id)
    if not entry:
        return False
    if entry.status == "disabled":
        return False                      # 人工显式关闭 → 不自动放行，别覆盖用户意图
    if entry.status != "active":
        marked_at = getattr(entry, "updated_at", 0) or 0
        if (time.time() - marked_at) < _RECOVERY_COOLDOWN:
            return False
    return bool(os.environ.get(entry.api_key_env, ""))


# 余额不足的特征。各厂商标法不一：DeepSeek/Kimi 是 402 + "Insufficient Balance"，
# 智谱是 400 带 1113 / "余额不足"，OpenAI 用 "insufficient_quota"。
_QUOTA_HINTS = ("insufficient balance", "insufficient_quota", "exceeded_current_quota",
                "quota exceeded", "余额不足", "欠费", "arrears", "please recharge")


_QUOTA_DEAD_KEY = "_quota_dead"   # {model_id: 标记时间戳}
_ALIAS_KEY = "_aliases"           # {请求名: 实际模型名}


def _quota_dead() -> dict:
    return _load_raw().get(_QUOTA_DEAD_KEY, {}) or {}


def record_alias(requested: str, actual: str) -> None:
    """记下「请求名 → 实际模型」的映射。

    厂商会把旧模型名路由到新模型：DeepSeek 2026-09-14 12:00 起，`deepseek-v4-pro`
    的请求**全部路由到 V4.1-Flash** —— 请求名不变，但返回体里的 `model` 字段变了。
    而委员会是按**请求名**选席位的：两个名字指向同一个模型时，看起来是"多视角碰撞"，
    实际是同一个模型自己跟自己碰，唯一验证过的核心价值直接归零。

    只有**实际调用**才知道真相（`/v1/models` 只列主推名），所以对账放在响应路径上。
    """
    if not requested or not actual or requested == actual:
        return
    try:
        data = _load_raw()
        aliases = data.setdefault(_ALIAS_KEY, {})
        if aliases.get(requested) == actual:
            return
        aliases[requested] = actual
        _store_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass   # 记不上不该影响调用本身


def canonical(model: str) -> str:
    """把别名归一到实际模型名（跟着链走，防 A→B→C）。没记过就原样返回。"""
    try:
        aliases = _load_raw().get(_ALIAS_KEY, {}) or {}
    except Exception:
        return model
    seen, cur = set(), model
    while cur in aliases and cur not in seen:
        seen.add(cur)
        cur = aliases[cur]
    return cur


def is_model_available(model: str) -> bool:
    """**模型级**可用性 —— 比 `is_available(provider)` 精确。

    provider 级熔断在"同一账号既有免费模型又有付费模型"时会误伤：一个付费模型欠费，
    把同厂商下还能用的模型一起踢出候选链（智谱就是这样 —— glm-5.3 欠费，
    glm-4.7/4-flash 明明能调，也一起消失）。而且踢掉是**静默的**：模型从链上没了，
    没有任何提示，表现就是"怎么又少了"。

    判据：① 这个模型自己欠费过吗 ② provider 还开着吗（没禁用、配了 key）
    —— 刻意**不看** provider 的 status，那正是会连坐的那一项。
    """
    ts = _quota_dead().get(model, 0)
    if ts and (time.time() - ts) < _RECOVERY_COOLDOWN:
        return False
    try:
        from singularity.scheduler import model_registry as mr
        provider = mr.provider_for_model(model)
    except Exception:
        provider = ""
    if not provider:
        return True
    entry = get(provider)
    if entry is None or entry.status == "disabled":
        return False
    return bool(os.environ.get(entry.api_key_env, ""))


def note_api_error(model: str, status_code: int, body: str = "") -> str:
    """模型调用失败时调一次。命中余额特征 → 标记 provider 为 quota_exhausted + 告警。

    返回命中的 api_id（没命中返回 ""）。意义：欠费不再只是"委员会静默少一席"——
    标成 quota_exhausted 后 is_available 会跳过它，下一轮调度不再撞同一堵墙。
    """
    low = (body or "").lower()
    if status_code != 402 and not any(h in low for h in _QUOTA_HINTS):
        return ""
    try:
        from singularity.scheduler import model_registry as mr
        api_id = mr.provider_for_model(model)
    except Exception:
        api_id = ""
    witness.warn("api_store",
                 f"quota_exhausted:{api_id or model}:http{status_code}:{(body or '')[:80]}"[:200])
    # 欠费记到**具体模型**上 —— 调度用它决定跳不跳（见 is_model_available）。
    # provider 状态仍然更新，但那只给界面看，不再作为调度的连坐依据。
    if model:
        try:
            data = _load_raw()
            data.setdefault(_QUOTA_DEAD_KEY, {})[model] = time.time()
            _store_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            witness.warn("api_store", f"mark_quota_dead_failed:{model}:{e}"[:200])
    if api_id:
        cur = get(api_id)
        if cur and cur.status != "quota_exhausted":
            set_status(api_id, "quota_exhausted",
                       notes=f"自动标记 {time.strftime('%Y-%m-%d %H:%M')}: http{status_code}")
    return api_id


def get_observer_model() -> str:
    """观察者用的模型 id（api_store.json 的 _observer 键，值为模型 id）。"""
    return _load_raw().get("_observer", "")


def set_observer_model(model_id: str) -> None:
    """设置观察者模型。空串 = 清除。"""
    data = _load_raw()
    if model_id:
        data["_observer"] = model_id
    else:
        data.pop("_observer", None)
    _store_path().write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _is_major_model(model_id: str) -> bool:
    """判断是否为当前代主力模型（缩小扫描结果到可管理的范围）。"""
    import re
    m = model_id.lower()
    # Well-known current-gen model patterns
    patterns = [
        r'^gpt-\d+', r'^o\d+',  # OpenAI: gpt-4/5/6..., o3/o4/o5...
        r'^claude-opus', r'^claude-sonnet', r'^claude-haiku',  # Anthropic (名字不带代际)
        r'^deepseek-v\d+', r'^deepseek-r1$',  # DeepSeek: v3/v4/v5...
        # DeepSeek 也有不带代际的名字(deepseek-flash / -pro / -chat / -reasoner)。
        # 只认 `v\d+` 时它们既不在能力快照里、也匹配不上模式 → 扫描时被静默丢掉。
        # deepseek-flash 就是这么从列表里消失的(同 Anthropic 那行"名字不带代际")。
        r'^deepseek-(flash|pro|chat|reasoner)',
        r'^glm-\d+',  # Zhipu: glm-4/5/6...
        r'^kimi-k\d+',  # Kimi: k2/k3/k4...
        r'^qwen3\.\d+-(max|plus)$', r'^qwen3-coder', r'^qwen-(max|plus|turbo|coder)',  # Qwen major
        r'^qwen3\.\d+-\d+b',  # Qwen 3.X with explicit params
    ]
    for p in patterns:
        if re.match(p, m):
            return True
    return False


def _infer_model_provider(model_id: str, fallback: str = "") -> str:
    """从模型 ID 推断真实 provider（处理模型网关代理多家的情况）。"""
    m = model_id.lower().replace("_", "").replace("/", "")
    if "qwen" in m: return "dashscope_api_key"
    if "glm" in m or "zhipu" in m: return "zhipu"
    if "kimi" in m or "moonshot" in m: return "kimi"
    if "deepseek" in m or "vanchin" in m: return "deepseek"
    if "siliconflow" in m: return "siliconflow"
    if "gpt" in m or "openai" in m or model_id.startswith("o") and model_id[1:].isdigit(): return "openai_api_key"
    if "claude" in m or "anthropic" in m: return "anthropic"
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
            dropped: list[tuple[str, str]] = []   # (模型 id, 为什么滤掉)
            for item in items:
                mid = item.get("id", "")
                # Filter: only keep text generation models suitable for agent use
                skip_prefixes = ("o1-", "test-", "sre-", "vanchin/", "_", "siliconflow/", "MiniMax", "ZHIPU/")
                skip_keywords = ("audio", "embedding", "tts", "dall-e", "whisper", "moderation",
                                "livetranslate", "ocr", "image", "asr", "fun-asr", "auto-handle",
                                "distill", "-preview", "-thinking", "-vl-", "-omni-", "-omi-",
                                "speech", "mt-", "math-", "xvq", "qvq", "qwq", "gui-",
                                "-0.5b", "-1.5b", "-1.8b", "-7b-chat", "-14b-chat",
                                "qwen1.5-", "qwen2-", "qwen-mt", "tongyi-",
                                "-longcontext", "-long", "-flash-character", "-s2s-",
                                "-realtime", "-deep-research", "-deep-search", "-1201", "-0107", "-0919", "-latest")
                skip = any(mid.startswith(p) for p in skip_prefixes) or any(k in mid.lower() for k in skip_keywords)
                if mid and skip:
                    dropped.append((mid, "skip"))
                if mid and not skip:
                    # 从已知模型库查能力数据
                    cap = known.get(mid) if isinstance(known, dict) else None
                    # Only show: known models OR major/current-generation models
                    if not cap and not _is_major_model(mid):
                        dropped.append((mid, "not_major"))
                        continue
                    # 推断真实 provider（处理模型网关情况, 如 DashScope 代理多家模型）
                    provider = _infer_model_provider(mid, entry.provider)
                    models.append({
                        "id": mid,
                        "display": item.get("id", mid),
                        "provider": provider,
                        # 自动兜底: 快照里没有的新模型, 默认推荐到定义/实现(低风险阶段), rating 仍诚实标 ?
                        "recommended_for": cap.recommended_for if cap else ["定义", "实现"],
                        "rating": cap.rating if cap else "?",
                        "speed": cap.speed if cap else "medium",
                        "cost": cap.cost if cap else "standard",
                        "strengths": cap.strengths if cap else [],
                        "notes": cap.notes if cap else "",
                        "known": cap is not None,
                    })
            # Dedup 1: remove dated snapshots if base version exists
            # Dedup 2: remove prefixed dups like 'kimi/kimi-k2.7-code' if 'kimi-k2.7-code' exists
            import re
            model_ids = {m["id"] for m in models}
            date_pattern = re.compile(r"-\d{4}-\d{2}-\d{2}$")
            filtered = []
            for m in models:
                mid = m["id"]
                # Dedup 1: dated snapshot whose base version exists → skip
                m2 = date_pattern.search(mid)
                if m2:
                    base = mid[:m2.start()]
                    if base in model_ids:
                        continue
                # Dedup 2: 'provider/model' where 'model' also exists → skip
                if "/" in mid:
                    short = mid.split("/")[-1]
                    if short in model_ids and short != mid:
                        continue
                filtered.append(m)
            # 落选账: 白名单对"厂商的命名"是开放集合, 永远补不全 ——
            # deepseek-flash 就是这么凭空少掉的, 用户只能自己发现"怎么只有一个"。
            # 聚合一条写进 alerts.jsonl, 下次漏了先看这儿, 不用再猜。
            if dropped:
                try:
                    from singularity.scheduler import witness
                    not_major = [m for m, r in dropped if r == "not_major"]
                    skip_n = len(dropped) - len(not_major)
                    preview = ", ".join(f"{m}({r})" for m, r in dropped[:6])
                    witness.warn("model_scan",
                                 f"{api_id}: 滤掉 {len(dropped)} 个 (skip {skip_n} / not_major {len(not_major)}): {preview}"[:200])
                    # not_major 才是要看的那一类: 它们像正经对话模型, 但白名单不认
                    if not_major:
                        witness.warn("model_scan",
                                     f"{api_id}: not_major 逐个 = {', '.join(not_major[:10])}"[:200])
                except Exception:
                    pass
            return filtered
    except Exception:
        # 网络/解析失败不再静默返回空, 抛给上层报出真实原因 (api_store_scan 会捕获返回 500)
        raise


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
        return ["any"]  # 两档后统一 any
    if any(k in mid for k in ["sonnet", "gpt-4", "k2", "flash"]):
        return ["any"]
    return ["any"]


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

