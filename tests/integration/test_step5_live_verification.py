"""Step 5 真实验证: QA+安全审计师对 CodeSnippetHub 架构方案出报告。

用法:
  cd /Users/jingzhe/奇点
  python3 tests/integration/test_step5_live_verification.py
  python3 tests/integration/test_step5_live_verification.py --claude  # 用本地Claude
"""
import sys, json, os, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from singularity.scheduler.roles import get_role
from singularity.scheduler._io import try_parse_json

out_dir = "/tmp/step5-verification-test"
os.makedirs(out_dir, exist_ok=True)

# ── 从 Step 1 产出提取约束 ──
step1_path = "/tmp/system-architect-test/output.md"
if not os.path.exists(step1_path):
    print("Step 1 产出不存在，请先跑 test_system_architect.py --claude"); sys.exit(1)

arch_raw = open(step1_path).read()
arch = try_parse_json(arch_raw)
if not arch:
    print("解析 Step 1 产出失败"); sys.exit(1)

constraints = arch.get("constraints", [])
modules = arch.get("modules", [])
tasks = arch.get("tasks", [])
print(f"加载 Step 1 架构: {len(modules)} 模块, {len(tasks)} 任务, {len(constraints)} 条约束")

# ── 构建验收上下文 ──
ctx = (
    f"项目: 团队代码片段管理器 (CodeSnippetHub)\n"
    f"模块: {len(modules)} 个\n"
    f"任务: {len(tasks)} 个\n\n"
    f"## 约束清单\n" +
    "\n".join(f"- [{c.get('type','?')}] {c.get('rule', c.get('text',''))}\n  验证方式: {c.get('check','?')}"
              for c in constraints[:10]) +
    f"\n\n## 假定实现说明\n"
    f"架构方案已通过, 假定各任务按标准实现。审计应基于约束逐条检查。"
)

# ── 准备角色 prompt ──
qa_role = get_role("qa_engineer")
sec_role = get_role("security_auditor")
assert qa_role and sec_role, "角色未找到"

qa_prompt = qa_role.system_prompt + f"\n\n---\n\n{ctx}\n\n请输出验收报告 JSON。"
sec_prompt = sec_role.system_prompt + f"\n\n---\n\n{ctx}\n\n请输出安全审计报告 JSON。"

mode = sys.argv[1] if len(sys.argv) > 1 else ""

def call_claude(prompt, label):
    """用本地 Claude CLI 调 LLM。"""
    out_file = f"{out_dir}/{label}.md"
    print(f"调用 {label} ... ", end="", flush=True)
    try:
        r = subprocess.run(
            ["/Users/jingzhe/.claude/local/claude", "--exclude-dynamic-system-prompt-sections", "-p", prompt],
            capture_output=True, text=True, timeout=120,
            env={**os.environ},
        )
        raw = r.stdout
        with open(out_file, "w") as f:
            f.write(raw)
        parsed = try_parse_json(raw)
        print(f"{len(raw)} chars, JSON={'✓' if parsed else '✗'}")
        return raw, parsed
    except Exception as e:
        print(f"失败: {e}")
        return "", {}

def call_deepseek(prompt, label):
    """用 DeepSeek API 调 LLM。"""
    out_file = f"{out_dir}/{label}.md"
    print(f"调用 {label} (DeepSeek) ... ", end="", flush=True)
    try:
        from singularity.scheduler.execution_judge import _call_model
        raw = _call_model(prompt, "deepseek-chat", max_tokens=4000)
        with open(out_file, "w") as f:
            f.write(raw)
        parsed = try_parse_json(raw) if raw else {}
        print(f"{len(raw) if raw else 0} chars, JSON={'✓' if parsed else '✗'}")
        return raw, parsed
    except Exception as e:
        print(f"失败: {e}")
        return "", {}

# ── 执行 ──
call = call_claude if mode == "--claude" else call_deepseek

print("\n=== QA 验收 ===")
qa_raw, qa_parsed = call(qa_prompt, "qa-report")

print("\n=== 安全审计 ===")
sec_raw, sec_parsed = call(sec_prompt, "security-report")

# ── 汇总 ──
print(f"\n=== 验证结果 ===")
if qa_parsed:
    vf = qa_parsed.get("verification", [])
    summary = qa_parsed.get("summary", {})
    print(f"QA: {len(vf)} 项验证, verdict={summary.get('verdict','?')}, "
          f"critical={summary.get('critical',0)}, major={summary.get('major',0)}")
if sec_parsed:
    findings = sec_parsed.get("findings", [])
    summary = sec_parsed.get("summary", {})
    print(f"安全: {len(findings)} 个发现, verdict={summary.get('verdict','?')}, "
          f"critical={summary.get('critical',0)}, high={summary.get('high',0)}")

print(f"\n完整报告: {out_dir}/")
