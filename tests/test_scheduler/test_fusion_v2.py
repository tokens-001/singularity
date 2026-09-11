"""融合机制 v2 单元测试（docs/融合机制重设计.md）。

_call_model 全部打桩 —— 测的是流程和裁决规则，不是模型。
"""

import json
import pytest

from singularity.scheduler import execution_judge as ej


def _j(obj):
    return "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"


PLANS = [("A", "方案A全文"), ("B", "方案B全文")]

EXTRACT = {
    "consensus": ["都用 PostgreSQL"],
    "disagreements": [
        {"id": 1, "dimension": "api_contracts", "point": "金额怎么存",
         "positions": {"A": "bigint 分", "B": "decimal 字符串"}, "raised_by": "A"},
        {"id": 2, "dimension": "modules", "point": "拆不拆 billing",
         "positions": {"A": "拆", "B": "不拆"}, "raised_by": "A"},
    ],
    "unique_gains": [
        {"id": 1, "content": "幂等键", "from": "B", "impact": "重试安全"},
    ],
}

R2_INSIST_ACCEPT = {
    "responses": [
        {"id": 1, "verdict": "insist", "reason": "浮点精度你误判了"},
        {"id": 2, "verdict": "accept", "reason": "你说得对"},
    ],
    "unique_gains": [{"id": 1, "stance": "adopt", "reason": "有用"}],
}


def _stub(monkeypatch, calls=None, **over):
    """给 _call_model 打桩。over 可覆盖各步返回值。"""
    def fake(prompt, model, max_tokens=2000, project_id=""):
        if calls is not None:
            calls.append((model, prompt))
        if "架构委员会秘书" in prompt:
            return _j(over.get("extract", EXTRACT))
        if "陈述己方理由" in prompt:
            return _j(over.get("r1", {"arguments": [{"id": 1, "reason": "整数不会丢精度"}],
                                      "unique_gains": [{"id": 1, "stance": "adopt", "reason": "好"}]}))
        if "逐条回应" in prompt:
            return _j(over.get("r2", R2_INSIST_ACCEPT))
        if "对你的论证给出了回应" in prompt:
            return _j(over.get("r3", {"confirms": [{"id": 1, "verdict": "agree", "reason": "确实"}]}))
        if "架构定稿人" in prompt:
            return over.get("draft", "最终稿")
        if "检查三件事" in prompt:
            return _j(over.get("confirm", {"approved": True, "issues": []}))
        raise AssertionError("未打桩的 prompt: " + prompt[:60])
    monkeypatch.setattr(ej, "_call_model", fake)


def _finalize_prompt(calls):
    return next(p for m, p in calls if "架构定稿人" in p)


def test_pick_writer_prefers_disciplined_model(tmp_path, monkeypatch):
    """定稿人按历史范围纪律选 —— 实测定稿人决定产物的范围纪律（乘法器 vs 过滤器）。"""
    from singularity.scheduler import config, execution_judge as ej
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    (tmp_path / "model_discipline.json").write_text(json.dumps({
        "noisy": {"violations": 10, "audits": 2},   # 5.0 处/次
        "clean": {"violations": 1, "audits": 2},    # 0.5 处/次
    }))
    assert ej._pick_writer([], ["noisy", "clean"]) == "clean"


def test_pick_writer_falls_back_without_data(tmp_path, monkeypatch):
    """没有审计数据时回退到原规则（提分歧最多者），不改变既有行为。"""
    from singularity.scheduler import config, execution_judge as ej
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    d = [{"raised_by": "b"}, {"raised_by": "b"}]
    assert ej._pick_writer(d, ["a", "b"]) == "b"


def test_finalize_prompt_carries_original_plans(monkeypatch):
    """定稿人必须拿到成员原稿。

    只看「提取员转述」出来的共识/分歧结论，定稿人会凭空丢字段 —— tasks/risks
    就这么整段丢过（提取员没提，它就真不写）。转述漏的东西定稿人补不回来，
    因为它根本没看过原稿。
    """
    calls = []
    _stub(monkeypatch, calls=calls)
    ej.fuse_architecture_v2("需求", PLANS)
    final = _finalize_prompt(calls)
    assert "方案A全文" in final
    assert "方案B全文" in final


# ── 裁决规则 ──────────────────────────────────────────────

def test_first_speaker_picks_most_disagreements(monkeypatch):
    assert ej._first_speaker(EXTRACT["disagreements"], ["A", "B"]) == "A"


def test_first_speaker_tie_break_by_member_order():
    ds = [{"raised_by": "B"}, {"raised_by": "A"}]
    assert ej._first_speaker(ds, ["A", "B"]) == "A"
    assert ej._first_speaker([], ["A", "B"]) == "A"


def test_insist_then_agree_gives_point_to_responder(monkeypatch):
    calls = []
    _stub(monkeypatch, calls=calls)
    out = ej.fuse_architecture_v2("需求", PLANS)
    assert out == "最终稿"
    p = _finalize_prompt(calls)
    assert '"winner": "B"' in p        # insist + 发言方 agree → 对方胜
    assert '"winner": "A"' in p        # accept → 发言方胜
    assert '"id": 1' in p and "幂等键" in p   # 全票 adopt → 进采纳清单


def test_insist_then_question_keeps_writers_view(monkeypatch):
    calls = []
    _stub(monkeypatch, calls=calls, r3={"confirms": [{"id": 1, "verdict": "question"}]})
    ej.fuse_architecture_v2("需求", PLANS)
    p = _finalize_prompt(calls)
    # 分歧 1 归发言方 A；分歧 2 仍是 A → 两条都是 A
    assert p.count('"winner": "A"') == 2


def test_repeat_round_stops_ping_pong(monkeypatch):
    """双方都死扛 → 复读即停，别烧到轮数上限。"""
    calls = []
    _stub(monkeypatch, calls=calls, r3={"confirms": [{"id": 1, "verdict": "question"}]})
    ej.fuse_architecture_v2("需求", PLANS)
    assert len([1 for _, p in calls if "逐条回应" in p]) == 2   # R2 只跑两轮


def test_gain_rejected_if_any_side_rejects(monkeypatch):
    calls = []
    _stub(monkeypatch, calls=calls,
          r2={**R2_INSIST_ACCEPT,
              "unique_gains": [{"id": 1, "stance": "reject", "reason": "过度设计"}]})
    ej.fuse_architecture_v2("需求", PLANS)
    p = _finalize_prompt(calls)
    adopted = p.split("【采纳的独有做法】")[1].split("【已驳回")[0]
    assert "幂等键" not in adopted


# ── 失败与回退 ────────────────────────────────────────────

def test_parse_error_returns_empty(monkeypatch):
    monkeypatch.setattr(ej, "_call_model", lambda p, m, max_tokens=2000, project_id="": "不是 JSON")
    assert ej.fuse_architecture_v2("需求", PLANS) == ""


def test_empty_extract_retries_then_gives_up(monkeypatch):
    """提取返回空 → 换模型逐个重试，全试完才放弃。

    实测踩过：glm-5.3 把 8000 token 烧在 reasoning 上返回空 content，
    旧代码当成"两家没分歧"，跳过整段对话直接定稿，写出 25 分的稿。

    2026-09-11 起失败会换 `_V2_EXTRACT_FALLBACKS` 里的模型重试 —— 提取是 v2 唯一的
    早退点，重试比"回退旧两阶段"划算得多（后者已被证明从未触发、且本身有致命缺陷）。
    """
    calls = []

    def fake(prompt, model, max_tokens=2000, project_id=""):
        calls.append((model, prompt))
        return "" if "架构委员会秘书" in prompt else "最终稿"

    monkeypatch.setattr(ej, "_call_model", fake)
    assert ej.fuse_architecture_v2("需求", PLANS) == ""
    tried = [m for m, p in calls if "架构委员会秘书" in p]
    # 首次 + 备选表（都不在委员会里）+ 委员本人（垫底的最后手段）
    assert tried[1:1 + len(ej._V2_EXTRACT_FALLBACKS)] == list(ej._V2_EXTRACT_FALLBACKS)
    assert tried[1 + len(ej._V2_EXTRACT_FALLBACKS):] == ["A", "B"]
    assert len(set(tried)) == len(tried)                     # 同一个模型不重复试


def test_extract_retry_recovers(monkeypatch):
    """换模型重试成功 → 整条 v2 正常往下走，不该失败。

    这是删掉旧两阶段之后 v2 的**唯一兜底**，必须真的能救回来。
    """
    calls = []

    def fake(prompt, model, max_tokens=2000, project_id=""):
        calls.append((model, prompt))
        if "架构委员会秘书" in prompt:
            if model != "deepseek-v4-pro":
                return ""                       # 前两个提取模型返回空
            return json.dumps({"consensus": ["都同意模块划分"],
                               "disagreements": [], "unique_gains": []})
        return "最终稿"

    monkeypatch.setattr(ej, "_call_model", fake)
    assert ej.fuse_architecture_v2("需求", PLANS) == "最终稿"
    assert [m for m, p in calls if "架构委员会秘书" in p][-1] == "deepseek-v4-pro"


def test_extract_retries_with_member_when_pool_exhausted(monkeypatch):
    """备选表被委员占满时，退到用委员本人当提取员 —— 不能一个候选都没有。

    实测（2026-09-11 真流水线）：智谱欠费 → 委员会只剩 [deepseek-v4-flash,
    deepseek-v4-pro] → 备选表里 glm-5.2 是死的、deepseek-v4-pro 又是委员
    → "不在委员会里"的候选为空 → fusion_v2_extract_failed_all → 整条融合
    掉到"截断 3000 字"那层。自己给自己出题只是质量问题，整条融合失败是功能没了。
    """
    calls = []

    def fake(prompt, model, max_tokens=2000, project_id=""):
        calls.append((model, prompt))
        if "架构委员会秘书" in prompt:
            return "" if model != "deepseek-v4-pro" else json.dumps(
                {"consensus": ["都一致"], "disagreements": [], "unique_gains": []})
        return "最终稿"

    monkeypatch.setattr(ej, "_call_model", fake)
    # 委员就是备选表里的两个模型 → 常规候选为空，必须退到委员本人
    plans = [("glm-5.2", "方案甲"), ("deepseek-v4-pro", "方案乙")]
    assert ej.fuse_architecture_v2("需求", plans, extract_model="glm-5.2") == "最终稿"
    assert "deepseek-v4-pro" in [m for m, p in calls if "架构委员会秘书" in p]


def test_all_empty_extract_returns_empty(monkeypatch):
    _stub(monkeypatch, extract={"consensus": [], "disagreements": [], "unique_gains": []})
    assert ej.fuse_architecture_v2("需求", PLANS) == ""


def test_no_disagreements_skips_dialogue(monkeypatch):
    calls = []
    _stub(monkeypatch, calls=calls,
          extract={"consensus": ["都一致"], "disagreements": [], "unique_gains": []})
    out = ej.fuse_architecture_v2("需求", PLANS)
    assert out == "最终稿"
    kinds = [p[:20] for _, p in calls]
    assert not any("陈述己方理由" in p for p in kinds)
    assert len([1 for _, p in calls if "架构定稿人" in p]) == 1


def test_plans_total_cap_splits_evenly(monkeypatch):
    """N 份合计上限按 N 均分 —— 否则 N=3 写满 20k×3 能顶爆上下文。"""
    monkeypatch.setattr(ej, "_FUSION_PLAN_CHARS", 20000)
    monkeypatch.setattr(ej, "_FUSION_PLANS_TOTAL", 300)
    block = ej._plans_block([("A", "x" * 1000), ("B", "y" * 1000)])
    assert block.count("x") == 150 and block.count("y") == 150


def test_single_plan_passthrough(monkeypatch):
    monkeypatch.setattr(ej, "_call_model", lambda p, m, max_tokens=2000, project_id="": "不该被调用")
    assert ej.fuse_architecture_v2("需求", [("A", "唯一方案")]) == "唯一方案"


# ── 定稿确认 ──────────────────────────────────────────────

def test_confirm_issues_triggers_one_rewrite(monkeypatch):
    calls = []
    state = {"n": 0}

    def fake(prompt, model, max_tokens=2000, project_id=""):
        calls.append((model, prompt))
        if "架构委员会秘书" in prompt:
            return _j(EXTRACT)
        if "陈述己方理由" in prompt:
            return _j({})
        if "逐条回应" in prompt:
            return _j(R2_INSIST_ACCEPT)
        if "对你的论证给出了回应" in prompt:
            return _j({"confirms": [{"id": 1, "verdict": "agree"}]})
        if "架构定稿人" in prompt:
            state["n"] += 1
            return f"稿{state['n']}"
        if "检查三件事" in prompt:
            # 第一次打回，第二次放行
            ok = state["n"] > 1
            return _j({"approved": ok, "issues": [] if ok else ["tasks 段缺失"]})
        raise AssertionError(prompt[:60])

    monkeypatch.setattr(ej, "_call_model", fake)
    out = ej.fuse_architecture_v2("需求", PLANS)
    assert out == "稿2"
    assert state["n"] == 2                     # 只重写一次
    assert any("tasks 段缺失" in p for _, p in calls)   # issues 带进了重写 prompt


# ── _call_model 对思考模型的兜底 ────────────────────────

def _fake_stream(monkeypatch, result):
    monkeypatch.setattr(ej, "_resolve_api", lambda m: ("X_KEY", "https://x/v1"))
    monkeypatch.setenv("X_KEY", "k")
    warns = []
    monkeypatch.setattr(ej.witness, "warn", lambda *a, **k: warns.append(a))
    monkeypatch.setattr(ej, "_stream_once", lambda *a, **k: result)
    return warns


def test_call_model_falls_back_to_reasoning(monkeypatch):
    """内容全落在 reasoning_content 的模型不能静默返回空（实测 v4-flash）。"""
    warns = _fake_stream(monkeypatch, (200, "", "stop", "", "思考内容"))
    assert ej._call_model("p", "m") == "思考内容"
    assert any("reasoning_only" in str(w) for w in warns)


def test_call_model_truly_empty_still_warns(monkeypatch):
    warns = _fake_stream(monkeypatch, (200, "", "length", "", ""))
    assert ej._call_model("p", "m") == ""
    assert any("empty_content" in str(w) for w in warns)


def test_confirm_empty_is_warned(monkeypatch):
    """确认步骤空返回不能静默通过（实测 glm-5.2 返回 0 字）。"""
    warns = []
    monkeypatch.setattr(ej.witness, "warn", lambda *a, **k: warns.append(a))

    def fake(prompt, model, max_tokens=2000, project_id=""):
        if "架构委员会秘书" in prompt:
            return _j(EXTRACT)
        if "陈述己方理由" in prompt:
            return _j({"arguments": [], "unique_gains": []})
        if "逐条回应" in prompt:
            return _j(R2_INSIST_ACCEPT)
        if "对你的论证给出了回应" in prompt:
            return _j({"confirms": [{"id": 1, "verdict": "agree"}]})
        if "架构定稿人" in prompt:
            return "最终稿"
        if "检查三件事" in prompt:
            return ""                       # 空返回
        raise AssertionError(prompt[:60])

    monkeypatch.setattr(ej, "_call_model", fake)
    assert ej.fuse_architecture_v2("需求", PLANS) == "最终稿"
    assert any("fusion_confirm_empty" in str(w) for w in warns)


# ── 空口 accept 不算让步（Not Just RLHF, arXiv 2605.12991）────

def test_demote_bare_accept():
    items = [{"id": 1, "verdict": "accept", "reason": "被精度论证说服"},
             {"id": 2, "verdict": "accept", "reason": "   "},
             {"id": 3, "verdict": "accept"},
             {"id": 4, "verdict": "insist", "reason": "你误判"}]
    out = ej._demote_bare_accept(items)
    assert [x["verdict"] for x in out] == ["accept", "question", "question", "insist"]
    assert out[0]["reason"] == "被精度论证说服"       # 带理由的不动


def test_bare_accept_does_not_converge(monkeypatch):
    """全 accept 但都没理由 → 不收敛，继续辩（旧逻辑会直接终局）。"""
    calls = []
    _stub(monkeypatch, calls=calls,
          r2={"responses": [{"id": 1, "verdict": "accept", "reason": ""},
                            {"id": 2, "verdict": "accept", "reason": ""}],
              "unique_gains": [{"id": 1, "stance": "adopt", "reason": "好"}]})
    ej.fuse_architecture_v2("需求", PLANS)
    assert any("对你的论证给出了回应" in p for _, p in calls)


def test_extractor_stays_member_when_no_usable_alternative(monkeypatch):
    """提取员是委员、而备选表里**没有可用又非委员**的模型 → 不换，就地自己出题。

    **这条原来断言"必须换成 `_V2_EXTRACT_FALLBACKS` 里的某个"**（前提是"备选表
    总是可用"）。2026-09-11 那个前提被证伪了：`_usable` 因为漏 import 抛 NameError
    被 except 吞掉、**从上线起恒返回 True**，所以"换兜底"实际是换到一个用户可能
    已经停用的模型上；而 `_call_model` 的 `_resolve_api` 又不看激活池 → 真去调它。
    现在 `_usable` 同时要求"provider 可用 + **在激活池里**"，换不动就留在委员里
    （`_warn_same_model` 会告警"选手给自己出题"——那是质量问题，比偷偷烧钱轻）。
    换得动的情况见 `TestExtractorRespectsActivePool`。
    """
    calls = []
    _stub(monkeypatch, calls=calls)
    monkeypatch.setattr(ej, "_v2_extractor_model", lambda: "A")   # A 是委员
    ej.fuse_architecture_v2("需求", PLANS)
    used = next(m for m, p in calls if "架构委员会秘书" in p)
    assert used == "A", f"没有可用兜底时不该换走（实际换成 {used}）"


# ── 2026-09-11 外派评审后的三处修补 ──────────────────────

def test_bare_adopt_stance_not_counted(monkeypatch):
    """独有做法的空口 adopt 不算采纳 —— 与分歧票的空口 accept 对称。

    采纳门槛是"全体 adopt"（保守，长度是膨胀主因）。但只有分歧票过了
    `_demote_bare_accept`（它认 `verdict` 字段），独有做法用的是 `stance`，
    一直没过闸 —— 一句不带理由的"同意采纳"就能把条目放行，方向和分歧点相反。
    """
    calls = []
    _stub(monkeypatch, calls=calls,
          r2={**R2_INSIST_ACCEPT,
              "unique_gains": [{"id": 1, "stance": "adopt"}]})   # 无 reason
    ej.fuse_architecture_v2("需求", PLANS)
    p = _finalize_prompt(calls)
    adopted = p.split("【采纳的独有做法】")[1].split("【已驳回")[0]
    assert "幂等键" not in adopted


def test_rulings_out_param_records_the_debate(monkeypatch):
    """裁决记录能被调用方取走（落 fusion_meta）。

    多模型碰撞是这套系统的核心价值主张，但它的证据原来只活在内存里：
    fusion_meta 只存 models/outputs/count，GATE2 的人拿到一份稿子，
    查不到"谁定稿、哪些分歧判给谁、哪些独有做法被驳回"。
    """
    _stub(monkeypatch)
    r = {}
    ej.fuse_architecture_v2("需求", PLANS, rulings=r)
    assert r["writer"] == "A"
    assert r["rounds"] >= 1
    assert {d["id"] for d in r["resolved"]} == {1, 2}
    assert [g["id"] for g in r["adopted"]] == [1]
    assert r["rejected"] == []


def test_rulings_rounds_zero_when_no_debate(monkeypatch):
    """只有共识、没分歧也没独有做法 → 辩论整段不跑，rounds 必须是 0。

    这条钉的是 NameError：`rounds` 原来只在辩论块里绑定，出参要用它，
    不预设默认值的话上面那条路径直接抛异常。
    """
    _stub(monkeypatch,
          extract={"consensus": ["都一致"], "disagreements": [], "unique_gains": []})
    r = {}
    assert ej.fuse_architecture_v2("需求", PLANS, rulings=r) == "最终稿"
    assert r["rounds"] == 0


def test_second_confirm_issues_are_warned(monkeypatch):
    """第二轮确认**仍**有问题 → 稿子照交，但不许静默丢掉。

    原写法 `if not issues or attempt: break` 把第二轮的 issues 直接扔了：
    既不告警也不记录，产物里"改完了"和"没改"长得一样。
    """
    warns = []
    monkeypatch.setattr(ej.witness, "warn", lambda *a, **k: warns.append(a))
    _stub(monkeypatch, confirm={"approved": False, "issues": ["tasks 段缺字段"]})
    ej.fuse_architecture_v2("需求", PLANS)
    assert any("fusion_confirm_unresolved" in str(w) for w in warns)


class TestExtractorRespectsActivePool:
    """提取员换兜底时必须认**激活池** —— 被用户停用的模型不能调。

    **两个坑叠在一起**（2026-09-11 实测）：
      ① `_usable` 里**只 import 了 `api_store` 却用了 `model_registry`** → 每次抛
         NameError → 被 `except` 吞掉 → 恒返回"可用"。**这个函数从上线起就没生效过**
         （同一个形状：嵌入路径 `SentenceTransformer` 没 import，也是被 except 吞掉）。
      ② `_call_model` 走的 `_resolve_api` **只看 provider + api_store，
         完全不看激活池**（本模块 `_disabled` 出现 0 次）。

    叠加后果：池里只剩两个便宜模型时（planning 用满两个 ⇒ 提取员必是委员），
    每次融合都换到 `glm-5.2` 并**真的发起调用** —— 用户为省钱停掉的模型一直在烧。
    """

    def _run(self, monkeypatch, pool, members, extract_model):
        import singularity.scheduler.dispatcher as disp
        monkeypatch.setattr(disp, "load_agents",
                            lambda: {"any": [{"model": m} for m in pool]})
        calls = []
        def fake(prompt, model, max_tokens=2000, project_id=""):
            calls.append((model, "架构委员会秘书" in prompt))
            if "架构委员会秘书" in prompt:
                return _j({"consensus": ["都同意"], "disagreements": [], "unique_gains": []})
            if "架构定稿人" in prompt:
                return "最终稿"
            return _j({})
        monkeypatch.setattr(ej, "_call_model", fake)
        monkeypatch.setattr(ej.witness, "warn", lambda *a, **k: None)
        ej.fuse_architecture_v2("需求", members, extract_model=extract_model)
        return calls

    def test_no_swap_to_model_outside_pool(self, monkeypatch):
        """兜底表里的模型不在激活池 → **不能**换过去，提取员留在委员里。"""
        calls = self._run(monkeypatch, pool=["m-a", "m-b"],
                          members=[("m-a", "A"), ("m-b", "B")], extract_model="m-a")
        used = {m for m, _ in calls}
        assert used <= {"m-a", "m-b"}, f"调了池外的模型: {used}"

    def test_swap_still_works_when_fallback_is_in_pool(self, monkeypatch):
        """兜底表里有个**真在池里**的非委员 → 该换还得换（别把功能关死）。"""
        monkeypatch.setattr(ej, "_V2_EXTRACT_FALLBACKS", ("m-c",))
        calls = self._run(monkeypatch, pool=["m-a", "m-b", "m-c"],
                          members=[("m-a", "A"), ("m-b", "B")], extract_model="m-a")
        extractor_calls = {m for m, is_ex in calls if is_ex}
        assert extractor_calls == {"m-c"}, f"该换成 m-c，实际 {extractor_calls}"
