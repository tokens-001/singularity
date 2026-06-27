"""Step 1 验证: 用真实产品方案测试 system_architect 角色。

用法:
  cd /Users/jingzhe/奇点
  python3 tests/test_scheduler/test_system_architect.py          # 跑完整测试
  python3 tests/test_scheduler/test_system_architect.py --dry    # 只保存prompt不调LLM
  python3 tests/test_scheduler/test_system_architect.py --claude # 用本地claude CLI
"""
import sys, json, os, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from singularity.scheduler.roles import get_role

# ── 产品方案: 团队代码片段管理器 ──
PRODUCT_BRIEF = {
    "description": "团队代码片段管理器 (CodeSnippetHub)",
    "scope": """
一个 Web 应用，让团队成员保存、搜索、分享代码片段。
核心功能:
1. 用户注册/登录 (OAuth2: GitHub/Google)
2. 创建/编辑/删除代码片段 (语法高亮, 多语言支持)
3. 标签 + 全文搜索 (基于 PG 全文检索)
4. 团队空间: 创建 team, 邀请成员, 共享片段
5. 片段收藏 + 评论
6. REST API + Web 前端

非功能需求:
- 支持 100 并发用户
- 搜索响应 <200ms
- 代码片段支持 50+ 语言语法高亮
""",
    "constraints": "必须用 PostgreSQL 做主库。部署用 Docker Compose。技术栈倾向 Python/TypeScript。",
    "research": """可借鉴方案:
1. GitHub Gist — 公开/私密 Gist, 版本历史
2. GitLab Snippets — 嵌入 GitLab 项目, 支持多文件
3. Pastebin — 简单粘贴分享, 过期机制
4. SnippetStore (开源) — 本地优先, 标签管理
5. MassCode — 多语言高亮, 文件夹组织

关键发现:
- 语法高亮: highlight.js (前端) 或 Pygments (后端) 是标准方案
- 搜索: PostgreSQL tsvector 在 10万条以下够用，超过用 ES
- 团队: RBAC (owner/admin/member) 是常见模式
""",
}

# ── 组装 prompt ──
role = get_role("system_architect")
if not role:
    print("ERROR: system_architect role not found")
    sys.exit(1)

prompt = role.get_full_prompt() + "\n\n---\n\n"
prompt += f"## 项目需求\n{PRODUCT_BRIEF['description']}\n\n"
prompt += f"## 项目范围\n{PRODUCT_BRIEF['scope']}\n\n"
prompt += f"## 约束\n{PRODUCT_BRIEF['constraints']}\n\n"
prompt += f"## 调研报告\n{PRODUCT_BRIEF['research']}\n\n"
prompt += "请输出完整架构方案 (JSON)，用 ```json ... ``` 包裹。"

out_dir = "/tmp/system-architect-test"
os.makedirs(out_dir, exist_ok=True)
with open(f"{out_dir}/prompt.md", "w") as f:
    f.write(prompt)

print(f"Prompt 已保存: {out_dir}/prompt.md ({len(prompt)} chars)")
print(f"\n{'='*60}")
print("System Architect Prompt (前 800 字):")
print(prompt[:800])
print("...")
print(f"{'='*60}")

# ── 执行 ──
mode = sys.argv[1] if len(sys.argv) > 1 else ""

if mode == "--dry":
    print("\n[Dry run] 只保存 prompt，跳过 LLM 调用。")
    sys.exit(0)

result_text = ""

if mode == "--claude":
    # 用本地 claude CLI
    print("\n调用本地 claude CLI ...")
    try:
        r = subprocess.run(
            ["/Users/jingzhe/.claude/local/claude", "--exclude-dynamic-system-prompt-sections", "-p", prompt],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "")},
        )
        result_text = r.stdout
        if r.returncode != 0:
            result_text += f"\n[stderr]: {r.stderr[:500]}"
    except Exception as e:
        result_text = f"CLI 调用失败: {e}"
else:
    # 用 dispatch 系统
    from singularity.scheduler import dispatcher as disp_mod
    agents = disp_mod.load_agents()
    d_list = [a for a in agents.get("D", []) if a.get("model") == "glm-5.2"]
    if not d_list:
        d_list = agents.get("D", [])
    d_agents = {"D": d_list[:1]}

    print(f"D agent: {d_list[0].get('model','?') if d_list else 'none'}")
    print("调度中 (D层,单模型)...")

    try:
        result = disp_mod.dispatch(
            prompt, "D", "test_system_architect_v1",
            d_agents,
            cwd=os.path.join(os.path.dirname(__file__), '..', '..'),
        )
        result_text = result.executor_result.raw_output if result and result.executor_result else "无输出"
    except Exception as e:
        result_text = f"dispatch 失败: {e}"

# ── 保存输出 ──
print(f"\n{'='*60}")
print(f"输出 ({len(result_text)} chars):")
print(result_text[:3000])
print(f"{'='*60}")

with open(f"{out_dir}/output.md", "w") as f:
    f.write(f"# System Architect Test Output\n\n")
    f.write(f"## Prompt\n\n```\n{prompt[:3000]}\n...\n```\n\n")
    f.write(f"## Raw Output\n\n```\n{result_text}\n```\n")
print(f"\n结果已保存到 {out_dir}/")
