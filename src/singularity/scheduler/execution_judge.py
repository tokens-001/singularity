"""execution_judge.py — Fusion 多模型合成模块

架构方案阶段多个模型各出一份 → fuse_architecture 两阶段合成
（五维差异分析 → 按 schema 去重定稿）。
裁判/定稿模型取自 fusion.toml [custom]，见 _resolve_fusion_models。
"""

import json
import logging
import os

from singularity.scheduler import config, witness
from singularity.scheduler._io import try_parse_json

_log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════
# Fusion 配置
# ═══════════════════════════════════════════════

def _load_fusion_config() -> dict:
    """加载 fusion.toml 配置。"""
    try:
        from ._io import load_toml
        path = config.SCHEDULER_DIR / "fusion.toml"
        return load_toml(path)
    except Exception:
        return {}


# 兜底表：只给「注册表里没有、但确实要调」的模型用。正常路径走 api_store。
_LEGACY_API = {
    "gpt-5.5": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "gpt-5.5-pro": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "claude-opus-4-8": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1"),
}


def _resolve_api(model: str) -> tuple[str, str]:
    """模型 id → (api_key_env, base_url)。

    先查模型注册表的 provider，再查 api_store 的 base_url/key_env —— 这样
    「激活模型」里任何一个都能用。以前这里是硬编码 8 个 id 的白名单，
    其余模型静默返回空串（选 deepseek-v4-flash 融合会无声失败）。
    """
    from singularity.scheduler import api_store, model_registry
    provider = model_registry.provider_for_model(model)
    entry = api_store.get(provider) if provider else None
    if entry:
        return entry.api_key_env, entry.base_url
    return _LEGACY_API.get(model, ("", ""))


def _call_model(prompt: str, model: str, max_tokens: int = 2000) -> str:
    """调用单个模型（用于合成阶段）。未知模型 / 缺 key → 返回 ""。"""
    env_var, base_url = _resolve_api(model)
    api_key = os.environ.get(env_var, "")
    if not api_key:
        witness.heartbeat('execution_judge', f'warn:no_key:{model}:{env_var}'[:80])
        return ""
    try:
        import httpx
        with httpx.Client(timeout=httpx.Timeout(240.0)) as client:
            r = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens, "temperature": 0.3},
            )
            if r.status_code == 200:
                choice = r.json()["choices"][0]
                content = choice.get("message", {}).get("content") or ""
                if not content:
                    # 思考模型把 max_tokens 全烧在 reasoning 上 → content 为空。
                    # 静默返回 "" 会让上层（融合分析/盲评）无声降级，这里显式告警。
                    witness.heartbeat('execution_judge',
                        f'warn:empty_content:{model}:{choice.get("finish_reason", "")}'[:80])
                    return ""
                if choice.get("finish_reason") == "length":
                    witness.heartbeat('execution_judge', f'warn:truncated:{model}'[:80])
                return content
    except Exception as e:
        witness.heartbeat('execution_judge', f'warn:{e}')
    return ""


def _resolve_fusion_models(judge_model: str = "", synthesizer_model: str = "") -> tuple[str, str]:
    """定稿/裁判模型：显式参数 > fusion.toml [custom] > 硬编码默认。

    [custom] 是 fusion.toml 里唯一非 tier 段（dual/triple/super 是 tier），
    架构委员会按 agent 链选人、不分 tier，所以读它。
    """
    cfg = _load_fusion_config().get("custom", {}) or {}
    judge = judge_model or cfg.get("judge_model") or "deepseek-chat"
    synth = synthesizer_model or cfg.get("call_model") or judge
    return judge, synth


# ═══════════════════════════════════════════════════
# 架构方案专用 Fusion (Step 2: 3模型碰撞)
# ═══════════════════════════════════════════════════

# 融合阶段每份方案的字符上限。曾写死 2000 —— 而方案实际 8k~20k 字，裁判和定稿人
# 只看得到前 ~15%，等于蒙眼合成（实测 brief2 单稿 13114/14433/11873 字）。
# 0 = 不限。20k 覆盖目前所有观测到的方案长度。
_FUSION_PLAN_CHARS = int(os.environ.get("QIDIAN_FUSION_PLAN_CHARS", "20000"))

_ARCH_FUSION_STAGE1 = """你是架构合成裁判。以下 {n} 个模型对同一需求独立产出了架构方案。

【原始需求】
{task}

【各模型架构方案】
{outputs}

请输出五维差异分析 JSON:

{{
  "consensus": [
    {{"point": "所有模型一致的点", "confidence": "high"}}
  ],
  "contradictions": [
    {{
      "dimension": "modules/data_model/api/tech_stack/tasks",
      "point": "矛盾点",
      "positions": {{"model_1": "观点", "model_2": "观点"}},
      "resolution": "你的裁决及理由",
      "winner": "model_1|model_2|merge"
    }}
  ],
  "unique_insights": [
    {{"point": "只有一个模型提出的好想法", "source": "model_name", "adopt": true}}
  ],
  "blind_spots": [
    {{"what": "所有模型都遗漏的需求点", "suggestion": "补充建议"}}
  ]
}}

分析原则:
- contradictions 必须给出明确裁决，不能"两者都对"
- modules 维度: 对比模块划分粒度、命名、依赖关系
- data_model 维度: 对比实体设计、字段、关系、索引
- api_contracts 维度: 对比接口定义、错误处理
- tech_stack 维度: 对比技术选型及理由
- tasks 维度: 对比任务拆解、复杂度评定、依赖关系
- blind_spots 对照原始需求逐条检查"""

_ARCH_FUSION_STAGE2 = """你是架构合成定稿人。基于五维分析，产出一份统一的架构方案。

【原始需求】
{task}

【五维分析】
{analysis}

【各模型原始方案（参考）】
{outputs}

合成规则（吸收重写，不是拼接、也不是择一 —— 最终方案质量必须高于任何单一输入）:
1. consensus → 直接锁定，写入最终方案
2. contradictions → 按裁决采用 winner 的观点
3. unique_insights (adopt=true) → 补充进最终方案
4. blind_spots → 补充缺失部分
5. 模块名/实体名去重: 同名合并，异名同义选更清晰的名字
6. API 去重: 同路径同方法 → 保留更完整的 spec
7. 任务去重: 同描述 → 合并，保留更详细的那个
8. 约束去重: 同含义 → 保留更严格的验证方式
9. 风险去重: 同风险 → 合并缓解措施取并集
10. 顺便生成 test_cases: 基于 PRD 成功标准 + API契约 + state_machine 生成测试用例

输出必须严格遵循以下 JSON schema:

{{
  "architecture": "综述 (<500字)",
  "modules": [{{"name":"","responsibility":"","depends_on":[],"interfaces":[]}}],
  "data_model": {{"database":"","entities":[],"relationships":[]}},
  "api_contracts": [{{"method":"","path":"","description":"","input":{{}},"output":{{}},"errors":[]}}],
  "tech_stack": {{"language":"","framework":"","database":"","cache":"","mq":""}},
  "constraints": [{{"type":"","rule":"","check":""}}],
  "tasks": [{{"id":"","title":"","description":"","complexity":"","layer":"","depends_on":[],"acceptance":""}}],
  "risks": [{{"risk":"","impact":"","mitigation":""}}],
  "test_cases": {{
    "unit": [{{"name":"","target_module":"","input":"","expected":""}}],
    "integration": [{{"name":"","interfaces_tested":[],"setup":"","expected":""}}],
    "e2e": [{{"name":"","user_flow":"","success_criteria":""}}],
    "security": [{{"name":"","rule":"","source":"constraints|通用规则库","expected":""}}]
  }},
  "fusion_notes": {{
    "resolved_contradictions": 0,
    "adopted_insights": 0,
    "filled_blind_spots": 0,
    "dedup_stats": "模块/实体/API/任务/约束 各项去重数量",
    "confidence": "high/medium/low — 合成结果的可信度"
  }}
}}

只输出 JSON，用 ```json ... ``` 包裹。"""


def _warn_same_model(judge: str, synth: str, members: list[str] | None) -> None:
    """裁判/定稿人若就是某个选手，等于自己评自己 —— 结论直接作废。

    MAD 论文（EMNLP 2024）明确指出裁判会偏向与自己 backbone 相同的一方。
    同厂没法完全避免（3 家厂商全在委员会里时没有第三方可选），但同模型必须报警。
    """
    if not members:
        return
    for role, m in (("judge", judge), ("synth", synth)):
        if m and m in members:
            witness.heartbeat("execution_judge", f"warn:fusion_self_judge:{role}:{m}"[:80])


def fuse_architecture(task_desc: str, outputs: list[str],
                      judge_model: str = "",
                      synthesizer_model: str = "",
                      member_models: list[str] | None = None) -> str:
    """架构方案专用两阶段融合。

    阶段一: 五维差异分析 (consensus/contradictions/insights/blind_spots)
    阶段二: 基于分析定稿，schema 去重合并

    裁判/定稿模型默认取 fusion.toml [custom]（见 _resolve_fusion_models），
    传参可覆盖。以前这里写死 deepseek-chat，导致 fusion.toml 整份不生效。
    """
    if not outputs or len(outputs) < 2:
        return outputs[0] if outputs else ""

    judge_model, synthesizer_model = _resolve_fusion_models(judge_model, synthesizer_model)
    _warn_same_model(judge_model, synthesizer_model, member_models)

    # 阶段一: 五维分析
    lim = _FUSION_PLAN_CHARS
    outputs_text = "\n\n---\n".join(
        f"[模型{i+1}]\n{o if lim <= 0 else o[:lim]}" for i, o in enumerate(outputs)
    )
    stage1_prompt = _ARCH_FUSION_STAGE1.format(
        n=len(outputs), task=task_desc[:1500], outputs=outputs_text
    )
    analysis_raw = _call_model(stage1_prompt, judge_model, max_tokens=4000)
    analysis = try_parse_json(analysis_raw) if analysis_raw else {}

    # 阶段二: 基于分析定稿
    analysis_text = json.dumps(analysis, ensure_ascii=False, indent=2) if analysis else "分析不可用"
    stage2_prompt = _ARCH_FUSION_STAGE2.format(
        task=task_desc[:1500], analysis=analysis_text, outputs=outputs_text
    )
    fused = _call_model(stage2_prompt, synthesizer_model, max_tokens=16000)
    return fused if fused else outputs[0]


def decompose_architecture(arch_json: dict) -> list[dict]:
    """拆解器: 把 unified_architecture.tasks 转成可执行 task 列表。

    输入: fuse_architecture 输出的 unified_architecture JSON
    输出: [{desc, suggested_level, depends_on_local_id, context_snippet, acceptance}, ...]

    context_snippet: 从架构文档提取的任务相关上下文 (模块/接口/约束)
    acceptance: 对应 test_cases 中的验收条件
    """
    tasks = arch_json.get("tasks", [])
    if not tasks:
        return []
    modules = {m.get("name", ""): m for m in arch_json.get("modules", [])}
    api_contracts = arch_json.get("api_contracts", [])
    constraints = arch_json.get("constraints", [])
    test_cases = arch_json.get("test_cases", {})

    result = []
    for t in tasks:
        tid = t.get("id", "")
        title = t.get("title", "")
        desc = t.get("description", "")
        layer = t.get("layer", "")
        deps = t.get("depends_on", [])

        # 提取上下文片段
        ctx_parts = []
        # 关联模块
        for mod_name in t.get("related_modules", []):
            if mod_name in modules:
                m = modules[mod_name]
                ctx_parts.append(f"[{mod_name}] {m.get('responsibility','')}")

        # 关联 API
        api_names = t.get("api_contracts", [])
        for api in api_contracts:
            if api.get("path", "") in api_names or api.get("description", "") in api_names:
                ctx_parts.append(f"API {api.get('method','')} {api.get('path','')}: {api.get('description','')}")

        # 相关约束
        for c in constraints:
            if any(kw in title.lower() or kw in desc.lower()
                   for kw in [c.get("type", ""), c.get("rule", "")[:20]]):
                ctx_parts.append(f"约束[{c.get('type','')}]: {c.get('rule','')}")

        # acceptance 来自 test_cases
        acceptance = t.get("acceptance", "")
        if not acceptance:
            # 尝试从 test_cases 匹配
            for tc_type in ("unit", "integration", "e2e"):
                for tc in test_cases.get(tc_type, []):
                    if tc.get("target_module", "") in title or tc.get("name", "") in title:
                        if not acceptance:
                            acceptance = tc.get("expected", "")

        result.append({
            "desc": f"{title}: {desc}" if title else desc,
            "suggested_level": layer or "any",
            "depends_on_local_id": list(deps) if isinstance(deps, list) else ([deps] if deps else []),
            "context_snippet": "\n".join(ctx_parts) if ctx_parts else "",
            "acceptance": acceptance,
        })
    return result


def _is_architecture_task(task: str) -> bool:
    """检测是否为架构设计任务。

    只认强短语。以前还含 "模块"/"entity"/"architecture" 这类单字词，
    "修复登录模块的 token 过期判断" 也会命中 → 实现任务被送进委员会，
    禁工具跑 7 波、拿回一份架构 JSON 而不是代码。架构 prompt 本身含
    「模块划分/数据模型/API契约/技术栈/架构方案」，仍能命中。
    """
    arch_keywords = ["模块划分", "数据模型", "API契约", "技术栈", "架构方案", "系统架构"]
    return any(kw in task for kw in arch_keywords)
