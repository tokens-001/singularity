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
TASK_SET = os.environ.get("AB_ROLE_SET", "easy")
CACHE = HERE / (".ab_role_cache.json" if TASK_SET == "easy" else ".ab_role_cache_hard.json")

# 每题：要实现的文件、规格、预先写好的测试
EASY_TASKS = [
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


HARD_TASKS = [
    {
        "name": "median",
        "file": "median.py",
        "spec": '实现 median(nums) -> float：返回列表的中位数。空列表抛 ValueError。**不要修改传入的列表**。',
        "test": '''import pytest
from median import median

def test_odd():
    assert median([3, 1, 2]) == 2

def test_even():
    assert median([4, 1, 3, 2]) == 2.5

def test_single():
    assert median([7]) == 7

def test_empty():
    with pytest.raises(ValueError):
        median([])

def test_does_not_mutate():
    data = [3, 1, 2]
    median(data)
    assert data == [3, 1, 2]
''',
    },
    {
        "name": "merge_intervals",
        "file": "merge_intervals.py",
        "spec": ('实现 merge_intervals(intervals) -> list：输入是 [(start, end), ...]（start <= end），'
                 '合并所有重叠**或相邻**的区间，按起点升序返回。空列表返回 []。**不要修改输入**。'),
        "test": '''from merge_intervals import merge_intervals

def test_overlap():
    assert merge_intervals([(1, 4), (2, 6)]) == [(1, 6)]

def test_adjacent_merged():
    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]

def test_disjoint_kept():
    assert merge_intervals([(1, 2), (4, 5)]) == [(1, 2), (4, 5)]

def test_unsorted_input():
    assert merge_intervals([(5, 6), (1, 3)]) == [(1, 3), (5, 6)]

def test_contained():
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]

def test_empty():
    assert merge_intervals([]) == []

def test_does_not_mutate():
    data = [(5, 6), (1, 3)]
    merge_intervals(data)
    assert data == [(5, 6), (1, 3)]
''',
    },
    {
        "name": "format_bytes",
        "file": "format_bytes.py",
        "spec": ('实现 format_bytes(n) -> str，按 1024 进制格式化：\n'
                 '  - n < 1024 → "{n}B"（0 -> "0B"，1023 -> "1023B"）\n'
                 '  - 否则选不超过 n 的最大单位（KB/MB/GB/TB），保留一位小数，格式 "{v:.1f}{unit}"'
                 '（1024 -> "1.0KB"，1536 -> "1.5KB"）\n'
                 '  - 负数抛 ValueError'),
        "test": '''import pytest
from format_bytes import format_bytes

def test_zero():
    assert format_bytes(0) == "0B"

def test_below_kb():
    assert format_bytes(1023) == "1023B"

def test_exactly_kb():
    assert format_bytes(1024) == "1.0KB"

def test_fraction():
    assert format_bytes(1536) == "1.5KB"

def test_mb():
    assert format_bytes(1024 * 1024) == "1.0MB"

def test_negative():
    with pytest.raises(ValueError):
        format_bytes(-1)
''',
    },
]


TASKS = HARD_TASKS if TASK_SET == "hard" else EASY_TASKS


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
