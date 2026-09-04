"""Step 2 验证: 3模型并行出架构方案 → fuse_architecture 合成 → 对比单模型版。

用法:
  cd /Users/jingzhe/奇点
  python3 tests/integration/test_step2_fusion.py
"""
import sys, json, os, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from singularity.scheduler.roles import get_role
from singularity.scheduler._io import try_parse_json

# ── 产品方案 (与 Step 1 相同) ──
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
5. MassCode — 多语言高亮, 文件夹组织""",
}

# ── 组装基础 prompt ──
role = get_role("system_architect")
if not role:
    print("ERROR: system_architect role not found"); sys.exit(1)

base_prompt = role.system_prompt  # 不含 persona 前缀
base_prompt += "\n\n---\n\n"
base_prompt += f"## 项目需求\n{PRODUCT_BRIEF['description']}\n\n"
base_prompt += f"## 项目范围\n{PRODUCT_BRIEF['scope']}\n\n"
base_prompt += f"## 约束\n{PRODUCT_BRIEF['constraints']}\n\n"
base_prompt += f"## 调研报告\n{PRODUCT_BRIEF['research']}\n\n"
base_prompt += "请输出完整架构方案 (JSON)，用 ```json ... ``` 包裹。"

# 3 种角色视角
PERSPECTIVES = {
    "builder": "你是 Builder（建设者）。关注可实现性、具体步骤、代码结构、模块划分。给出可落地的方案。\n\n",
    "skeptic": "你是 Skeptic（质疑者）。主动找方案的漏洞：边界条件、并发安全、异常路径、向后兼容。指出所有可能出错的地方。\n\n",
    "analyst": "你是 Analyst（分析者）。关注架构合理性、技术选型权衡、长期维护成本。从更高维度评估方案。\n\n",
}

CLAUDE_CLI = ["/Users/jingzhe/.claude/local/claude", "--exclude-dynamic-system-prompt-sections", "-p"]

out_dir = "/tmp/step2-fusion-test"
os.makedirs(out_dir, exist_ok=True)

# ── 3 模型并行产出 ──
outputs = []
for role_name, prefix in PERSPECTIVES.items():
    prompt = prefix + base_prompt
    out_file = f"{out_dir}/model_{role_name}.json"
    print(f"调用 {role_name} ... ", end="", flush=True)
    try:
        r = subprocess.run(
            CLAUDE_CLI + [prompt],
            capture_output=True, text=True, timeout=180,
            env={**os.environ},
        )
        raw = r.stdout
        with open(out_file, "w") as f:
            f.write(raw)
        parsed = try_parse_json(raw) if raw else {}
        outputs.append(raw)
        print(f"{len(raw)} chars, JSON={bool(parsed)}")
    except Exception as e:
        print(f"失败: {e}")
        outputs.append("")

valid = [o for o in outputs if o]
print(f"\n有效产出: {len(valid)}/3")

if len(valid) < 2:
    print("模型产出不足，无法 fusion")
    sys.exit(1)

# ── 保存单模型产出 (Step 1 参照) ──
print(f"\n单模型参照已保存到 {out_dir}/ (Step 1 产出见 /tmp/system-architect-test/)")

# ── 运行 fuse_architecture ──
print("\n========== Fusion 阶段一: 五维分析 ==========")
from singularity.scheduler.execution_judge import _call_model, _ARCH_FUSION_STAGE1

outputs_text = "\n\n---\n".join(
    f"[模型{i+1}]\n{o[:2000]}" for i, o in enumerate(valid)
)
task_text = base_prompt[:1500]
stage1_prompt = _ARCH_FUSION_STAGE1.format(n=len(valid), task=task_text, outputs=outputs_text)
print(f"Stage1 prompt: {len(stage1_prompt)} chars")

# 用 DeepSeek 做裁判 (DEEPSEEK_API_KEY 已设置)
analysis_raw = _call_model(stage1_prompt, "deepseek-chat")
analysis = try_parse_json(analysis_raw) if analysis_raw else {}
print(f"分析完成: {len(analysis_raw) if analysis_raw else 0} chars")

with open(f"{out_dir}/stage1_analysis.json", "w") as f:
    f.write(analysis_raw)

# ── 阶段二: 定稿 ──
print("\n========== Fusion 阶段二: 定稿 ==========")
from singularity.scheduler.execution_judge import _ARCH_FUSION_STAGE2

analysis_text = json.dumps(analysis, ensure_ascii=False, indent=2) if analysis else "分析不可用"
stage2_prompt = _ARCH_FUSION_STAGE2.format(
    task=task_text, analysis=analysis_text, outputs=outputs_text
)
print(f"Stage2 prompt: {len(stage2_prompt)} chars")
fused = _call_model(stage2_prompt, "deepseek-chat")
print(f"定稿完成: {len(fused) if fused else 0} chars")

with open(f"{out_dir}/fused_architecture.md", "w") as f:
    f.write(fused if fused else "Fusion 失败")

fused_parsed = try_parse_json(fused) if fused else {}
print(f"Fusion JSON 有效: {bool(fused_parsed)}")
if fused_parsed:
    print(f"  模块: {len(fused_parsed.get('modules',[]))} 个")
    print(f"  任务: {len(fused_parsed.get('tasks',[]))} 个")
    print(f"  约束: {len(fused_parsed.get('constraints',[]))} 条")
    print(f"  API: {len(fused_parsed.get('api_contracts',[]))} 个")
    fn = fused_parsed.get('fusion_notes', {})
    if fn:
        print(f"  Fusion notes: contradictions={fn.get('resolved_contradictions','?')}, "
              f"insights={fn.get('adopted_insights','?')}, "
              f"blind_spots={fn.get('filled_blind_spots','?')}, "
              f"confidence={fn.get('confidence','?')}")

# ── 对比 Step 1 ──
step1_path = "/tmp/system-architect-test/output.md"
if os.path.exists(step1_path):
    step1_text = open(step1_path).read()
    step1_parsed = try_parse_json(step1_text) or {}
    if step1_parsed:
        print(f"\n========== 对比: 单模型 vs Fusion ==========")
        s1_modules = len(step1_parsed.get('modules', []))
        s1_tasks = len(step1_parsed.get('tasks', []))
        s1_apis = len(step1_parsed.get('api_contracts', []))
        f_modules = len(fused_parsed.get('modules', []))
        f_tasks = len(fused_parsed.get('tasks', []))
        f_apis = len(fused_parsed.get('api_contracts', []))
        print(f"模块: 单模型 {s1_modules} → Fusion {f_modules}")
        print(f"任务: 单模型 {s1_tasks} → Fusion {f_tasks}")
        print(f"API:  单模型 {s1_apis} → Fusion {f_apis}")

print(f"\n结果全部保存到 {out_dir}/")
