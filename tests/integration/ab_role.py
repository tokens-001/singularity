"""角色注入 A/B：开 vs 关，比单元测试通过率。

测的是 _exec.py 首轮把 roles.toml 的 implementer.system_prompt 拼进任务这一步
（生产里 _workflow_phases.py:243 写死 route_role="implementer"）。

只隔离这一个变量：
- 两臂都带 _GLOBAL_CONSTRAINTS（生产里无条件注入）
- 都不带记忆/项目上下文注入 —— 所以直接调 _inject_role_context，
  不走 _build_effective_task（后者会额外注入 MAGMA 记忆，那是另一个变量）
- 同一模型、同一题目、同一份测试，只有"有没有那段角色提示词"不同

判据：pytest 通过率（客观，不用 LLM 评委）。

用法: .venv/bin/python tests/integration/ab_role.py [每组重复 默认2]
      AB_ROLE_MODEL=deepseek-v4-flash  AB_ROLE=implementer  AB_ROLE_TASKS=3
"""
import os, sys, json, subprocess, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import singularity.scheduler.dispatcher as disp
from singularity.scheduler._exec import _GLOBAL_CONSTRAINTS, _inject_role_context
from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("AB_ROLE_MODEL", "deepseek-v4-flash")
ROLE = os.environ.get("AB_ROLE", "implementer")
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
CACHE = HERE / ".ab_role_cache.json"

# 每题：要实现的文件、规格、预先写好的测试
TASKS = [
    {
        "name": "slugify",
        "file": "slugify.py",
        "spec": ('实现 slugify(text) -> str：全部转小写；把连续的非字母数字字符替换成单个 "-"；'
                 '去掉首尾的 "-"；空字符串返回 ""。只处理 ASCII 字母数字。'),
        "test": '''from slugify import slugify

def test_basic():
    assert slugify("Hello World") == "hello-world"

def test_multiple_spaces():
    assert slugify("a   b") == "a-b"

def test_strip():
    assert slugify("  Hi There  ") == "hi-there"

def test_symbols():
    assert slugify("C & Python") == "c-python"

def test_empty():
    assert slugify("") == ""
''',
    },
    {
        "name": "parse_duration",
        "file": "parse_duration.py",
        "spec": ('实现 parse_duration(s) -> int：把 "2h" / "30m" / "45s" / "1h30m" 这类'
                 '时长字符串解析成总秒数。不认识的格式抛 ValueError。'),
        "test": '''import pytest
from parse_duration import parse_duration

def test_hours():
    assert parse_duration("2h") == 7200

def test_minutes():
    assert parse_duration("30m") == 1800

def test_seconds():
    assert parse_duration("45s") == 45

def test_mixed():
    assert parse_duration("1h30m") == 5400

def test_invalid():
    with pytest.raises(ValueError):
        parse_duration("abc")
''',
    },
    {
        "name": "chunk_list",
        "file": "chunk_list.py",
        "spec": '实现 chunk_list(items, size) -> list：把列表按 size 切成若干段。size <= 0 抛 ValueError。',
        "test": '''import pytest
from chunk_list import chunk_list

def test_even():
    assert chunk_list([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

def test_remainder():
    assert chunk_list([1, 2, 3], 2) == [[1, 2], [3]]

def test_empty():
    assert chunk_list([], 3) == []

def test_bad_size():
    with pytest.raises(ValueError):
        chunk_list([1], 0)
''',
    },
]


def build_prompt(task, with_role: bool) -> str:
    p = (f"{_GLOBAL_CONSTRAINTS}\n\n"
         f"在工作目录下创建 {task['file']}，实现：\n{task['spec']}\n\n"
         f"工作目录里已有 tests/test_{task['name']}.py，你的实现必须让它们全部通过。\n"
         f"用 `python3 -m pytest tests/ -q` 验证（本机只有 python3，没有 python）。")
    if with_role:
        ctx = _inject_role_context(ROLE)
        if ctx:
            p = ctx + "\n\n---\n" + p
    return p


def run_one(task, with_role: bool, i: int) -> dict:
    """跑一次：建临时工作区 → 出题 → 执行 → 跑测试。"""
    work = Path(tempfile.mkdtemp(prefix=f"abrole_{task['name']}_"))
    (work / "tests").mkdir()
    (work / "tests" / f"test_{task['name']}.py").write_text(task["test"], encoding="utf-8")
    cfg = disp._ensure_agent_type({"model": MODEL})
    t0 = time.time()
    ex = OpenAIAgentExecutor(cfg, build_prompt(task, with_role), f"abrole{i}",
                             cwd=str(work), agent_level="any")
    res = ex.run()
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                           cwd=str(work), capture_output=True, text=True, timeout=300)
        passed = r.returncode == 0
        tail = (r.stdout or r.stderr).strip().splitlines()[-1][:80] if (r.stdout or r.stderr) else ""
    except Exception as e:
        passed, tail = False, f"pytest 异常: {e}"
    return {"passed": passed, "elapsed": round(time.time() - t0, 1),
            "success": res.success, "detail": tail}


def main():
    print(f"模型: {MODEL} | 角色: {ROLE} | 重复: {REPEATS} | 题数: {len(TASKS)}")
    print("=" * 70, flush=True)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    rows = []
    for task in TASKS:
        for arm in (True, False):
            label = "开角色" if arm else "关角色"
            for i in range(REPEATS):
                key = f"{task['name']}|{label}|{i}"
                if key in cache:
                    r = cache[key]
                else:
                    r = run_one(task, arm, i)
                    cache[key] = r
                    CACHE.write_text(json.dumps(cache, ensure_ascii=False))
                rows.append({**r, "task": task["name"], "arm": label})
                print(f"  {task['name']:<16}{label}  第{i+1}次  "
                      f"{'✅ 通过' if r['passed'] else '❌ 失败'}  {r['elapsed']:>5.1f}s  {r['detail']}",
                      flush=True)

    print("\n" + "=" * 70)
    for arm in ("开角色", "关角色"):
        sub = [r for r in rows if r["arm"] == arm]
        if sub:
            print(f"{arm}: 通过 {sum(r['passed'] for r in sub)}/{len(sub)}  "
                  f"平均 {sum(r['elapsed'] for r in sub)/len(sub):.0f}s")
    # 按题配对看（同题同次才是可比的）
    print("\n按题配对:")
    for task in TASKS:
        n = task["name"]
        on = [r for r in rows if r["task"] == n and r["arm"] == "开角色"]
        off = [r for r in rows if r["task"] == n and r["arm"] == "关角色"]
        if on and off:
            print(f"  {n:<16} 开 {sum(r['passed'] for r in on)}/{len(on)}  "
                  f"vs 关 {sum(r['passed'] for r in off)}/{len(off)}")


if __name__ == "__main__":
    main()
