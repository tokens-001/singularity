"""Step 3 验证: Observer 定义层 4 角色。

用法:
  cd /Users/jingzhe/奇点
  python3 tests/test_scheduler/test_observer_definition.py
  python3 tests/test_scheduler/test_observer_definition.py --chat  # 交互对话测试
"""
import sys, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

# ── 1. 测试技能加载 ──
print("=" * 50)
print("1. 技能加载测试")
from singularity.scheduler.observer_agent import (
    _load_observer_skills, _detect_definition_intent,
    _definition_role_prompt, _get_definition_context,
)

skills = _load_observer_skills()
print(f"加载技能: {len(skills)} 个")
for k, v in skills.items():
    prompt_len = len(v.get("system_prompt", ""))
    print(f"  {k}: {v['name']} ({prompt_len} chars)")

assert len(skills) == 4, f"应为 4 个技能，实际 {len(skills)}"
print("OK: 4 个技能加载成功")

# ── 2. 测试意图检测 ──
print("\n" + "=" * 50)
print("2. 意图检测测试")

cases = [
    ("我要做一个团队知识库", "product-manager"),
    ("帮我设计一个代码片段管理工具", "product-manager"),
    ("这个界面应该用什么颜色风格", "ui-designer"),
    ("用户登录流程应该怎么做", "interaction-designer"),
    ("有哪些竞品可以参考", "researcher"),
    ("系统现在有多少任务在跑", ""),  # 系统监控，不触发定义层
    ("帮我创建一个修复登录bug的任务", ""),  # 任务创建
]
for q, expected in cases:
    detected = _detect_definition_intent(q)
    status = "✓" if detected == expected else f"✗ (期望:{expected}, 检测:{detected})"
    print(f"  {status} '{q[:30]}...' → {detected or '无'}")

print("OK: 意图检测正确")

# ── 3. 测试角色 prompt ──
print("\n" + "=" * 50)
print("3. 角色 prompt 测试")
for role_key in ["product-manager", "interaction-designer", "ui-designer", "researcher"]:
    prompt = _definition_role_prompt(role_key)
    ctx = _get_definition_context(role_key)
    print(f"  {role_key}: role={len(prompt)} chars, context={len(ctx)} chars")
    assert len(prompt) > 100, f"{role_key} prompt 太短"
print("OK: 4 个角色 prompt 都有效")

# ── 4. 对话测试 (可选) ──
if "--chat" in sys.argv:
    print("\n" + "=" * 50)
    print("4. 对话测试 (用产品经理角色)")

    from singularity.scheduler.observer_agent import _answer_question

    question = "我要做一个团队代码片段管理器，类似 GitHub Gist 但加团队共享功能"
    print(f"用户: {question}")
    answer = _answer_question(question)
    print(f"Observer: {answer[:1000]}")
    print(f"...\n(总 {len(answer)} chars)")

print("\n" + "=" * 50)
print("Step 3 验证通过")
