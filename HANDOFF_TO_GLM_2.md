# 给 GLM-5.2 的执行指令 — 第二轮清理（类型注解 + JSON 统一）

> Opus 已出规格。每项都有明确的"改什么/怎么改/怎么验"。
> **铁律**:① 改前先跑全量测试确认基线绿色 ② 每改完一项跑一次全量 ③ 贴运行输出，不准"应该能跑"。
> 工作目录:`/Users/jingzhe/奇点/python`。全程带 `QIDIAN_SKIP_EMBED=1`。

---

## 基线验证（必须第一步做）

```bash
cd /Users/jingzhe/奇点/python
QIDIAN_SKIP_EMBED=1 python3 smoke_test.py        # 必须 43/43
QIDIAN_SKIP_EMBED=1 python3 test_exec_run.py      # 必须 21/21
QIDIAN_SKIP_EMBED=1 python3 unit_tests.py          # 必须 24/24
```

三项全绿再开始改。贴出结果。

---

## 任务 A: 类型注解补 `from __future__ import annotations`（10 文件，估时 15min）

Python 3.9 下 `list[int]`/`dict[str,float]` 等泛型注解在运行时求值会炸 `TypeError`，需要文件顶部加 `from __future__ import annotations` 把注解变成惰性字符串。

### 要改的文件（10 个）

| # | 文件 | 插入位置 |
|---|------|---------|
| 1 | `app.py` | 第 1 行（docstring 后） |
| 2 | `tools/dash_tui.py` | 第 1 行 |
| 3 | `scheduler/codegraph.py` | 第 1 行 |
| 4 | `scheduler/judge_monitor.py` | 第 1 行 |
| 5 | `scheduler/task_templates.py` | 第 1 行 |
| 6 | `scheduler/conductor.py` | 第 1 行 |
| 7 | `scheduler/model_profile.py` | 第 1 行 |
| 8 | `qidian-knowledge/scripts/hybrid.py` | 第 1 行 |
| 9 | `qidian-knowledge/scripts/embedder.py` | 第 1 行 |
| 10 | `qidian-knowledge/scripts/eval.py` | 第 1 行 |

### 改法（每个文件完全相同）

找到文件开头 —— 如果有 `"""..."""` 即 docstring，插在 docstring 之后；如果没有 docstring，插在第 1 行。

```python
# 改前
"""模块说明..."""
import os
...

# 改后
"""模块说明..."""
from __future__ import annotations
import os
...
```

注意：**只能加一行，不能改任何其他东西。** `from __future__ import annotations` 必须是文件第一个非 docstring 的语句（Python 语法要求）。

### 验收

```bash
cd /Users/jingzhe/奇点/python
# 逐文件验证 import 不炸
for f in app.py tools/dash_tui.py scheduler/codegraph.py scheduler/judge_monitor.py scheduler/task_templates.py scheduler/conductor.py scheduler/model_profile.py qidian-knowledge/scripts/hybrid.py qidian-knowledge/scripts/embedder.py qidian-knowledge/scripts/eval.py; do
  QIDIAN_SKIP_EMBED=1 python3 -c "import sys;sys.path.insert(0,'.');import $(echo $f | sed 's|/|.|g' | sed 's|\.py||')" 2>&1 || echo "FAIL: $f"
done
```

然后跑全量测试（三项），必须仍然 43+21+24 全绿。

**贴出 for 循环的输出 + 三项测试最后一行。**

---

## 任务 B: conductor.py JSON 解析切到 `_io.try_parse_json`（估时 30min）

### 背景

`conductor.py` 的 `_judge_pass()` 函数（约 L218-240）手动实现了一套"从 LLM 输出提取 JSON"逻辑：正则找 `{...}` 块 → `json.loads` → 尾逗号修复重试。这套逻辑和 `_io.try_parse_json` 高度重复——后者已经在项目里、被 `workflow.py` 使用、更完善（还处理 ```json 代码块、截断修复）。

### 当前代码位置

文件：`scheduler/conductor.py`
- `import json`（顶部，L15 附近）
- `import re as _re`（顶部，近似 L12）
- `_judge_pass()` 函数（约 L200-240）

### 改法

**Step 1**: 检查顶部 imports，如果已有 `from ._io import try_parse_json` 就不动。如果没有，在顶部 imports 区域加一行：
```python
from ._io import try_parse_json
```

**Step 2**: 找到 `_judge_pass()` 函数中的 JSON 提取逻辑（大致是这样）：
```python
# 2. 裸 {...} 块
for m in _re.finditer(r"\{[^{}]*\}", raw):
    candidates.append(m.group(0).strip())

# 3. 整体当做 JSON
candidates.append(raw.strip())

for c in candidates:
    try:
        obj = json.loads(c)
        return bool(obj.get("pass", True))
    except (json.JSONDecodeError, TypeError):
        try:
            fixed = _re.sub(r",\s*}", "}", c)
            fixed = _re.sub(r",\s*]", "]", fixed)
            obj = json.loads(fixed)
            return bool(obj.get("pass", True))
        except (json.JSONDecodeError, TypeError):
            continue

return True
```

替换为：
```python
# 用 _io.try_parse_json 统一提取 JSON
result = try_parse_json(raw)
if not result.get("parse_error"):
    return bool(result.get("pass", True))
return True  # 解析失败，默认放行
```

**Step 3**: 如果替换后 `json` 和 `_re` 在 conductor.py 中不再被其他函数使用，删掉对应的 import 行。**先 grep 确认**——如果其他函数还在用，不能删。

### 红线

- **不准改 `return True` 默认放行的语义**：原代码在全部解析失败时放行，新代码也必须放行。
- **不准改函数签名**：`_judge_pass(raw: str) -> bool` 不变。

### 验收

```bash
cd /Users/jingzhe/奇点/python
QIDIAN_SKIP_EMBED=1 python3 -c "import sys;sys.path.insert(0,'.');from scheduler.conductor import _judge_pass;print('import OK')"
# 跑全量测试
QIDIAN_SKIP_EMBED=1 python3 smoke_test.py        # 必须 43/43
QIDIAN_SKIP_EMBED=1 python3 test_exec_run.py      # 必须 21/21
QIDIAN_SKIP_EMBED=1 python3 unit_tests.py          # 必须 24/24
```

贴出三项测试的最后一行。

---

## 任务 C（可选，做完 A+B 有余力才做）: execution_judge.py JSON 解析切到 `_io.try_parse_json`

### 背景

`execution_judge.py` 的 `_parse_verdict()` 函数（约 L115-140）同样实现了 "提取 JSON → 解析" 逻辑。

### 当前代码

```python
def _parse_verdict(raw: str) -> JudgeVerdict:
    if not raw:
        return JudgeVerdict(...)

    candidates = []
    for m in _re.finditer(r"```(?:json)?\s*\n?(.*?)```", raw, _re.DOTALL):
        candidates.append(m.group(1).strip())
    for m in _re.finditer(r"\{[^{}]*\}", raw):
        candidates.append(m.group(0).strip())
    candidates.append(raw.strip())

    for c in candidates:
        try:
            obj = json.loads(c)
            return JudgeVerdict(...)
        except json.JSONDecodeError:
            continue
    ...
```

### 改法

替换为 `try_parse_json` 调用：
```python
result = try_parse_json(raw)
if not result.get("parse_error"):
    return JudgeVerdict(
        pass_=bool(result.get("pass", True)),
        score=float(result.get("score", 0.5)),
        reason=str(result.get("reason", "裁判未返回有效 JSON")),
        failure_mode=str(result.get("failure_mode", "unknown")),
        uncertain=bool(result.get("uncertain", True)),
    )
# 解析失败，默认放行（保持原语义）
return JudgeVerdict(pass_=True, score=0.5, reason="裁判未返回有效 JSON，默认放行",
                    failure_mode="unknown", uncertain=True)
```

### 红线

- **`raw` 为空的提前 return 必须保留**（在调用 try_parse_json 之前）
- **`field.get()` 必须带 default**：`result.get("pass", True)` 不能写成 `result["pass"]`——如果 JSON 里缺字段会炸
- **默认放行语义必须一致**

### 验收

同任务 B 的验收步骤。全量测试必须绿。

---

## 出问题怎么办

- 测试红了：先看是哪条红的。**不准改测试去迁就代码**——测试是基线。回退改动，重新读代码，理解为什么行为变了再重做。
- 如果 `try_parse_json` 的语义和原代码不完全一致导致测试失败：**停。把差异记录下来抛回来**，不要自己猜修。
- `from __future__ import annotations` 导入失败：检查是不是插错位置了（必须在 docstring 之后、其他 import 之前）。
