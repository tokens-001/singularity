"""execution_judge.py — Fusion 多模型合成模块

ponytail: AI裁判已移除。仅保留 fuse_outputs() 用于规划阶段多模型并行出方案。
架构方案阶段可以多个模型各出一份，fuse_outputs 交叉合成一份最优方案。
"""

import fcntl
import json
import logging
import os
from typing import Optional

from singularity.scheduler import config, witness
from singularity.scheduler._io import try_parse_json

_log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════
# Fusion 配置 & 角色
# ═══════════════════════════════════════════════

def _load_fusion_config() -> dict:
    """加载 fusion.toml 配置。"""
    try:
        from ._io import load_toml
        path = config.SCHEDULER_DIR / "fusion.toml"
        return load_toml(path)
    except Exception:
        return {}

# 角色定义 — 借鉴 model-fusion 角色多样性 (skeptic/builder/analyst)
_FUSION_ROLES = {
    "builder": "你是 Builder（建设者）。关注可实现性、具体步骤、代码结构、模块划分。给出可落地的方案。",
    "skeptic": "你是 Skeptic（质疑者）。主动找方案的漏洞：边界条件、并发安全、异常路径、向后兼容。指出所有可能出错的地方。",
    "analyst": "你是 Analyst（分析者）。关注架构合理性、技术选型权衡、长期维护成本。从更高维度评估方案。",
}

# ═══════════════════════════════════════════════
# 阶段一：裁判分析 — 五维结构化 JSON
# ═══════════════════════════════════════════════

_STAGE1_PROMPT = """你是 Fusion 裁判分析器。以下 N 个模型对同一任务独立产出了方案/代码。

【任务】
{task}

【各模型产出】
{outputs}

请输出结构化五维分析 JSON（不要输出其他内容）:

{{
  "consensus": ["所有模型一致同意的点 — 最高置信，直接锁定"],
  "contradictions": [
    {{"point": "矛盾点描述", "model_a": "模型A观点", "model_b": "模型B观点", "resolution": "你的裁决及理由"}}
  ],
  "partial_coverage": [
    {{"point": "部分模型覆盖的点", "covered_by": ["model_x"], "confidence": "high/medium/low"}}
  ],
  "unique_insights": [
    {{"point": "只有一个模型提出的独到见解", "source_model": "model_name"}}
  ],
  "blind_spots": ["需求要求但所有模型都遗漏的点"]
}}

分析原则:
- consensus 只放真正一致的，不要模糊归类
- contradictions 必须给出明确裁决，不能 "两者都对"
- blind_spots 对照原始需求逐条检查，不要说 "无"
- 如果某个维度确实为空，用空数组 []"""


def _call_model(prompt: str, model: str) -> str:
    """调用单个模型（用于合成阶段）。"""
    api_map = {
        "deepseek-chat": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions"),
        "deepseek-v4-pro": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions"),
        "glm-5-turbo": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        "glm-5.2": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        "kimi-k2.7-code": ("KIMI_API_KEY", "https://api.moonshot.cn/v1/chat/completions"),
        "gpt-5.5": ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
        "gpt-5.5-pro": ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
        "claude-opus-4-8": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/messages"),
    }
    env_var, base_url = api_map.get(model, ("", ""))
    api_key = os.environ.get(env_var, "")
    if not api_key:
        return ""
    try:
        import httpx
        with httpx.Client(timeout=httpx.Timeout(60)) as client:
            r = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 2000, "temperature": 0.3},
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        witness.heartbeat('execution_judge', f'warn:{e}')
    return ""


def _stage1_analyze(task: str, outputs: list[str], judge_model: str = "deepseek-chat") -> dict:
    """阶段一：裁判模型输出结构化五维 JSON。"""
    outputs_text = "\n\n---\n".join(
        f"[模型{i+1}]\n{o[:1500]}" for i, o in enumerate(outputs)
    )
    prompt = _STAGE1_PROMPT.format(task=task, outputs=outputs_text)
    raw = _call_model(prompt, judge_model)
    return try_parse_json(raw) if raw else {}


# ═══════════════════════════════════════════════
# 阶段二：基于五维分析 + 6项合成提纲定稿
# ═══════════════════════════════════════════════

_STAGE2_PROMPT = """你是 Fusion 最终定稿人。请基于以下五维分析和6项合成提纲写出最终答案。

【原始任务】
{task}

【五维分析】
{analysis}

【各模型原始产出】
{outputs}

按以下6项提纲合成（借鉴 model-fusion）:
1. Claim ledger — 列出所有模型的所有主张，不遗漏任何观点
2. Correlated-error check — 多个模型犯同类错误 → 可能是 prompt 歧义，标注出来
3. Evidence-based contradiction resolution — 矛盾不靠投票，靠证据。说明为什么选A不选B
4. Coverage union — 取所有模型的覆盖面并集，确保没有遗漏
5. Calibration — 标注每个结论的置信度 (high/medium/low)
6. Anti-majority guard — 少数派意见如果证据充分，保留不丢弃

只输出最终方案/代码，不输出分析过程。"""


def fuse_outputs(task_desc: str, output_a: str, output_b: str,
                 outputs: list[str] = None, tier: str = "triple") -> str:
    """Fusion 两阶段合成:
    阶段一: 裁判模型输出结构化五维JSON分析
    阶段二: 调用模型基于五维分析+6项提纲写出定稿

    防递归: FUSION_CHILD=1 环境变量防止融合模型再调融合
    tier: budget|self|standard
    """
    all_outputs = outputs or [output_a, output_b]
    if len(all_outputs) < 2:
        return all_outputs[0] if all_outputs else ""

    # 防递归: 如果已经是 Fusion 子进程，直接返回第一个输出
    if os.environ.get("FUSION_CHILD") == "1":
        return all_outputs[0]

    cfg = _load_fusion_config()
    tier_cfg = cfg.get("tiers", {}).get(tier, {})
    judge_model = tier_cfg.get("judge_model", "deepseek-chat")

    # 阶段一
    analysis = _stage1_analyze(task_desc, all_outputs, judge_model)

    # 阶段二: 基于五维分析 + 6项提纲定稿
    outputs_text = "\n\n---\n".join(
        f"[模型{i+1}]\n{o[:1200]}" for i, o in enumerate(all_outputs)
    )
    analysis_text = json.dumps(analysis, ensure_ascii=False, indent=2) if analysis else "分析不可用"
    prompt = _STAGE2_PROMPT.format(task=task_desc, analysis=analysis_text, outputs=outputs_text)

    call_model = tier_cfg.get("call_model", "deepseek-chat")
    # 标记子进程防递归
    os.environ["FUSION_CHILD"] = "1"
    try:
        fused = _call_model(prompt, call_model)
    finally:
        os.environ.pop("FUSION_CHILD", None)
    return fused if fused else f"{output_a}\n\n---\n{output_b}"


# ═══════════════════════════════════════════════
# Fusion Tool — 模型可自主调用
# ═══════════════════════════════════════════════

FUSION_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "fusion_second_opinion",
        "description": "对当前任务请求跨模型第二意见。当你遇到架构决策、安全边界、或不确定的方案时调用。会并行调另一个模型+合成裁判给出融合结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "需要第二意见的具体问题或决策点"
                },
                "tier": {
                    "type": "string",
                    "enum": ["dual", "triple", "super"],
                    "description": "融合级别: dual(双模型), triple(三模型), super(超级协作)"
                }
            },
            "required": ["question"]
        }
    }
}


def get_fusion_tool_def() -> dict:
    """返回 Fusion 工具定义，注册给 executor 的 function calling。"""
    return FUSION_TOOL_DEF


def execute_fusion_tool(question: str, tier: str = "budget") -> str:
    """执行跨模型第二意见。由 executor 在 tool_call 时调用。"""
    outputs = run_parallel_models(question)
    if len(outputs) < 2:
        return outputs[0] if outputs else "Fusion 不可用: 模型不足"
    return fuse_outputs(question, outputs[0], outputs[1], outputs=outputs, tier=tier)


def run_parallel_models(task_desc: str, level: str = "E", tier: str = "budget") -> list[str]:
    """并行调用 N 个模型产出独立方案。

    Budget Fusion: 3模型 (DeepSeek+GLM+Kimi)
    Self Fusion: 同模型×2，不同温度增加推理多样性 (+6.7分论文验证)
    Standard Fusion: 2强模型

    配置: fusion.toml tiers.<tier> (HermesFusion格式)。
    文件锁: flock 防并发跑两次。
    """
    import concurrent.futures
    from .model_registry import provider_for_model

    cfg = _load_fusion_config()
    # 支持新框架 dual/triple/super + 旧兼容 budget→dual, standard→triple, self→dual
    tier_map = {"budget": "dual", "self": "dual", "standard": "triple"}
    tier = tier_map.get(tier, tier)
    tier_cfg = cfg.get(tier, cfg.get("triple", {}))
    if not tier_cfg:
        tier_cfg = {"models": ["deepseek-chat", "glm-5-turbo"], "max_tokens": 2000, "temperature": 0.7, "timeout_sec": 60}
    models = tier_cfg.get("models", ["deepseek-chat", "glm-5-turbo"])
    roles_list = tier_cfg.get("roles", ["builder", "skeptic"])
    base_temp = tier_cfg.get("temperature", 0.7)
    max_tokens = tier_cfg.get("max_tokens", 2000)
    timeout = tier_cfg.get("timeout_sec", 60)

    # 文件锁: 防并发跑两次 (借鉴 HermesFusion flock)
    lock_path = config.QIDIAN_DIR / ".fusion.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = open(str(lock_path), "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        if lock_fd:
            lock_fd.close()
        return []  # 已有 Fusion 在跑

    try:
        def _call_one(model, role, idx):
            api_key = ""
            base_url = ""
            try:
                from .api_store import available_apis
                provider = provider_for_model(model)
                for api_entry in available_apis():
                    if api_entry.id == provider or api_entry.provider == provider:
                        api_key = os.environ.get(api_entry.api_key_env, "")
                        base_url = api_entry.base_url
                        break
            except Exception as e:
                witness.heartbeat('execution_judge', f'warn:{e}')
            if not api_key or not base_url:
                return ""

            # Self-Fusion: 同模型不同温度 (+0.1/instance)
            temp = base_temp + (0.1 * idx) if tier == "self" else base_temp

            system_prompt = _FUSION_ROLES.get(role, "你是代码专家。")
            try:
                import httpx
                with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
                    r = client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": task_desc},
                            ],
                            "max_tokens": max_tokens,
                            "temperature": min(temp, 1.5),
                        },
                    )
                    if r.status_code == 200:
                        return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                witness.heartbeat('execution_judge', f'warn:{e}')
            return ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
            roles = roles_list + ["analyst"] * (len(models) - len(roles_list))
            futures = [pool.submit(_call_one, models[i], roles[i], i) for i in range(len(models))]
            results = [f.result() for f in futures]
        return [r for r in results if r]
    finally:
        if lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
