"""dispatcher.py — 读 agents.toml 选 executor 并调用。

v2: 集成 api_store + model_registry，API 欠费/限流时自动跳过对应 agent。
project_lineup 支持项目级自定义编组。
"""

from __future__ import annotations
import importlib
import logging
import threading
import time
from dataclasses import dataclass

from singularity.scheduler import config, witness
from singularity.scheduler.log import timed
from singularity.scheduler._types import _pending_sse_events
from singularity.scheduler.executors import (
    BaseExecutor, ExecutorResult,
    ClaudeCliExecutor, ZhipuApiExecutor, OpenAIAgentExecutor, AnthropicApiExecutor,
)

# 档位 → 下一档 的映射表。**故意为空**：两档制已合并成单档("any")，
# 没有"下一档"可升。`escalate()` 因此恒返回 None（详见它的 docstring）。
# 将来重加档位时在这里填表即可，调用方不用动。
_ESCALATION: dict[str, str] = {}


def _all_agents_list(agents: dict) -> list[dict]:
    """两档后: 从所有层级收集 agent (去重)。"""
    seen = set()
    result = []
    for level_agents in agents.values():
        for a in (level_agents if isinstance(level_agents, list) else []):
            m = a.get("model", "")
            if m and m not in seen:
                seen.add(m)
                result.append(a)
    return result


_EXECUTOR_BY_TYPE = {
    "claude-cli": ClaudeCliExecutor,
    "zhipu-api": ZhipuApiExecutor,
    "anthropic-api": AnthropicApiExecutor,
    "openai-agent": OpenAIAgentExecutor,
}

# ── Skill/MCP 缓存 (P2-4) ──────────────────────────────────
# dispatch() 在 worker 线程读, invalidate_* 从 Flask 请求线程调。
# 用一把 Lock 串行化 读/写/失效, 防并发 lost-update (同 tracker._LOCK 模式)。
_CACHE_LOCK = threading.Lock()
_SKILL_CACHE: dict[tuple[str, str], tuple[list, str, dict]] = {}  # (level,model) → (tools,prompt,skills)
_MCP_CACHE: tuple[list, object] | None = None  # (tools, executor_callable), None=未加载/已失效


@dataclass
class DispatchResult:
    level: str
    agent_cfg: dict
    executor_result: ExecutorResult
    attempts: int


def load_agents() -> dict:
    """加载 agent 配置: agents.toml (基础) + agents_custom.json (覆盖)。

    custom 文件中可包含:
      - 每层追加的 agent 配置 (add)
      - _disabled: [model_name, ...] — 从 toml 中禁用的模型
    """
    from ._io import load_toml
    data = load_toml(config.AGENTS_TOML)
    raw = data.get("agents", {})
    agents = {}
    for k, v in raw.items():
        key = k
        agents[key] = list(v)  # shallow copy

    # 合并自定义覆盖
    custom = _custom_agents()
    for k, cfgs in custom.items():
        if k.startswith("_"):
            continue
        level = k
        if level not in agents:
            agents[level] = []
        for c in cfgs:
            if isinstance(c, dict) and c.get("model"):
                # 避免重复
                existing = [a.get("model") for a in agents[level]]
                if c["model"] not in existing:
                    agents[level].append(c)

    # 应用禁用列表
    for level in agents:
        disabled = custom.get("_disabled", {}).get(level, [])
        if disabled:
            agents[level] = [a for a in agents[level] if a.get("model") not in disabled]

    return agents


def _ensure_agent_type(agent_cfg: dict) -> dict:
    """自动补全 agent 配置：缺 type/provider/api_key_env 时从 model_registry + api_store 填充。

    ponytail: agents_custom.json 可以只写 model，其余字段自动推断。
    """
    # ponytail: 先补全缺失字段
    model = agent_cfg.get("model", "")
    need_fill = not agent_cfg.get("type") or not agent_cfg.get("api_key_env")
    if need_fill and model:
        try:
            from . import model_registry as mr
            from . import api_store
            m = mr.get(model)
            if m:
                apis = api_store.list_all()
                api = apis.get(m.provider)
                if api:
                    if not agent_cfg.get("type"):
                        agent_cfg["type"] = "openai-agent"
                    if not agent_cfg.get("provider"):
                        agent_cfg["provider"] = m.provider
                    if not agent_cfg.get("api_key_env"):
                        agent_cfg["api_key_env"] = api.api_key_env
                    if not agent_cfg.get("entry"):
                        agent_cfg["entry"] = api.base_url + "/chat/completions"
        except Exception as _e:
            logging.getLogger(__name__).warning("agent config scan failed: %s", _e)

    # ponytail: 尊重 models.toml 每模型 max_turns(thinking 模型如 deepseek-v4-pro=3 轮),
    # 不再硬抬 ≥15——慢模型 15 轮×~150s 直接撞 orchestrator 900s deadline
    if model:
        try:
            from . import model_registry as mr
            m = mr.get(model)
            registry_max = getattr(m, 'max_turns', 10) or 10 if m else 10
        except Exception:
            registry_max = 10
        current_max = agent_cfg.get("max_turns", 0)
        agent_cfg["max_turns"] = current_max or registry_max or 8
    agent_cfg.setdefault("max_tool_turns", 3)
    agent_cfg.setdefault("request_template", {"model": model, "max_tokens": config.MODEL_MAX_TOKENS})
    return agent_cfg


def is_model_active(model: str) -> bool:
    """这个模型现在**允许被调**吗 —— 在不在激活池里。

    「激活池」= `load_agents()` 返回的那份（用户的启用/停用就是 `_disabled`，
    在 `load_agents` 里生效）。**读不出来时抛异常**，方向交给调用方定：
    派发侧 fail-open（池子读挂 ⇒ 整个池子空掉、一个任务都派不出去，
    比多跑一个模型严重），融合侧 fail-closed（见 `execution_judge._model_in_active_pool`）。

    ⚠️ **判据必须是"在不在池里"，不能只挡 `_disabled`**：模型注册表里有一批
    **从来没人启用过**的模型（kimi-k2.6 / qwen3.7-max / gpt-5.5 …，2026-09-18 数是 15 个）
    —— 它们没被停用，只是没配成 agent。只挡 `_disabled` 的话，
    `_expand_review_pool` 照样拿它们去审代码（**真花钱**）。

    ⚠️ **闸门只设这一处，且在所有选路的下游**：`pick_agent_fallback_chain`
    （含 cascade 换人、委员会席位）、`_expand_review_pool`、`multi_model_review`
    的 stub 分支 —— 全都过 `agent_api_available`，所以装在它里面一处就够。
    装错层是这个仓的老毛病：2026-09-18 00:35 我先把闸门装在 `execution_judge._call_model`
    （融合侧），而真在烧钱的**是派发侧这条**——`_expand_review_pool` 拿 `_disabled`
    里的 `glm-5.2` / `deepseek-v4-pro` 去审代码，每 20 来秒一轮，
    重启、测试全绿、变异也验过，**钱照烧**（00:37 分诊账还在进）。

    ⚠️ **池子空 ≠ 停用**：那是"根本没配 agent"（测试环境就是，`conftest` 把
    `QIDIAN_DIR` 指到 tmp 后池子恒空）⇒ 放行。混淆两者的代价实测过：
    `_model_in_active_pool` 加这条时**一次红了 7 个用例**。

    ⚠️ 这是"**别自动派活**"的闸门，不是"别调它"的闸门 —— 有一处**故意**不走这里：
    `_benchmark.run_benchmark`（`POST /api/models/<id>/benchmark`）**直接构造 executor**，
    因为那是**用户点名要测某个模型**，不是系统替他挑人。别顺手把它也堵上。
    """
    pool = _all_agents_list(load_agents())
    if not pool:
        return True
    return any(a.get("model") == model for a in pool)


def agent_api_available(agent_cfg: dict) -> bool:
    """检查 agent 的 API 是否可用。

    所有类型都经过 model_registry → api_store 检查。
    claude-cli 也检查 api_store 状态。

    硬限制：OpenAI 模型必须在 _order 里显式列出才会被选中，
    防止误烧 GPT 额度。
    """
    # 自动补全缺失的 type/provider
    agent_cfg = _ensure_agent_type(agent_cfg)
    model = agent_cfg.get("model", "")
    agent_type = agent_cfg.get("type", "")

    # 闸门：不在激活池 / 被用户停用 ⇒ 不可用。见 `is_model_active`。
    # ⚠️ 位置在 `_ensure_agent_type` **之后**是故意的 —— 那行会把 type/provider
    # 就地补进 cfg，调用方（如 `_expand_review_pool`）要读补出来的 `type`。
    if model:
        try:
            active = is_model_active(model)
        except Exception as e:      # noqa: BLE001
            # 查不了 ⇒ **放行**（fail-open，和融合侧相反，理由见 `is_model_active`）。
            # 但要出声：静默吞掉的话，"池子读不出来"和"这个模型没问题"长得一模一样。
            from . import witness
            witness.warn("dispatch", f"pool_check_failed:{model}:{type(e).__name__}"[:120],
                         key="pool_check_failed")
            active = True
        if not active:
            return False

    # ponytail: API 类 agent 必须有 entry 或 api_key_env, 否则无法调 API
    if agent_type in ("openai-agent", "zhipu-api"):
        entry = agent_cfg.get("entry", "")
        key_env = agent_cfg.get("api_key_env", "")
        if not entry and not key_env:
            return False  # 空壳 agent (前端添加但未配置)

    provider = ""
    if model:
        try:
            from . import model_registry as mr
            provider = mr.provider_for_model(model)
            if provider:
                from . import api_store
                # 模型级判据，不是 provider 级：provider 被标欠费时，同厂商下
                # 还能调的模型不该被连坐踢出（智谱付费模型欠费 → 免费/低价模型一起消失）。
                if not api_store.is_model_available(model):
                    return False
        except Exception as e:
            from . import witness; witness.warn("dispatch", f"api_check:{e}")

    # 硬限制：OpenAI 模型除非在 _order 显式列出或有显式配置，否则不可用
    if provider == "openai":
        custom = _custom_agents()
        all_ordered = []
        for tier_order in (custom.get("_order", {}) or {}).values():
            all_ordered.extend(tier_order)
        # ponytail: built-from-registry agents may have agent_cfg with provider set;
        # only reject if _order is defined AND model is not in it
        if all_ordered and model not in all_ordered:
            return False

    # claude-cli: api_store 通过了就算通过
    etype = agent_cfg.get("type", "")
    if etype == "claude-cli":
        return True

    env_key = agent_cfg.get("api_key_env", "")
    if env_key:
        import os
        return bool(os.environ.get(env_key, ""))
    return True


def _build_agent_from_registry(model_name: str) -> dict | None:
    """模型不在 agents.toml 时，从 model_registry + api_store 自动构造配置。"""
    try:
        from . import model_registry as mr
        from . import api_store
        m = mr.get(model_name)
        if not m:
            return None
        apis = api_store.list_all()
        api = apis.get(m.provider, {}) if hasattr(apis, 'get') else {}
        return {
            "model": model_name,
            "type": "openai-agent",
            "entry": getattr(api, "base_url", "") + "/chat/completions" if hasattr(api, "base_url") else "",
            "api_key_env": getattr(api, "api_key_env", ""),
            "max_turns": m.max_turns,
            "default": False,
            "roles": ["generic"],
            "sandbox": "worktree",
            "request_template": {"model": model_name, "max_tokens": config.MODEL_MAX_TOKENS},
        }
    except Exception as e:
        from . import witness; witness.warn("dispatch", f"build_agent:{e}")
        return None


def _find_agent_by_model(agents: dict, model_name: str) -> dict | None:
    """跨所有层搜索 agent 配置。"""
    for level_cfgs in agents.values():
        for a in level_cfgs:
            if a.get("model") == model_name:
                return a
    return None




def pick_agent_fallback_chain(agents: dict, level: str,
                               exclude: set = None,
                               project_lineup: dict[str, list[str]] = None,
                               fallback_levels: list[str] = None,
                               restrict_to_lineup: bool = False) -> list[dict]:
    """返回该层可用 agent 列表。project_lineup > 阵容顺序。

    API 不可用的自动跳过。
    若目标层无可用 agent，尝试 fallback_levels 列表。

    ``restrict_to_lineup=True`` 时 lineup **就是全部候选**：不再追加同层其余模型，
    也不做路由学习器重排。给「阶段 → 模型」用 —— 委员会席位和审查员名单靠它才
    限制得住。默认 False = 旧语义（lineup 只是"优先"，其余仍作兜底），别改。

    这里原来还有个 `role` 参数（"按角色挑模型"，读 agent 的 `roles` 字段）。
    2026-09-11 删掉：全仓**没有任何调用方传它**，那条分支从写下那天起就没跑过。
    「谁来做」现在是 `phase_models`（阶段 → 模型）管的，不再走角色。
    """
    restricted = False          # 本次是否真的按"只留 lineup"返回了

    def _collect(tier: str):
        nonlocal restricted
        cands = agents.get(tier, [])
        if not cands:
            return []
        excl = exclude or set()
        res = []; s = set()
        lineup = (project_lineup or {}).get(tier, [])
        if lineup:
            for mn in lineup:
                found = None
                for a in cands:
                    k = a.get("model","")
                    if k == mn and k not in s and k not in excl and agent_api_available(a):
                        found = a; break
                if not found:
                    cross = _find_agent_by_model(agents, mn)
                    if cross and agent_api_available(cross): found = cross
                if found:
                    res.append(found); s.add(found.get("model",""))
            if restrict_to_lineup:
                if res:
                    restricted = True
                    return res          # 只留指定席位，其余一律不追加
                # 指定的一个都解析不出来（被禁用 / 欠费 / 名字过期）→ fail-open 回全池。
                # 否则配错一个名字就让整个阶段没模型可跑，比"多跑几个"严重得多。
                # 但必须留痕，不然界面上配了东西却完全没生效，查都没处查。
                witness.warn("dispatcher",
                             f"lineup_all_unavailable:{tier}:{','.join(lineup)}"[:120])
        # 同层 agent 平等, 不区分 default 优先级
        for a in cands:
            k = a.get("model","")
            if k not in s and k not in excl and agent_api_available(a):
                res.append(a); s.add(k)
        return res

    # 两档后: level 为空时从全池收集
    if level:
        result = _collect(level)
    else:
        result = []
        excl = exclude or set()
        seen = set()
        for a in _all_agents_list(agents):
            k = a.get("model", "")
            if k not in seen and k not in excl and agent_api_available(a):
                result.append(a)
                seen.add(k)
    if not result and fallback_levels:
        for fl in fallback_levels:
            result = _collect(fl)
            if result:
                break
    # ponytail: dedup across tiers (same model may appear in multiple levels)
    seen = set()
    deduped = []
    for a in result:
        k = a.get("model", "")
        if k not in seen:
            deduped.append(a); seen.add(k)

    # ── 路由学习者权重排序 ──
    # 按模型在所有任务类型下的平均 Hedge 权重降序,
    # 权重>1=近期成功多, <1=近期失败多, 1=冷启动
    # **受限时跳过**：用户点名"第 1 个当主力"之后，再按历史权重重排就是语义撒谎 ——
    # 界面上点的是 A，实际调的是学习器挑的 B（deepseek-v4-flash 的历史权重 2.12，
    # 只要它在链上就会被顶到最前）。
    if len(deduped) > 1 and not restricted:
        try:
            from singularity.scheduler.route_learner import load_learner
            learner = load_learner()
            if learner and learner._stats:
                model_weights: dict[str, float] = {}
                for stat in learner._stats.values():
                    prev = model_weights.get(stat.model, 1.0)
                    # avg across task_types for this model
                    model_weights[stat.model] = (prev + stat.hedge_weight) / 2
                if model_weights:
                    deduped.sort(
                        key=lambda a: model_weights.get(a.get("model", ""), 1.0),
                        reverse=True,
                    )
        except Exception as _e:
            logging.getLogger(__name__).warning("route learner sort failed: %s", _e)  # learner 挂了不阻塞选择

    # 注：这里曾经加过"轮换"（每次换个模型打头），已撤掉。
    # 理由：`any` 层那个列表**本质是"主力 + 备用"** —— 原设计就是永远用第一个、
    # 挂了才用下一个。轮换会让同一项目里调研用一个模型、架构用另一个、审查又换回来，
    # 出了问题不好归因，成本和效果也忽高忽低；而且两个模型的样本都变少，学习器收敛更慢。
    # 想验证"哪些模型配了但没跑过"，看用量页就够了（未使用 / 配额耗尽都会标出来）。

    # ── 熔断过滤：刚连挂的模型本轮跳过 ──
    # fail-open: 全池都熔断时原样返回，否则一个坏 key 能让整个调度停摆
    from singularity.scheduler import _model_breaker
    alive = [a for a in deduped if _model_breaker.is_available(a.get("model", ""))]

    # ── 别名去重：两个 id 可能指向**同一个实际模型**（厂商把旧名路由到新模型）──
    # 委员会按"请求名"选席位，不去重就会拿同一个模型占两席 —— 看着是"多视角碰撞"，
    # 实际是自己跟自己碰，而这是唯一验证过有价值的那个能力。
    # 映射由 openai_agent 在响应路径上对账写入（`/v1/models` 只列主推名，看不出来）。
    try:
        from . import api_store as _as
        by_canon: dict[str, dict] = {}
        for a in alive:
            m = a.get("model", "")
            c = _as.canonical(m)
            cur = by_canon.get(c)
            # 撞车时**优先留"名字就是实际模型"的那个**：留着旧名会把能力评级也带错
            # （models.toml 里 deepseek-v4-pro 是 SS+，可它 9/14 后实际是 V4.1-Flash）。
            if cur is None or m == c:
                by_canon[c] = a
        if by_canon:
            alive = list(by_canon.values())   # dict 保插入序 → 链的顺序不变
    except Exception:
        pass   # 去重失败就当没去重，不能因此让链空掉

    return alive or deduped



_LAZY_SPOKES = ("_dispatch_skills", "_dispatch_exec", "_dispatch_crud")

# 🔴 **这个循环必须互斥**（2026-09-16 补；`lazy_spoke_import_failed` 已报 43 次跨三天）。
#
# 形状（读码得到，非推断）：`__getattr__` 按 `_LAZY_SPOKES` **依次** `import_module`。
# 两个线程同时进来的话：
#   · T1 开始执行 `_dispatch_skills` —— 它此刻**在 `sys.modules` 里，但还没跑完**；
#   · T2 也 `import_module("_dispatch_skills")`，拿到那个**半成品**，`hasattr` 为假，往后走；
#   · T2 接着 `import_module("_dispatch_exec")` —— 它第 8 行是
#     `from ..._dispatch_skills import _load_skills_for_agent`，而那个名字在
#     `_dispatch_skills` **第 57 行**才定义
#   ⇒ `cannot import name '_load_skills_for_agent' from partially initialized module`
#     —— **与真机告警文本逐字一致**。
# 加锁之后"进入这一段的线程只有一个"，后来者看到的是**已跑完**的模块。
# ⚠️ 用 **RLock**：辐条执行期间会反向 `from dispatcher import ...`，万一碰到属性访问
# 又落回这里，同一线程不能自锁死。
_LAZY_SPOKES_LOCK = threading.RLock()


def _custom_agents() -> dict:
    """`_dispatch_crud._load_custom_agents` 的**惰性**访问。

    ⚠️ 不能在本文件模块级 import 它 —— 辐条也 import 本模块，模块级就成环
    （详见文件末尾 `__getattr__` 那段）。
    ⚠️ 也**不能**指望那个 `__getattr__`：它只管"从外面 `dispatcher.X`"，
    **管不到本文件函数体内的全局名查找**（那是直接查模块 `__dict__`）。
    """
    from singularity.scheduler._dispatch_crud import _load_custom_agents
    return _load_custom_agents()


def __getattr__(name: str):
    """把三兄弟的名字**惰性**转发出去（PEP 562）。

    ⚠️ **原来这里是三句 `from ..._dispatch_* import *`**，而三兄弟**各有一条**
    `from singularity.scheduler.dispatcher import (...)` ⇒ **一个毂 + 三根双向辐条**。
    **谁先被导入谁吃亏**：先导 `_dispatch_crud` 时，它第 1 行去导 `dispatcher`，
    `dispatcher` 跑到这里执行 `from ..._dispatch_crud import *` —— 而那一刻
    `_dispatch_crud` **刚执行到第 1 行**、`update_agent` 还没定义
    ⇒ **`dispatcher.update_agent` 干脆不存在**。

    实测症状（2026-09-14）：`tests/test_scheduler/test_thinking_params.py` **单独跑必红**
    （`_api_admin.agent_update` 撞 `AttributeError`），而它在整套里是绿的
    （别的文件先导了 dispatcher）⇒ **顺序依赖**，正常用法（经包入口）永远看不到，
    所以这个洞躺了很久。

    **惰性化之后谁先被导入都行**：三兄弟要的那些名字（`load_agents` / `DispatchResult` /
    `_EXECUTOR_BY_TYPE` …）都定义在本文件**前面**，导入时就能拿到；而外面要的
    `dispatch` / `add_agent` / `invalidate_mcp_cache` …（定义在辐条里）**等访问时再解析**,
    那一刻三个模块早就加载完了。

    ⚠️ **试过、不行的两条**（记下来免得再试）：① 把辐条那条反向 import 挪到**文件末尾**
    —— 报错只是挪到隔壁 `_dispatch_skills`；② 拆"叶子模块"—— 另一个量级。
    ⚠️ **辐条侧惰性化也不行**：`_dispatch_skills` 会给 `_MCP_CACHE` **赋值**（带 `global`），
    惰性化会让它变成遮蔽、和 dispatcher 各拿一份。
    ⚠️ 全仓**没有任何地方**用 `from dispatcher import *`（2026-09-14 扫过），
    所以不需要保留"再导出"那层语义；`dispatcher.X` 这种属性访问照常работает。
    """
    # 🔴 **dunder 一律直接拒，不进循环**（2026-09-18 修，这才是 `lazy_spoke_import_failed` 的根）。
    #
    # 症状：`lazy_spoke_import_failed:_dispatch_exec:cannot import name
    # '_load_skills_for_agent' from partially initialized module '_dispatch_skills'`，
    # 跨三天报了 80+ 次，2026-09-16 加锁**没治好**（因为根本不是竞态）。
    #
    # 真机制（探针实测，不是推断）：`from singularity.scheduler.dispatcher import X`
    # 这条**普通 import 语句**，importlib 会先执行 `_handle_fromlist`，而它第一件事是
    # `hasattr(module, "__path__")`（判断是不是包）。本模块有 PEP 562 的模块级
    # `__getattr__` ⇒ **这一问被转发进来**，于是**每一次 from-import 都白白跑一遍
    # 惰性循环**（实测一次测试跑：`__path__` 64 次 + `__test__` 26 次 + `__bases__` 13 次）。
    # 而最毒的一次是**重入**：`_dispatch_skills` 模块体第 3 行自己就是一条
    # `from singularity.scheduler.dispatcher import (...)` ⇒ 它开始导入时又问了句
    # `__path__` ⇒ 循环里 `import_module("_dispatch_skills")` 拿到**半成品**（它正在被导入）
    # ⇒ 往下走 `_dispatch_exec` ⇒ 它第 8 行 `from _dispatch_skills import _load_skills_for_agent`
    # ⇒ 名字还不存在 ⇒ 炸。**同一个线程自己绕回来，RLock 是同线程放行的，锁挡不住这个形状。**
    #
    # 危害：告警刷屏只是表面 —— 更坏的是**把 `.qidian/alerts.jsonl` 变成不可信的账本**
    # （它正是查故障用的那份），而且每次 `lazy_spoke_import_failed` 都会**白导一遍**
    # 三兄弟。修在门口之后实测：一次全量测试 0 条。
    # ⚠️ 拒掉不会丢东西：三个辐条的 `__all__` 里**没有** dunder —— 唯一存在的
    # `__annotate__` / `__conditional_annotations__` 是 **Python 3.14 (PEP 649) 给模块
    # 自动加的**，不是辐条自己导出的，而且**没有任何调用方从 `dispatcher` 上取它们**
    # （取也是取某个函数/类的，不走模块转发）。`test_lazy_spoke_dunder.py` 里有一条
    # 守卫盯着这件事 —— 哪天有辐条真导出 dunder，它会红。
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    # 整个循环互斥 —— 见 `_LAZY_SPOKES_LOCK` 上面那段（半成品模块那个洞）。
    with _LAZY_SPOKES_LOCK:
        for _mod in _LAZY_SPOKES:
            try:
                _m = importlib.import_module(f"singularity.scheduler.{_mod}")
            except ImportError as e:
                # ⚠️ 别吞：辐条导不进来是真故障（循环导入 / 语法错），
                # 吞了就会被伪装成"dispatcher 没这个属性"，把真正的原因埋掉。
                witness.warn("dispatcher", f"lazy_spoke_import_failed:{_mod}:{e}"[:160],
                             key="lazy_spoke_import_failed")
                continue
            if hasattr(_m, name):
                return getattr(_m, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
