"""Step 4 验证: E层4工程师角色+layer路由。

用法: cd /Users/jingzhe/奇点 && python3 tests/test_scheduler/test_step4_execution.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from singularity.scheduler.roles import get_role, registry

# ── 1. 验证角色加载 ──
print("1. 4个E层工程师角色:")
for role_key in ["frontend_engineer", "backend_engineer", "data_engineer", "devops_engineer"]:
    role = get_role(role_key)
    assert role, f"{role_key} 未找到"
    ra = registry.get_assignment(role_key)
    agent = registry.get_active_agent(role_key)
    print(f"  {role_key}: level={role.level}, agent={agent.name if agent else '?'}, "
          f"prompt={len(role.system_prompt)} chars")

# ── 2. 验证 layer→role 映射 ──
print("\n2. Layer→Role 路由映射:")
LAYER_ROLE_MAP = {
    "frontend": ("E", "frontend_engineer"),
    "backend": ("E", "backend_engineer"),
    "data": ("E+", "data_engineer"),
    "devops": ("E", "devops_engineer"),
}
for layer, (level, role_key) in LAYER_ROLE_MAP.items():
    role = get_role(role_key)
    assert role.level == level, f"{role_key} level should be {level}"
    print(f"  {layer} → {role_key} ({level}) ✓")

# ── 3. 验证 skill 文件 ──
print("\n3. Skill 文件检查:")
skill_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'singularity', 'skills')
for name in ["frontend-engineer", "backend-engineer", "data-engineer", "devops-engineer"]:
    path = os.path.join(skill_dir, name, "SKILL.md")
    assert os.path.exists(path), f"Missing: {path}"
    size = os.path.getsize(path)
    print(f"  {name}/SKILL.md: {size} bytes ✓")

# ── 4. 验证角色边界 ──
print("\n4. E层角色边界检查:")
for role_key in ["frontend_engineer", "backend_engineer", "data_engineer", "devops_engineer"]:
    role = get_role(role_key)
    prompt = role.system_prompt
    assert "照规格施工" in prompt, f"{role_key} 缺'照规格施工'"
    assert "不越界" in prompt or "不改" in prompt, f"{role_key} 缺边界约束"
    assert "architecture_issues" in prompt, f"{role_key} 缺架构问题上报"
    print(f"  {role_key}: 边界约束 ✓")

print("\nStep 4 全部通过")
