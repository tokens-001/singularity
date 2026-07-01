"""Step 5 验证: QA+安全审计师角色 + 验收流程。

用法: cd /Users/jingzhe/奇点 && python3 tests/test_scheduler/test_step5_verification.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from singularity.scheduler.roles import get_role, registry

# ── 1. 验证 QA+安全审计师角色 ──
print("1. 验收层角色:")
for role_key in ["qa_engineer", "security_auditor"]:
    role = get_role(role_key)
    assert role, f"{role_key} 未找到"
    # 两档后 role 不再绑定 level, 统一 any
    ra = registry.get_assignment(role_key)
    agent = registry.get_active_agent(role_key)
    print(f"  {role_key}: agent={agent.name if agent else '?'}, "
          f"prompt={len(role.system_prompt)} chars")

# ── 2. 验证输出schema ──
print("\n2. 输出schema检查:")
qa = get_role("qa_engineer")
sec = get_role("security_auditor")

qa_required = ["verification", "test_coverage", "regression", "summary"]
for field in qa_required:
    assert field in qa.system_prompt, f"QA 缺输出字段: {field}"
print(f"  QA: {qa_required} ✓")

sec_required = ["findings", "dependency_scan", "compliance", "summary"]
for field in sec_required:
    assert field in sec.system_prompt, f"安全审计师 缺输出字段: {field}"
print(f"  安全审计师: {sec_required} ✓")

# ── 3. 验证边界 ──
print("\n3. 边界检查:")
assert "不写代码" in qa.system_prompt and "只出报告" in qa.system_prompt
print("  QA: 不写代码·只出报告 ✓")
assert "不修代码" in sec.system_prompt and "只出报告" in sec.system_prompt
print("  安全审计师: 不修代码·只出报告 ✓")

# ── 4. 验证 workflow._run_verification 存在 ──
print("\n4. Workflow 集成:")
from singularity.scheduler.workflow import _run_verification
assert callable(_run_verification)
print("  _run_verification() ✓")

# ── 5. 验证 skill 文件 ──
print("\n5. Skill 文件:")
skill_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'singularity', 'skills')
for name in ["qa-engineer", "security-auditor"]:
    path = os.path.join(skill_dir, name, "SKILL.md")
    assert os.path.exists(path), f"Missing: {path}"
    size = os.path.getsize(path)
    print(f"  {name}/SKILL.md: {size} bytes ✓")

print("\nStep 5 全部通过")
