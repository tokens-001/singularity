"""execution_judge.py — Fusion 多模型合成模块

架构方案阶段多个模型各出一份 → `fuse_architecture_v2` 合成
（提取三类 → 结构化辩论 → 按 schema 定稿）。

旧两阶段 `fuse_architecture` 已于 2026-09-11 删除：它有已知致命缺陷（取并集膨胀到
输入之和 1.8×、撞 max_tokens 腰斩、丢过整个 tasks 段），却被当作 v2 失败时的兜底 ——
而告警日志显示它**从未在真机触发过**。兜底改由「提取失败换模型重试」承担，见
`fuse_architecture_v2` 里的 `_extract_once`。
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


def _stream_once(client, base_url: str, headers: dict, payload: dict) -> tuple[int, str, str, str, str]:
    """一次流式 POST。返回 (status, content, finish_reason, err_text, reasoning)。

    reasoning 单收一路：执行器早就在认它（openai_agent.py:406），这里一直只收 content，
    于是思考模型（实测 glm-5.3 / deepseek-v4-flash）走融合路径全部静默返回空。
    """
    with client.stream("POST", f"{base_url}/chat/completions",
                       headers=headers, json={**payload, "stream": True}) as r:
        if r.status_code >= 400:
            r.read()                                  # 先取回 body 才能读 .text
            return r.status_code, "", "", (r.text or "")[:200], ""
        parts, reasons, finish = [], [], ""
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
                if delta.get("reasoning_content"):
                    reasons.append(delta["reasoning_content"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        return 200, "".join(parts), finish, "", "".join(reasons)


def _call_model(prompt: str, model: str, max_tokens: int = 2000) -> str:
    """调用单个模型（用于合成/盲评）。未知模型 / 缺 key → 返回 ""。

    流式（QIDIAN_STREAM=0 可退回非流式）：停滞超过 QIDIAN_STALL_TIMEOUT 秒就断开。
    """
    env_var, base_url = _resolve_api(model)
    api_key = os.environ.get(env_var, "")
    if not api_key:
        witness.warn('execution_judge', f'no_key:{model}:{env_var}'[:80])
        return ""
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens, "temperature": 0.3}
        with httpx.Client(timeout=httpx.Timeout(240.0, connect=15.0, read=_STALL_TIMEOUT)) as client:
            status, content, finish, err, reasoning = _stream_once(client, base_url, headers, payload)
            if status == 400 and "temperature" in err:
                # 部分模型只接受 temperature=1（实测 kimi-k3：'only 1 is allowed for this model'）
                payload.pop("temperature", None)
                status, content, finish, err, reasoning = _stream_once(client, base_url, headers, payload)
            if status == 200:
                if not content and reasoning and finish != "length":
                    # 模型正常收尾、但把答案落在 reasoning_content 里（执行器同样这么兜）
                    # → 回退。**只有 finish != "length" 才能这么干**：撞上限时 reasoning
                    # 是半截思考、不是答案，当结果返回会误导上层。
                    witness.warn('execution_judge',
                                 f'reasoning_only:{model}:{finish}'[:80])
                    return reasoning
                if not content:
                    # 思考模型把 max_tokens 全烧在 reasoning 上 → content 为空。
                    # 静默返回 "" 会让上层（融合分析/盲评）无声降级，这里显式告警。
                    witness.warn('execution_judge',
                                 f'empty_content:{model}:{finish}'[:80])
                    return ""
                if not finish:
                    # 流结束却没有终止标记 → 多半被连接切断，content 可能是半截。
                    # 实测 v2 定稿就撞过：11111 字断在 JSON 字符串中间，静默返回。
                    witness.warn('execution_judge', f'no_finish:{model}'[:80])
                if finish == "length":
                    witness.warn('execution_judge', f'truncated:{model}'[:80])
                return content
            # 非 200 以前什么都不记，上层只看到空串，查不出原因（kimi-k3 就是这样
            # 静默失败了很久：temperature 不被接受 → 400 → 空串）
            witness.warn('execution_judge',
                         f'http{status}:{model}:{err[:40]}'[:80])
            try:
                from . import api_store
                api_store.note_api_error(model, status, err)  # 欠费 → 标 provider，下轮跳过
            except Exception:
                pass
    except Exception as e:
        witness.warn('execution_judge', f'{e}'[:80])
    return ""


# 融合阶段每份方案的字符上限。曾写死 2000 —— 而方案实际 8k~20k 字，裁判和定稿人
# 只看得到前 ~15%，等于蒙眼合成（实测 brief2 单稿 13114/14433/11873 字）。
# 0 = 不限。40k 覆盖目前所有观测到的方案长度（初稿额度放开后单稿到 31k 字，
# 20k 会砍掉 36%，融合看到的和评委看到的不是同一份东西）。
_FUSION_PLAN_CHARS = int(os.environ.get("QIDIAN_FUSION_PLAN_CHARS", "40000"))

# 融合提示词里「需求」部分的字符上限。曾写死 1500 —— 但生产的架构任务 =
# 角色提示词 + 需求 + 完整 schema（约 3.5k 字），截到 1500 会把 schema 和需求
# 后半段砍掉，融合根本看不到完整需求。0 = 不限。
_FUSION_TASK_CHARS = int(os.environ.get("QIDIAN_FUSION_TASK_CHARS", "4000"))

# 各方案全文的合计上限（新旧路径共用；按 N 均分）。0 = 不限。
# N=2 时 120k//2 = 60k/份，够放下 31k 的单稿；N=4 时降到 30k/份（会截断并告警）。
_FUSION_PLANS_TOTAL = int(os.environ.get("QIDIAN_FUSION_PLANS_TOTAL", "120000"))

# 融合各步的输出上限。实测融合稿要 24k~27k 字（含 schema 必填的 tasks/risks），
# 16000 token 撞顶被腰斩 —— 三题全部截断，且分数和"截断到哪"完美单调。
# v2 的对话/确认步同样吃这个值：它们按需用（实测确认步只出 32 字），
# 但给 2000 会撞顶返回空（warn:empty_content:*:length）。
_FUSION_MAX_TOKENS = int(os.environ.get("QIDIAN_FUSION_MAX_TOKENS", "")
                         or config.MODEL_MAX_TOKENS)

# 定稿输出 schema。旧两阶段和新 v2 共用 —— 单花括号（这里不经过 .format）。
# 字段顺序 = 输出顺序。tasks/risks 是下游拆任务的唯一依据，排前面 —— 实测融合稿
# 被截断过三次，每次丢的都是排在最末尾的它们（brief 3 因此整个 tasks 段为 0 分）。
_ARCH_SCHEMA = """{
  "architecture": "综述 (<500字)",
  "modules": [{"name":"","responsibility":"","depends_on":[],"interfaces":[]}],
  "tasks": [{"id":"","title":"","description":"","complexity":"","layer":"","depends_on":[],"acceptance":""}],
  "risks": [{"risk":"","impact":"","mitigation":""}],
  "data_model": {"database":"","entities":[],"relationships":[]},
  "api_contracts": [{"method":"","path":"","description":"","input":{},"output":{},"errors":[]}],
  "tech_stack": {"language":"","framework":"","database":"","cache":"","mq":""},
  "constraints": [{"type":"","rule":"","check":""}],
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


def _warn_same_model(model: str, members: list[str] | None, role: str) -> None:
    """某个"裁判型"角色若由委员会成员本人担任，等于自己评自己 —— 结论直接作废。

    MAD 论文（EMNLP 2024）明确指出裁判会偏向与自己 backbone 相同的一方。
    同厂没法完全避免（3 家厂商全在委员会里时没有第三方可选），但同模型必须报警。

    以前签名是 `(judge, synth, members)` —— 那是 v1 的两个角色（裁判 + 合成定稿）。
    v2 只有提取员一个这样的位置，synth 恒传空串，所以去掉它、角色名改由调用方给。
    """
    if not members or not model:
        return
    if model in members:
        witness.warn("execution_judge", f"fusion_self_judge:{role}:{model}"[:80])



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

【分歧清单】
{disagreements}

【已有论证】
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

【分歧清单】
{disagreements}

【双方论证】
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

【各成员原稿】
下面是各成员自己的完整方案，供你核对细节、补齐上面结论没覆盖到的字段。
**与上面结论冲突之处一律以结论为准** —— 原稿是素材，不是让你照抄或取并集。
{plans}

要求:
1. 分歧按结论采用对应立场 —— 不折中、不两个都写
2. 只写采纳的独有做法；驳回的一条都不要出现
3. 长度控制在单份方案的 1.1~1.3 倍以内 —— 不取并集、不重复、不堆砌
4. 逐条核对原始需求：modules / data_model / api_contracts / tasks 里的每一项都必须能
   指回需求中的某一条。需求没要求的（哪怕某成员提了、评审也通过了）一律不写进主方案
   —— 确有必要就写进 risks，risk 填「范围外建议：…」，不要混进 modules/data_model/
   api_contracts。实测教训：需求只要求计费，融合稿却继承了成员稿里的支付网关与账本，
   在"需求边界明确"的任务上因此输给更克制的单稿。
5. 顺便生成 test_cases（基于 PRD 成功标准 + API契约 + state_machine）

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


# ② 提取的输出上限。思考模型（实测 glm-5.3）会先把预算烧在 reasoning 上，
# 8000/16000 都撞过（空 content 或截断）—— 提取步骤一空，整条 v2 就废了。
_V2_EXTRACT_MAX_TOKENS = int(os.environ.get("QIDIAN_FUSION_EXTRACT_TOKENS", "")
                             or config.MODEL_MAX_TOKENS)


# v2 ② 提取的默认模型。**必须是非思考模型**：思考模型会把 max_tokens 烧在
# reasoning 上、content 返回空，整条 v2 废掉 —— 实测 glm-5.3 和 deepseek-v4-flash
# 都撞过，而注册表的 reasoning 标注不可靠（v4-flash 标 false，实际会输出思考链）。
# 别改回观察者模型：观察者就是 v4-flash，实测在提取 prompt 上返回空。
_V2_EXTRACT_DEFAULT = os.environ.get("QIDIAN_FUSION_EXTRACT_MODEL", "glm-5.3-flash")

# 提取模型必须**不在委员会里** —— 否则等于选手给自己出题（warn:fusion_self_judge）。
# 阵容是动态的，所以只列备选，运行时挑第一个不在阵容里的。
_V2_EXTRACT_FALLBACKS = ("glm-5.2", "deepseek-v4-pro")


def _v2_extractor_model() -> str:
    """v2 ② 提取用哪个模型：fusion.toml [custom].extract_model > 默认。"""
    cfg = _load_fusion_config().get("custom", {}) or {}
    return cfg.get("extract_model") or _V2_EXTRACT_DEFAULT


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
        witness.warn("execution_judge", f"plans_truncated:{lim}"[:80])
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


def _model_discipline() -> dict:
    """读模型范围纪律表：{model: {"violations": n, "audits": n}}。

    由 tests/integration/coverage_audit.py 累积写入。实测定稿人是「乘法器」还是
    「过滤器」直接决定产物的范围纪律 —— 同一批稿子同一需求，glm 当定稿人时把两家
    的超范围内容都收进来（ledger 23 + RabbitMQ 7），deepseek 当定稿人时连自己的
    RabbitMQ 都砍了（ledger 1 + RabbitMQ 1）。
    """
    try:
        p = config.QIDIAN_DIR / "model_discipline.json"
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _pick_writer(disagreements: list, members: list[str]) -> str:
    """选定稿人：优先历史范围纪律好的，没数据回退「提分歧最多者」。

    纪律 = 违例数 / 审计次数，越低越好。定稿人这个位置决定产物的范围纪律，
    而原来按「谁提分歧多」定 —— 跟纪律无关。
    """
    disc = _model_discipline()
    scored = [(m, (disc.get(m) or {}).get("violations", 0) / max((disc.get(m) or {}).get("audits", 1), 1))
              for m in members if (disc.get(m) or {}).get("audits")]
    if scored:
        return min(scored, key=lambda kv: kv[1])[0]
    return _first_speaker(disagreements, members)


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _demote_bare_accept(items: list) -> list:
    """空口 accept 不算让步 → 降级成 question（对话继续）。

    依据：Not Just RLHF（arXiv 2605.12991）—— 一句"大家都同意了"就能把模型从
    对翻到错 44~98%。让步必须说出被哪条论据说服，否则可能只是被共识信号带跑。
    """
    out = []
    for it in items or []:
        if (isinstance(it, dict) and it.get("verdict") == "accept"
                and not str(it.get("reason") or "").strip()):
            witness.warn("execution_judge", "bare_accept"[:80])
            it = {**it, "verdict": "question"}
        out.append(it)
    return out


def _votes_into(store: dict, who: str, items: list, field: str) -> dict:
    """把 [{id, <field>}] 收进 store[(who, id)]（只留最新一票），返回本轮结果。"""
    cur = {}
    for it in items or []:
        if isinstance(it, dict) and "id" in it:
            store[(who, it["id"])] = it.get(field, "")
            cur[it["id"]] = it.get(field, "")
    return cur


def fuse_architecture_v2(task_desc: str, plans: list[tuple[str, str]],
                         extract_model: str = "") -> str:
    """新融合机制。plans: [(模型名, 方案全文)]。任一步失败返回 ""。

    参数叫 extract_model 不叫 judge_model —— v2 **没有裁判这个角色**，v1 才有。
    提取员兼职了"分辨共识与分歧"这件事，但它就是提取员。
    """
    if len(plans) < 2:
        return plans[0][1] if plans else ""
    members = [m for m, _ in plans]
    extractor = extract_model or _v2_extractor_model()
    if extractor in members:
        alt = next((m for m in _V2_EXTRACT_FALLBACKS if m not in members), "")
        if alt:
            witness.warn("execution_judge",
                         f"extractor_swapped:{extractor}->{alt}"[:80])
            extractor = alt
    _warn_same_model(extractor, members, role="extractor")

    task = task_desc[:_FUSION_TASK_CHARS]
    plans_text = _plans_block(plans)

    # ── ② 提取三类 ──
    # 提取失败必须回退，不能"空着往下走"。三种失败都实测过：
    #   - raw 空（思考模型把 max_tokens 烧在 reasoning 上）→ 实测 glm-5.3 撞过
    #   - try_parse_json 失败返回 {"parse_error": True}（仍是 dict）
    #   - 三样全空 = 没提取到，不是"两家没分歧"
    def _extract_once(model: str):
        raw = _call_model(_V2_EXTRACT.format(n=len(plans), task=task, outputs=plans_text),
                          model, max_tokens=_V2_EXTRACT_MAX_TOKENS)
        d = try_parse_json(raw) if raw else {}
        if not raw or not isinstance(d, dict) or d.get("parse_error"):
            return None
        return d

    deltas = _extract_once(extractor)
    prev = extractor
    # 候选顺序：先"不在委员会里"的（避免选手给自己出题），再退到委员本人。
    # **必须有第二档** —— 否则委员会一缩编，备选表就整个被委员占满，一个候选都不剩。
    # 实测（2026-09-11 真流水线）：智谱欠费 → 委员会只剩 [deepseek-v4-flash,
    # deepseek-v4-pro] → 备选表里的 glm-5.2 是死的、deepseek-v4-pro 又是委员
    # → 无候选 → fusion_v2_extract_failed_all → 整条融合掉到"截断 3000 字"那层。
    # 权衡：自己给自己出题只是质量问题（有 fusion_self_judge 告警），
    # 整条融合失败是**功能没了**，后者严重得多。
    retry_pool = ([m for m in _V2_EXTRACT_FALLBACKS if m not in members and m != extractor]
                  + [m for m in members if m != extractor])
    if deltas is None:
        # 换模型重试。**这一步是兜底的主力**：v2 的失败几乎全在提取（思考模型把额度
        # 烧在 reasoning 上 → 空 content），换个模型大概率就好了。
        for alt in retry_pool:
            # 用 prev 不用 extractor：extractor 是**最初**那个，第二次重试时来源已经不是它了。
            # 日志写错来源 = 排查时按错的方向找（这仓库的老毛病就是日志撒谎）。
            witness.warn("execution_judge", f"extract_retry:{prev}->{alt}"[:80])
            deltas = _extract_once(alt)
            if deltas is not None:
                if alt in members:
                    # 用上委员了 = 自己给自己出题，外面得知道
                    witness.warn("execution_judge",
                                 f"extractor_is_member_fallback:{alt}"[:80])
                break
            prev = alt
    if deltas is None:
        witness.warn("execution_judge", "fusion_v2_extract_failed_all"[:80])
        return ""
    disagreements = [d for d in (deltas.get("disagreements") or []) if isinstance(d, dict)]
    gains = [g for g in (deltas.get("unique_gains") or []) if isinstance(g, dict)]
    consensus = deltas.get("consensus") or []
    if not (consensus or disagreements or gains):
        witness.warn("execution_judge", "fusion_v2_empty_extract"[:80])
        return ""

    writer = _pick_writer(disagreements, members)
    others = [m for m in members if m != writer]

    # ── ③ 共享对话 ──
    # 轮 1 发言方陈述 → 其余成员逐条回应 → 有 insist 才让发言方确认，交替往复。
    # 终止三选一：全 accept / 发言方全 agree（有人让步才算结论）/ 复读 / 撞轮数上限。
    # 只认「有人让步」是刻意的 —— 否则发言方一句 question 就终局，多给的轮次是死代码。
    # 空口 accept（没写理由）先降级成 question，见 _demote_bare_accept。
    max_rounds = max(2, int(os.environ.get("QIDIAN_FUSION_V2_ROUNDS", "5")))
    transcript = []
    resp_votes, conf_votes, gain_votes = {}, {}, {}   # (谁, 条目id) → 最新一票

    # 消融开关（实验用）：不跑辩论，提取完直接定稿。
    # **副作用必须知道**：不投票 ⇒ unique_gains 全部落选（采纳门槛是"全体 adopt"，
    # 空票不算 adopt）。所以这条路径不是"少辩论"，是"只保留共识 + 分歧默认判给发言方"。
    # 拿它跟全量跑配对比较，才答得了"辩论到底值不值"。
    _skip_debate = os.environ.get("QIDIAN_FUSION_V2_NO_DEBATE") == "1"
    if _skip_debate and (disagreements or gains):
        witness.warn("execution_judge", "fusion_v2_debate_skipped"[:80])

    if (disagreements or gains) and not _skip_debate:
        d_json, g_json = _j(disagreements), _j(gains)
        a1 = try_parse_json(_call_model(
            _V2_ROUND1.format(speaker=writer, task=task, outputs=plans_text,
                              disagreements=d_json, unique_gains=g_json),
            writer, max_tokens=_FUSION_MAX_TOKENS) or "") or {}
        transcript.append(f"[{writer} 陈述]\n{_j(a1)}")
        _votes_into(gain_votes, writer, a1.get("unique_gains"), "stance")
        rounds, prev = 1, {}

        while rounds < max_rounds:
            def _respond(m):
                r = _call_model(_V2_ROUND2.format(
                    speaker=m, task=task, outputs=plans_text, disagreements=d_json,
                    transcript="\n\n".join(transcript), unique_gains=g_json),
                    m, max_tokens=_FUSION_MAX_TOKENS)
                return m, (try_parse_json(r) if r else {})

            cur = {}
            for m, a in _parallel([lambda m=m: _respond(m) for m in others]):
                a = a if isinstance(a, dict) else {}
                if a.get("parse_error"):
                    # 解析失败 = 这家的票全丢 → 该分歧点默认判给发言方。不吭声就查不出来。
                    witness.warn("execution_judge", f"fusion_round2_json:{m}"[:80])
                transcript.append(f"[{m} 回应]\n{_j(a)}")
                cur.update(_votes_into(resp_votes, m,
                                       _demote_bare_accept(a.get("responses")), "verdict"))
                _votes_into(gain_votes, m, a.get("unique_gains"), "stance")
            rounds += 1
            if cur == prev:
                break                       # 复读机 → 再辩也没新信息，别烧 token
            prev = dict(cur)
            if all(v == "accept" for v in cur.values()):
                break                       # 全 accept（且都带理由）→ 收敛
            if rounds >= max_rounds:
                break

            c = try_parse_json(_call_model(
                _V2_ROUND3.format(speaker=writer, task=task, disagreements=d_json,
                                  transcript="\n\n".join(transcript)),
                writer, max_tokens=_FUSION_MAX_TOKENS) or "") or {}
            transcript.append(f"[{writer} 确认]\n{_j(c)}")
            cur_c = _votes_into(conf_votes, writer, c.get("confirms"), "verdict")
            rounds += 1
            if not any(v == "question" for v in cur_c.values()):
                break                       # 发言方全认了 → 收敛

    # 撞上限仍有 question 的点 → 按发言方处理，但别让它静默通过
    stuck = [i for (w, i), v in conf_votes.items() if v == "question"]
    if stuck:
        witness.warn("execution_judge", f"fusion_stuck:{len(stuck)}"[:80])

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
        adopted=_j(adopted), rejected=_j(rejected), schema=_ARCH_SCHEMA,
        # 原文必须给。只看「提取员转述」出来的结论，定稿人会凭空丢字段 ——
        # tasks/risks 就是这么整段丢过（提取员没提，它就真不写）。转述丢的
        # 东西定稿人补不回来，因为它根本没看到原稿。
        plans=_plans_block(plans))
    draft = _call_model(final_prompt, writer, max_tokens=_FUSION_MAX_TOKENS)
    if not draft:
        return ""

    for attempt in range(2):
        def _check(m):
            c = _call_model(_V2_CONFIRM.format(checker=m, writer=writer, task=task, draft=draft),
                            m, max_tokens=_FUSION_MAX_TOKENS)
            if not c:
                witness.warn("execution_judge", f"fusion_confirm_empty:{m}"[:80])
                return None
            p = try_parse_json(c)
            if not isinstance(p, dict) or p.get("parse_error"):
                witness.warn("execution_judge", f"fusion_confirm_json:{m}"[:80])
                return None
            return p
        issues = []
        for p in _parallel([lambda m=m: _check(m) for m in others]):
            if p is None or p.get("approved", True):
                continue              # 拿不到结论按"没意见"处理，但上面已告警
            issues += [str(i) for i in (p.get("issues") or [])] or ["(未给出具体问题)"]
        if not issues or attempt:
            break
        draft = _call_model(
            final_prompt + "\n\n【上一稿被指出的问题，请修正】\n" + "\n".join(issues),
            writer, max_tokens=_FUSION_MAX_TOKENS) or draft
    return draft


def decompose_architecture(arch_json: dict) -> list[dict]:
    """拆解器: 把 unified_architecture.tasks 转成可执行 task 列表。

    输入: fuse_architecture_v2 输出的 unified_architecture JSON
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
