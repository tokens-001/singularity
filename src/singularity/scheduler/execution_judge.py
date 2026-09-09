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


# 流式：read timeout 就是"多久没有新 token"的上限，超时即断开连接。
# 融合定稿一次要吐 2 万字，非流式只能干等整体超时，这里最该有停滞检测。
_STALL_TIMEOUT = float(os.environ.get("QIDIAN_STALL_TIMEOUT", "90"))


def _stream_once(client, base_url: str, headers: dict, payload: dict) -> tuple[int, str, str, str]:
    """一次流式 POST。返回 (status, content, finish_reason, err_text)。"""
    with client.stream("POST", f"{base_url}/chat/completions",
                       headers=headers, json={**payload, "stream": True}) as r:
        if r.status_code >= 400:
            r.read()                                  # 先取回 body 才能读 .text
            return r.status_code, "", "", (r.text or "")[:200]
        parts, finish = [], ""
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()                  # 容忍 "data:{...}" 无空格
            if body == "[DONE]":
                break
            chunk = json.loads(body)
            for ch in chunk.get("choices", []) or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        return 200, "".join(parts), finish, ""


def _call_model(prompt: str, model: str, max_tokens: int = 2000) -> str:
    """调用单个模型（用于合成/盲评）。未知模型 / 缺 key → 返回 ""。

    流式（QIDIAN_STREAM=0 可退回非流式）：停滞超过 QIDIAN_STALL_TIMEOUT 秒就断开。
    """
    env_var, base_url = _resolve_api(model)
    api_key = os.environ.get(env_var, "")
    if not api_key:
        witness.heartbeat('execution_judge', f'warn:no_key:{model}:{env_var}'[:80])
        return ""
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens, "temperature": 0.3}
        with httpx.Client(timeout=httpx.Timeout(240.0, connect=15.0, read=_STALL_TIMEOUT)) as client:
            status, content, finish, err = _stream_once(client, base_url, headers, payload)
            if status == 400 and "temperature" in err:
                # 部分模型只接受 temperature=1（实测 kimi-k3：'only 1 is allowed for this model'）
                payload.pop("temperature", None)
                status, content, finish, err = _stream_once(client, base_url, headers, payload)
            if status == 200:
                if not content:
                    # 思考模型把 max_tokens 全烧在 reasoning 上 → content 为空。
                    # 静默返回 "" 会让上层（融合分析/盲评）无声降级，这里显式告警。
                    witness.heartbeat('execution_judge',
                                      f'warn:empty_content:{model}:{finish}'[:80])
                    return ""
                if finish == "length":
                    witness.heartbeat('execution_judge', f'warn:truncated:{model}'[:80])
                return content
            # 非 200 以前什么都不记，上层只看到空串，查不出原因（kimi-k3 就是这样
            # 静默失败了很久：temperature 不被接受 → 400 → 空串）
            witness.heartbeat('execution_judge',
                              f'warn:http{status}:{model}:{err[:40]}'[:80])
    except Exception as e:
        witness.heartbeat('execution_judge', f'warn:{e}'[:80])
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

# 各方案全文的合计上限（v2 用；按 N 均分）。0 = 不限。
_FUSION_PLANS_TOTAL = int(os.environ.get("QIDIAN_FUSION_PLANS_TOTAL", "60000"))

# 定稿输出上限。实测融合稿要 24k~27k 字（含 schema 必填的 tasks/risks），
# 16000 token 撞顶被腰斩 —— 三题全部截断，且分数和"截断到哪"完美单调。
_FUSION_MAX_TOKENS = int(os.environ.get("QIDIAN_FUSION_MAX_TOKENS", "16000"))

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

{schema}

只输出 JSON，用 ```json ... ``` 包裹。"""

# 定稿输出 schema。旧两阶段和新 v2 共用 —— 单花括号（这里不经过 .format）。
_ARCH_SCHEMA = """{
  "architecture": "综述 (<500字)",
  "modules": [{"name":"","responsibility":"","depends_on":[],"interfaces":[]}],
  "data_model": {"database":"","entities":[],"relationships":[]},
  "api_contracts": [{"method":"","path":"","description":"","input":{},"output":{},"errors":[]}],
  "tech_stack": {"language":"","framework":"","database":"","cache":"","mq":""},
  "constraints": [{"type":"","rule":"","check":""}],
  "tasks": [{"id":"","title":"","description":"","complexity":"","layer":"","depends_on":[],"acceptance":""}],
  "risks": [{"risk":"","impact":"","mitigation":""}],
  "test_cases": {
    "unit": [{"name":"","target_module":"","input":"","expected":""}],
    "integration": [{"name":"","interfaces_tested":[],"setup":"","expected":""}],
    "e2e": [{"name":"","user_flow":"","success_criteria":""}],
    "security": [{"name":"","rule":"","source":"constraints|通用规则库","expected":""}]
  },
  "fusion_notes": {
    "resolved_contradictions": 0,
    "adopted_insights": 0,
    "filled_blind_spots": 0,
    "dedup_stats": "模块/实体/API/任务/约束 各项去重数量",
    "confidence": "high/medium/low — 合成结果的可信度"
  }
}"""


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
        task=task_desc[:1500], analysis=analysis_text, outputs=outputs_text,
        schema=_ARCH_SCHEMA,
    )
    fused = _call_model(stage2_prompt, synthesizer_model, max_tokens=_FUSION_MAX_TOKENS)
    return fused if fused else outputs[0]


# ═══════════════════════════════════════════════════
# 新融合机制 v2（QIDIAN_FUSION_V2=1）—— 见 docs/融合机制重设计.md
#
#   ② 提取三类（共识/分歧/独有优点）1 次
#   ③ 共享对话：发言方陈述 → 其余逐条 accept/insist → 有 insist 才确认，往复至收敛
#   ④ 发言方定稿 → 其余确认（不认可带 issues 重写 1 次）
#
# 相比旧两阶段：补上了「反驳通道」（insist + 解释），且定稿输入是对话结论
# 而不是 N 份方案全文 —— 从根上压住"取并集"造成的膨胀。
# 任一步拿不到输出 → 返回 ""，由调用方回退旧流程（默认路径不变）。
# ═══════════════════════════════════════════════════

_V2_EXTRACT = """你是架构委员会秘书。以下 {n} 个模型对同一需求独立产出了架构方案。

【原始需求】
{task}

【各模型方案】
{outputs}

请提取三类信息，只输出 JSON：

{{
  "consensus": ["所有模型一致的点，只列点不展开"],
  "disagreements": [
    {{"id": 1, "dimension": "modules|data_model|api_contracts|tech_stack|tasks|risks",
      "point": "分歧点一句话",
      "positions": {{"模型名": "该模型的立场"}},
      "raised_by": "提出方模型名（立场与对方相反的一方）"}}
  ],
  "unique_gains": [
    {{"id": 1, "content": "某家独有、别人没有的好做法", "from": "模型名", "impact": "影响面"}}
  ]
}}

规则:
- consensus 只列已一致的点，不要展开描述
- disagreements 只列真正互斥的（同一处两种不能并存的解法），措辞差异不算
- unique_gains 只列确实只有一家提出的，不要凑数
- 没有就留空数组
只输出 JSON，用 ```json ... ``` 包裹。"""

_V2_ROUND1 = """你是架构委员会成员「{speaker}」。委员会已把你的观点与其他成员的分歧列成了清单。

【原始需求】
{task}

【各模型方案】
{outputs}

【分歧清单】
{disagreements}

【各家独有做法】
{unique_gains}

请对**全部分歧点**逐条陈述己方理由，并对其余成员的独有做法表态。只输出 JSON：

{{
  "arguments": [{{"id": 1, "reason": "你为什么主张这个做法（技术理由）"}}],
  "unique_gains": [{{"id": 1, "stance": "adopt|reject", "reason": "..."}}]
}}

约束: 只谈清单上的条目，不许重述方案全文。只输出 JSON。"""

_V2_ROUND2 = """你是架构委员会成员「{speaker}」。以下是分歧清单与已有论证。

【原始需求】
{task}

【各模型方案】
{outputs}

【分歧清单与已有论证】
{transcript}

【各家独有做法】
{unique_gains}

请逐条回应，并对其余成员的独有做法表态。只输出 JSON：

{{
  "responses": [{{"id": 1, "verdict": "accept|insist",
                  "reason": "accept=被说服，采用对方观点；insist=坚持，并解释对方哪里误判"}}],
  "unique_gains": [{{"id": 1, "stance": "adopt|reject", "reason": "..."}}]
}}

约束: 只谈清单上的条目，不许重述方案全文。只输出 JSON。"""

_V2_ROUND3 = """你是架构委员会成员「{speaker}」。其他成员对你的论证给出了回应，其中有 insist。

【原始需求】
{task}

【分歧清单与双方论证】
{transcript}

请对标注 insist 的条目表态。只输出 JSON：

{{"confirms": [{{"id": 1, "verdict": "agree|question",
                 "reason": "agree=接受对方反驳；question=仍然质疑"}}]}}

只输出 JSON。"""

_V2_FINALIZE = """你是架构定稿人「{writer}」。下面是委员会的最终结论，请据此产出统一架构方案。

【原始需求】
{task}

【已达成共识】
{consensus}

【分歧结论（逐条已定，按此采用）】
{resolved}

【采纳的独有做法】
{adopted}

【已驳回的独有做法（不要写进方案）】
{rejected}

要求:
1. 分歧按结论采用对应立场 —— 不折中、不两个都写
2. 只写采纳的独有做法；驳回的一条都不要出现
3. 长度控制在单份方案的 1.1~1.3 倍以内 —— 不取并集、不重复、不堆砌
4. 顺便生成 test_cases（基于 PRD 成功标准 + API契约 + state_machine）

输出必须严格遵循以下 JSON schema:

{schema}

只输出 JSON，用 ```json ... ``` 包裹。"""

_V2_CONFIRM = """你是架构委员会成员「{checker}」。下面是「{writer}」根据委员会结论写出的定稿。

【原始需求】
{task}

【定稿】
{draft}

请检查三件事：分歧结论有没有被正确落实？有没有把驳回的做法写了进去？有没有明显缺失？
只输出 JSON：

{{"approved": true, "issues": ["不认可时逐条列出"]}}

只输出 JSON。"""


def _fusion_v2_enabled() -> bool:
    """读 env（不缓存）—— 测试和 A/B 都要能中途切换。"""
    return os.environ.get("QIDIAN_FUSION_V2") == "1"


def _parallel(thunks: list) -> list:
    """跑一批无参函数，保序返回结果。"""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(thunks), 4)) as ex:
        return [f.result() for f in [ex.submit(t) for t in thunks]]


def _plans_block(plans: list[tuple[str, str]]) -> str:
    """各方案全文。单份上限 QIDIAN_FUSION_PLAN_CHARS，合计上限 QIDIAN_FUSION_PLANS_TOTAL。

    总量上限是必须的：N 份 × 单份上限没有约束时，N=3 写满就是 60k 字（≈40-60k token），
    能顶爆 64k 上下文的模型。按 N 均分，保证每家拿到同样预算。
    """
    lim = _FUSION_PLAN_CHARS
    if _FUSION_PLANS_TOTAL > 0 and plans:
        per = _FUSION_PLANS_TOTAL // len(plans)
        lim = per if lim <= 0 else min(lim, per)
    if lim > 0 and any(len(o) > lim for _, o in plans):
        witness.heartbeat("execution_judge", f"warn:plans_truncated:{lim}"[:80])
    return "\n\n---\n".join(
        f"[{m}]\n{o if lim <= 0 else o[:lim]}" for m, o in plans)


def _first_speaker(disagreements: list, members: list[str]) -> str:
    """轮 1 发言方 = 提出分歧最多的一方；平手取 members 顺序靠前者。"""
    cnt = {m: 0 for m in members}
    for d in disagreements:
        who = d.get("raised_by", "")
        if who in cnt:
            cnt[who] += 1
    return max(members, key=lambda m: cnt[m])


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _votes_into(store: dict, who: str, items: list, field: str) -> dict:
    """把 [{id, <field>}] 收进 store[(who, id)]（只留最新一票），返回本轮结果。"""
    cur = {}
    for it in items or []:
        if isinstance(it, dict) and "id" in it:
            store[(who, it["id"])] = it.get(field, "")
            cur[it["id"]] = it.get(field, "")
    return cur


def fuse_architecture_v2(task_desc: str, plans: list[tuple[str, str]],
                         judge_model: str = "") -> str:
    """新融合机制。plans: [(模型名, 方案全文)]。任一步失败返回 ""。"""
    if len(plans) < 2:
        return plans[0][1] if plans else ""
    members = [m for m, _ in plans]
    judge, _ = _resolve_fusion_models(judge_model, "")
    _warn_same_model(judge, "", members)

    task = task_desc[:1500]
    plans_text = _plans_block(plans)

    # ── ② 提取三类 ──
    raw = _call_model(_V2_EXTRACT.format(n=len(plans), task=task, outputs=plans_text),
                      judge, max_tokens=8000)
    deltas = try_parse_json(raw) if raw else {}
    # try_parse_json 失败返回 {"parse_error": True}（仍是 dict）—— 不判它就会
    # 静默退化成"没有分歧"，然后拿垃圾定稿。
    if not isinstance(deltas, dict) or deltas.get("parse_error"):
        return ""
    disagreements = [d for d in (deltas.get("disagreements") or []) if isinstance(d, dict)]
    gains = [g for g in (deltas.get("unique_gains") or []) if isinstance(g, dict)]
    consensus = deltas.get("consensus") or []

    writer = _first_speaker(disagreements, members)
    others = [m for m in members if m != writer]

    # ── ③ 共享对话 ──
    # 轮 1 发言方陈述 → 其余成员逐条回应 → 有 insist 才让发言方确认，交替往复。
    # 终止三选一：全 accept / 发言方全 agree（有人让步才算结论）/ 复读 / 撞轮数上限。
    # 只认「有人让步」是刻意的 —— 否则发言方一句 question 就终局，多给的轮次是死代码。
    max_rounds = max(2, int(os.environ.get("QIDIAN_FUSION_V2_ROUNDS", "5")))
    transcript = []
    resp_votes, conf_votes, gain_votes = {}, {}, {}   # (谁, 条目id) → 最新一票
    if disagreements or gains:
        d_json, g_json = _j(disagreements), _j(gains)
        a1 = try_parse_json(_call_model(
            _V2_ROUND1.format(speaker=writer, task=task, outputs=plans_text,
                              disagreements=d_json, unique_gains=g_json),
            writer, max_tokens=6000) or "") or {}
        transcript.append(f"[{writer} 陈述]\n{_j(a1)}")
        _votes_into(gain_votes, writer, a1.get("unique_gains"), "stance")
        rounds, prev = 1, {}

        while rounds < max_rounds:
            def _respond(m):
                r = _call_model(_V2_ROUND2.format(
                    speaker=m, task=task, outputs=plans_text,
                    transcript="\n\n".join(transcript), unique_gains=g_json),
                    m, max_tokens=6000)
                return m, (try_parse_json(r) if r else {})

            cur = {}
            for m, a in _parallel([lambda m=m: _respond(m) for m in others]):
                a = a if isinstance(a, dict) else {}
                transcript.append(f"[{m} 回应]\n{_j(a)}")
                cur.update(_votes_into(resp_votes, m, a.get("responses"), "verdict"))
                _votes_into(gain_votes, m, a.get("unique_gains"), "stance")
            rounds += 1
            if cur == prev:
                break                       # 复读机 → 再辩也没新信息，别烧 token
            prev = dict(cur)
            if not any(v == "insist" for v in cur.values()):
                break                       # 全 accept → 收敛
            if rounds >= max_rounds:
                break

            c = try_parse_json(_call_model(
                _V2_ROUND3.format(speaker=writer, task=task,
                                  transcript="\n\n".join(transcript)),
                writer, max_tokens=4000) or "") or {}
            transcript.append(f"[{writer} 确认]\n{_j(c)}")
            cur_c = _votes_into(conf_votes, writer, c.get("confirms"), "verdict")
            rounds += 1
            if not any(v == "question" for v in cur_c.values()):
                break                       # 发言方全认了 → 收敛

    # 撞上限仍有 question 的点 → 按发言方处理，但别让它静默通过
    stuck = [i for (w, i), v in conf_votes.items() if v == "question"]
    if stuck:
        witness.heartbeat("execution_judge", f"warn:fusion_stuck:{len(stuck)}"[:80])

    # 分歧结论：默认发言方胜；对方 insist 且发言方 agree（认输）→ 对方胜。
    # ponytail: 多个 insist 方各自立场不同时只记第一个 —— N>2 才有的歧义，
    # 实际分歧点几乎都是两家对立（spec 按两方设计）。
    resolved = []
    for d in disagreements:
        did = d.get("id")
        vs = [v for (m, i), v in resp_votes.items() if i == did]
        winner = writer
        if "insist" in vs and conf_votes.get((writer, did)) == "agree":
            winner = next((m for (m, i), v in resp_votes.items()
                           if i == did and v == "insist"), writer)
        resolved.append({**d, "winner": winner})

    # 独有做法：全体 adopt 才采纳（保守 —— 长度就是膨胀的主因）
    adopted = []
    for g in gains:
        stances = [v for (m, i), v in gain_votes.items() if i == g.get("id")]
        if stances and all(s == "adopt" for s in stances):
            adopted.append(g)
    rejected = [g for g in gains if g not in adopted]

    # ── ④ 定稿 + 确认 ──
    final_prompt = _V2_FINALIZE.format(
        writer=writer, task=task, consensus=_j(consensus), resolved=_j(resolved),
        adopted=_j(adopted), rejected=_j(rejected), schema=_ARCH_SCHEMA)
    draft = _call_model(final_prompt, writer, max_tokens=_FUSION_MAX_TOKENS)
    if not draft:
        return ""

    for attempt in range(2):
        def _check(m):
            c = _call_model(_V2_CONFIRM.format(checker=m, writer=writer, task=task, draft=draft),
                            m, max_tokens=2000)
            return try_parse_json(c) if c else {}
        issues = []
        for p in _parallel([lambda m=m: _check(m) for m in others]):
            if not isinstance(p, dict) or p.get("approved", True):
                continue
            issues += [str(i) for i in (p.get("issues") or [])] or ["(未给出具体问题)"]
        if not issues or attempt:
            break
        draft = _call_model(
            final_prompt + "\n\n【上一稿被指出的问题，请修正】\n" + "\n".join(issues),
            writer, max_tokens=_FUSION_MAX_TOKENS) or draft
    return draft


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
