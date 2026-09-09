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
    def fake(prompt, model, max_tokens=2000):
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
    monkeypatch.setattr(ej, "_call_model", lambda p, m, max_tokens=2000: "不是 JSON")
    assert ej.fuse_architecture_v2("需求", PLANS) == ""


def test_empty_extract_returns_empty(monkeypatch):
    """提取返回空 → 必须回退，不能拿空议程去定稿。

    实测踩过：glm-5.3 把 8000 token 烧在 reasoning 上返回空 content，
    旧代码当成"两家没分歧"，跳过整段对话直接定稿，写出 25 分的稿。
    """
    calls = []

    def fake(prompt, model, max_tokens=2000):
        calls.append(prompt)
        return "" if "架构委员会秘书" in prompt else "最终稿"

    monkeypatch.setattr(ej, "_call_model", fake)
    assert ej.fuse_architecture_v2("需求", PLANS) == ""
    assert len(calls) == 1                    # 提取失败就停，没有往下走


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


def test_legacy_fuse_also_respects_total_cap(monkeypatch):
    """旧两阶段路径共用同一个合计上限 —— 否则默认路径仍会顶爆上下文。"""
    monkeypatch.setattr(ej, "_FUSION_PLAN_CHARS", 20000)
    monkeypatch.setattr(ej, "_FUSION_PLANS_TOTAL", 200)
    seen = []
    monkeypatch.setattr(ej, "_call_model",
                        lambda p, m, max_tokens=2000: (seen.append(p), "{}")[1])
    ej.fuse_architecture("需求", ["x" * 1000, "y" * 1000])
    assert seen[0].count("x") == 100 and seen[0].count("y") == 100
    assert "[模型1]" in seen[0]           # 标签没变


def test_single_plan_passthrough(monkeypatch):
    monkeypatch.setattr(ej, "_call_model", lambda p, m, max_tokens=2000: "不该被调用")
    assert ej.fuse_architecture_v2("需求", [("A", "唯一方案")]) == "唯一方案"


# ── 定稿确认 ──────────────────────────────────────────────

def test_confirm_issues_triggers_one_rewrite(monkeypatch):
    calls = []
    state = {"n": 0}

    def fake(prompt, model, max_tokens=2000):
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
    monkeypatch.setattr(ej.witness, "heartbeat", lambda *a, **k: warns.append(a))
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
    monkeypatch.setattr(ej.witness, "heartbeat", lambda *a, **k: warns.append(a))

    def fake(prompt, model, max_tokens=2000):
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


# ── 旧路径 prompt 仍然可格式化（schema 抽出去别抽坏了）────

def test_stage2_prompt_formats():
    p = ej._ARCH_FUSION_STAGE2.format(
        task="t", analysis="a", outputs="o", schema=ej._ARCH_SCHEMA)
    assert '"test_cases"' in p and "{schema}" not in p


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


# ── 提取模型不能是委员本人 ────────────────────────────────

def test_extractor_swapped_when_it_is_a_member(monkeypatch):
    calls = []
    _stub(monkeypatch, calls=calls)
    monkeypatch.setattr(ej, "_v2_extractor_model", lambda: "A")   # A 是委员
    ej.fuse_architecture_v2("需求", PLANS)
    used = next(m for m, p in calls if "架构委员会秘书" in p)
    assert used in ej._V2_EXTRACT_FALLBACKS and used not in ("A", "B")
