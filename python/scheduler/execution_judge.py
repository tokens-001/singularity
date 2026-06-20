"""execution_judge.py — 执行裁判模块

每个 Agent 执行完成后，裁判用便宜的 E 层模型快速判断：
- 任务是否真的完成了？（对比描述和实际产出）
- 质量是否可接受？
不合格的自动打回重试，最多 3 次，每次换不同的模型。

依赖：scheduler 通用 LLM 调用、tracker 状态读写。
"""

import fcntl
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from . import config
from ._io import try_parse_json

_log = logging.getLogger(__name__)


@dataclass
class JudgeVerdict:
    """裁判判定结果。"""
    pass_: bool                    # 是否通过
    score: float = 0.0            # 0-1 质量分
    reason: str = ""              # 失败原因（注入下一轮 Reflexion）
    failure_mode: str = ""        # 失败模式分类：tool_loop / empty_output / json_error / semantic_error / context_insufficient / unknown
    uncertain: bool = False       # 不确定时给 pass=true 但标记 uncertain，宁可放过不冤杀

    def to_dict(self) -> dict:
        return {
            "pass": self.pass_, "score": self.score, "reason": self.reason,
            "failure_mode": self.failure_mode, "uncertain": self.uncertain,
        }


def judge(task_description: str, agent_output: str, task_type: str = "default") -> JudgeVerdict:
    """裁判入口：对比任务描述和 Agent 产出，返回结构化判定。

    先做低成本预检（空输出、不合法 JSON 等），预检不通过直接返回 fail，
    省掉一次 LLM 调用。
    """
    # ── 预检层：低成本拦截明显失败 ──
    pre = _pre_check(agent_output)
    if pre:
        return pre

    # ── LLM 裁判层 ──
    return _llm_judge(task_description, agent_output, task_type)


def _pre_check(output: str) -> Optional[JudgeVerdict]:
    """低成本预检：空输出、非法 JSON、tool-loop 残留检测。"""
    if not output or not output.strip():
        return JudgeVerdict(
            pass_=False, score=0.0, reason="空输出",
            failure_mode="empty_output",
        )

    # 检测 tool-loop 残留（Agent 输出中包含未完成的工具调用标记）
    tool_loop_markers = [
        '{"tool_calls"', '"function_call"', '```json\n{"name":',
        'I need to read', 'Let me search', 'Let me check the file',
    ]
    loop_count = sum(1 for m in tool_loop_markers if m.lower() in output.lower())
    if loop_count >= 3:
        return JudgeVerdict(
            pass_=False, score=0.1, reason=f"检测到 tool-loop 残留({loop_count}处)",
            failure_mode="tool_loop",
        )

    # 检测 JSON 格式错误（输出中包含未闭合的括号）
    if output.strip().startswith("{") or output.strip().startswith("["):
        try:
            json.loads(output.strip())
        except json.JSONDecodeError:
            return JudgeVerdict(
                pass_=False, score=0.2, reason="输出 JSON 格式不合法",
                failure_mode="json_error",
            )

    return None


def _llm_judge(task_desc: str, output: str, task_type: str) -> JudgeVerdict:
    """调用 E 层模型做语义判断。"""
    prompt = _build_judge_prompt(task_desc, output, task_type)
    raw = _call_e_layer(prompt)

    return _parse_verdict(raw)


def _build_judge_prompt(task_desc: str, output: str, task_type: str) -> str:
    """构建裁判 prompt——防偏见：不说'这是 Agent 的输出请打分'，
    改为'请判断这段代码修改是否完成了以下需求'。"""
    output_snippet = output[:3000]  # 截断避免 token 浪费
    return f"""请判断以下代码/文本修改是否完成了所述需求。

【需求描述】
{task_desc}

【任务类型】{task_type}

【实际产出】
{output_snippet}

【判断标准】
- 是否完成了需求描述中的所有要点？
- 产出是否可以直接使用（不需要人工修补）？
- 如果产出看起来正确但你不完全确定，标记 uncertain=true

【输出格式】只输出 JSON：
{{"pass": true/false, "score": 0.0-1.0, "reason": "一句话原因", "failure_mode": "semantic_error/context_insufficient/ok", "uncertain": true/false}}"""


def _parse_verdict(raw: str) -> JudgeVerdict:
    """解析 LLM 返回的 JSON verdict。"""
    if not raw:
        return JudgeVerdict(pass_=True, score=0.5, reason="裁判未返回结果，默认放行",
                            failure_mode="unknown", uncertain=True)

    # 用 _io.try_parse_json 统一提取 JSON
    result = try_parse_json(raw)
    if not result.get("parse_error"):
        return JudgeVerdict(
            pass_=bool(result.get("pass", True)),
            score=float(result.get("score", 0.5)),
            reason=str(result.get("reason", "")),
            failure_mode=str(result.get("failure_mode", "unknown")),
            uncertain=bool(result.get("uncertain", False)),
        )

    return JudgeVerdict(pass_=True, score=0.5, reason="裁判结果解析失败，默认放行",
                        failure_mode="unknown", uncertain=True)


def _call_e_layer(prompt: str) -> str:
    """调 E 层最便宜模型。"""
    import os
    try:
        import httpx
        # 从环境获取第一个可用的 E 层 API
        for env_var, base_url, model in [
            ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions", "deepseek-chat"),
            ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-5-turbo"),
            ("KIMI_API_KEY", "https://api.moonshot.cn/v1/chat/completions", "kimi-k2.7-code"),
        ]:
            api_key = os.environ.get(env_var, "")
            if api_key:
                with httpx.Client(timeout=httpx.Timeout(30)) as client:
                    r = client.post(
                        base_url,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": "你是代码审查裁判。只输出要求的 JSON。"},
                                {"role": "user", "content": prompt},
                            ],
                            "max_tokens": 256,
                            "temperature": 0.1,
                        },
                    )
                    if r.status_code == 200:
                        data = r.json()
                        return data["choices"][0]["message"]["content"]
                    else:
                        _log.warning(f"Judge LLM {env_var} returned {r.status_code}")
        return ""
    except Exception as e:
        _log.warning(f"Judge LLM call failed: {e}")
        return ""


# ═══════════════════════════════════════════════
# 重试策略
# ═══════════════════════════════════════════════

def should_retry(verdict: JudgeVerdict, retry_count: int, max_retries: int = 3) -> bool:
    """判断是否应该重试。信息不足不重试。"""
    if verdict.pass_:
        return False
    if retry_count >= max_retries:
        return False
    if verdict.failure_mode == "context_insufficient":
        return False  # 缺少上下文，重试无意义
    return True


def build_reflexion_feedback(verdict: JudgeVerdict) -> str:
    """构建下一轮的 Reflexion 提示。"""
    return (
        f"[上一轮结果不合格]\n"
        f"原因: {verdict.reason}\n"
        f"失败模式: {verdict.failure_mode}\n"
        f"请修正后重新输出。"
    )


# ═══════════════════════════════════════════════
# Self-Fusion — 多模型并行 + 合成裁判 (model-fusion 论文)
# ═══════════════════════════════════════════════

def _load_fusion_config() -> dict:
    """加载 fusion.toml 配置。HermesFusion: 模型无关，换模型只改配置不改代码。"""
    try:
        from ._io import load_toml
        from . import config
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


def _stage1_analyze(task: str, outputs: list[str], judge_model: str = "deepseek-chat") -> dict:
    """阶段一：裁判模型输出结构化五维 JSON。"""
    outputs_text = "\n\n---\n".join(
        f"[模型{i+1}]\n{o[:1500]}" for i, o in enumerate(outputs)
    )
    prompt = _STAGE1_PROMPT.format(task=task, outputs=outputs_text)
    raw = _call_e_layer(prompt)
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
                 outputs: list[str] = None, tier: str = "budget") -> str:
    """Fusion 两阶段合成:
    阶段一: 裁判模型输出结构化五维JSON分析
    阶段二: 调用模型基于五维分析+6项提纲写出定稿

    防递归: FUSION_CHILD=1 环境变量防止融合模型再调融合
    tier: budget|self|standard
    """
    import os
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
        fused = _call_single_model(prompt, call_model)
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
                    "enum": ["budget", "self", "standard"],
                    "description": "融合级别: budget(便宜2模型), self(同模型跑两遍), standard(强模型)"
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


def _call_single_model(prompt: str, model: str) -> str:
    """直接调单个模型（用于合成阶段）。"""
    import os
    api_map = {
        "deepseek-chat": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions"),
        "glm-5-turbo": ("ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        "kimi-k2.7-code": ("KIMI_API_KEY", "https://api.moonshot.cn/v1/chat/completions"),
        "deepseek-v4-pro": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions"),
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
    except Exception:
        pass
    return ""

# ═══════════════════════════════════════════════
# Fusion 进阶机制
# ═══════════════════════════════════════════════

_CROSS_MODEL_PROMPT = """[跨模型第二意见]
单模型审查完毕。以下是另一个模型对同一任务的独立分析。
请对比你的结论和以下第二意见，找出:
1. 你同意但第二意见反对的点
2. 你反对但第二意见坚持的点
3. 第二意见发现但你遗漏的点
4. 综合判断: 是否需要修正你的结论？

第二意见:
{other_output}"""

_FINDING_CLASSES = {
    "contract_misread": "误读需求 — 模型误解了任务要求，产出偏离原始意图",
    "valid_actionable": "有效可操作 — 真实问题，有明确的修复方案",
    "valid_tradeoff": "有效但可接受 — 方案合理但非唯一，属于风格或取舍差异",
    "noise": "噪音 — 误报或不影响实际功能的细枝末节",
}

def classify_finding(finding: str) -> str:
    """用 cheap-model 对发现做 4 类分类。
    返回: contract_misread | valid_actionable | valid_tradeoff | noise
    ponytail: 单次 LLM 调用。需要时上训练分类器。
    """
    classes_desc = "\n".join(f"- {k}: {v}" for k, v in _FINDING_CLASSES.items())
    prompt = f"将以下代码审查发现归入4类之一:\n{classes_desc}\n\n发现: {finding}\n\n只输出类别名。"
    raw = _call_e_layer(prompt)
    for k in _FINDING_CLASSES:
        if k in raw:
            return k
    return "valid_actionable"  # 默认归为有效

def build_cross_model_prompt(other_output: str) -> str:
    """生成跨模型第二意见提示。"""
    return _CROSS_MODEL_PROMPT.format(other_output=other_output[:1500])


def run_parallel_models(task_desc: str, level: str = "E", tier: str = "budget") -> list[str]:
    """并行调用 N 个模型产出独立方案。

    Budget Fusion: 3模型 (DeepSeek+GLM+Kimi)
    Self Fusion: 同模型×2，不同温度增加推理多样性 (+6.7分论文验证)
    Standard Fusion: 2强模型

    配置: fusion.toml tiers.<tier> (HermesFusion格式)。
    文件锁: flock 防并发跑两次。
    """
    import os
    import concurrent.futures
    from .model_registry import provider_for_model

    cfg = _load_fusion_config()
    if tier == "custom":
        custom = cfg.get("custom", {})
        tier_cfg = {
            "models": custom.get("models", ["deepseek-chat", "glm-5-turbo"]),
            "judge_model": custom.get("judge_model", "deepseek-chat"),
            "call_model": custom.get("call_model", "deepseek-chat"),
            "max_tokens": 2000, "temperature": 0.7, "timeout_sec": 60,
        }
    else:
        tier_cfg = cfg.get("tiers", {}).get(tier, cfg.get("tiers", {}).get("budget", {}))
    models = tier_cfg.get("models", ["deepseek-chat", "glm-5-turbo"])
    roles_list = tier_cfg.get("roles", ["builder", "skeptic"])
    base_temp = tier_cfg.get("temperature", 0.7)
    max_tokens = tier_cfg.get("max_tokens", 2000)
    timeout = tier_cfg.get("timeout_sec", 60)

    # 文件锁: 防并发跑两次 (借鉴 HermesFusion flock)
    import fcntl
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
            except Exception:
                pass
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
            except Exception:
                pass
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
