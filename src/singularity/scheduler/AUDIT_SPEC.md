# Singularity后端架构审计规范 v1.1

> 供 Claude Opus / Codex 执行。7 维度 31 项。每项必须有证据，无证据标记"未验收"，不允许写"通过"。

---

## 审计范围

```
python/scheduler/          (36 个 .py 文件)
python/scheduler/executors/ (5 个 .py 文件)
python/app.py              (Web 层)
python/skills/             (Skill 系统)
```

**共约 45 个文件，~14000 行代码。**

---

## 审计维度

### D1. 分层违规 (规则来源: ARCHITECTURE.md 第三章)

| # | 检查项 | 验证方式 | 允许 | 禁止 |
|---|--------|---------|------|------|
| D1-1 | executor 导入 scheduler 内部模块 | `grep "from \.\. import\|from scheduler\." python/scheduler/executors/*.py` | `..config` 仅此一项 | 其他所有 |
| D1-2 | executor 导入 skills 包 | `grep "from skills\|import skills" python/scheduler/executors/*.py` | 无 | 全部禁止 |
| D1-3 | scheduler 模块导入 executor 内部函数 | `grep "from \.executors\." python/scheduler/*.py` | `from .executors import BaseExecutor, ExecutorResult` 及 executor 类 | executor 内部私有函数 |
| D1-4 | skills 包导入 scheduler | `grep "from scheduler\|import scheduler" python/skills/*.py` | 无 | 全部禁止 |
| D1-5 | 函数体内延迟导入 | `grep "^\s*from \. import\|^\s*from scheduler\." python/scheduler/*.py` 检查是否在 def 内 | 无新增 | 每处延迟导入标记并说明原因 |

### D2. 文件结构 (规则来源: ARCHITECTURE.md 规则4)

| # | 检查项 | 验证方式 | 阈值 | 超标动作 |
|---|--------|---------|------|---------|
| D2-1 | 文件行数 | `wc -l python/scheduler/*.py python/app.py` | 见 D2-1 阈值表 | 超标文件需判断是否有明显拆分点 |
| D2-2 | 函数行数 | `grep -n "^def "` 后计算相邻函数间距 | ≤80 行/函数 | 超标函数标注 |
| D2-3 | 模块是否职责单一 | 检查文件开头 docstring + 函数分类 | 一个模块一个主题 | 混合多个不相关主题的标记 |

**D2-1 阈值表:**

| 文件 | 当前行数 | 上限 | 
|------|---------|------|
| app.py | 1211 | 1200 |
| _api.py | 1032 | 1200 |
| memory.py | 953 | 1000 |
| orchestrator.py | 661 | 700 |
| __main__.py | 633 | 700 |
| workflow.py | 592 | 700 |
| openai_agent.py | 597 | 600 |
| _exec.py | 523 | 600 |
| dispatcher.py | 507 | 600 |
| mcp.py | 490 | 500 |
| 其余 | <500 | 500 |

### D3. 重复代码 (规则来源: 代码自身)

| # | 检查项 | 验证方式 |
|---|--------|---------|
| D3-1 | 相似函数体 | 肉眼比较同名模式函数（如 `_run_*`），列出 ≥10 行重复的代码块 |
| D3-2 | JSON 解析重复 | `grep "json.loads\|json.JSONDecodeError" python/scheduler/*.py`，统计各处是否使用了统一的 `_try_parse_json` |
| D3-3 | 错误处理模式重复 | `grep "except Exception as e:" python/scheduler/*.py` 后比较处理逻辑 |
| D3-4 | 配置加载重复 | `grep "tomllib.load\|open.*toml\|_read_json" python/scheduler/*.py` 是否统一 |

### D4. 异常处理 (规则来源: 通用标准)

| # | 检查项 | 验证方式 |
|---|--------|---------|
| D4-1 | 裸 except 无类型 | `grep "except:" python/scheduler/*.py` (排除 `except Exception:` 和特定异常) |
| D4-2 | 吞异常无日志 | `grep -A2 "except" python/scheduler/*.py` 检查 except 块是否只有 `pass` 且无注释说明原因 |
| D4-3 | API 路由无异常处理 | 检查 `python/app.py` 中 `@app.route` 函数是否有 try/except |
| D4-4 | 文件操作无异常处理 | `grep "open\|read_text\|write_text" python/scheduler/*.py` 是否被 try 包裹 |

### D5. 命名与风格一致性 (规则来源: 周围代码)

| # | 检查项 | 验证方式 |
|---|--------|---------|
| D5-1 | 私有函数前缀 | `grep "^def [a-z]" python/scheduler/*.py` 内部函数是否以 `_` 开头 |
| D5-2 | 模块名一致性 | 公开模块 vs 内部模块是否用 `_prefix.py` 区分 |
| D5-3 | 导入顺序 | 检查各文件是否按 标准库→第三方→本地 排列 |
| D5-4 | 类型注解覆盖率 | 检查 `def` 函数签名是否有类型注解（≥80% 覆盖率目标） |

---

### D6. 循环导入 (规则来源: Python 导入机制 + ARCHITECTURE.md 规则3)

> **严重度: P0** — 循环导入导致模块加载失败，服务直接炸。

| # | 检查项 | 验证方式 |
|---|--------|---------|
| D6-1 | 显式环检测 | 从每个模块入口追踪 `from . import` 链，画有向图，检查是否有环 |
| D6-2 | 延迟导入作为破环手段 | `grep -rn "^\s*from \.\|^\s*from scheduler\." python/scheduler/*.py python/scheduler/executors/*.py` 过滤出函数体内的导入，每处标注：哪个模块→延迟导入了哪个模块→为了破什么环 |
| D6-3 | _api.py 与 orchestrator 的潜在环 | 检查 `_api.py` 是否导入了 `orchestrator`（orchestrator 又导入 `_exec` → `dispatcher` → `_api` 形成环） |
| D6-4 | memory ↔ _lifecycle 已破环验证 | 确认 `_lifecycle.py` 不导入 `memory.py`，依赖通过参数注入 |
| D6-5 | dispatcher ↔ executor 已破环验证 | 确认 `openai_agent.py` 不导入 `dispatcher`，依赖通过参数注入 |

**验证命令**:
```bash
# D6-1: 追踪导入图
python3 -c "
import ast, sys, os
sys.path.insert(0, 'python')
# 收集所有 from . import 边
edges = {}
for root, dirs, files in os.walk('python/scheduler'):
    for f in files:
        if f.endswith('.py') and not f.startswith('__'):
            mod = os.path.join(root, f)[7:-3].replace('/', '.')
            if mod.endswith('.__init__'): mod = mod[:-9]
            try:
                tree = ast.parse(open(os.path.join(root, f)).read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module and node.level and node.level > 0:
                        if 'scheduler' in (node.module or ''):
                            edges.setdefault(mod, set()).add(node.module)
            except: pass
print('Import graph edges:', len(edges))
for src, dsts in edges.items():
    for dst in dsts:
        if src in edges.get(dst, set()):
            print(f'CYCLE: {src} <-> {dst}')
"
```

### D7. 状态一致性 (规则来源: orchestrator.py docstring 契约 + tracker 文件系统状态机)

> **严重度: P0** — 状态机写坏 = 任务卡死/重复执行/产出丢失。

| # | 检查项 | 验证方式 |
|---|--------|---------|
| D7-1 | 谁在写 tracker | 契约："只有主线程写 tracker"。grep 所有 `tracker.transition\|tracker.create` 调用位置，确认是否在 `ThreadPoolExecutor.submit` 的回调内 |
| D7-2 | CAS 状态转换原子性 | tracker 使用文件系统实现状态机，检查 `cas()` (compare-and-swap) 是否真正原子（文件 rename 操作） |
| D7-3 | 崩溃恢复 | 检查 `tracker.recover()` 是否覆盖了所有中间态（RUNNING/DISPATCHED 但进程已死） |
| D7-4 | 取消信号的竞态 | 检查 `_cmd_cancel` 写 cancel 文件 vs 执行循环读 cancel 文件之间是否有竞态窗口 |
| D7-5 | worktree 泄漏 | 检查 worktree 创建/清理是否对称，`_cleanup_wt` 是否在所有退出路径调用 |
| D7-6 | 内存事件持久化 | memory.py 的 `_save_events` 使用原子写（tmp + replace），确认无 partial write 风险 |

**验证命令**:
```bash
# D7-1: 谁在写 tracker
echo "=== tracker.transition 调用位置 ==="
grep -rn "tracker\.transition\|tracker\.create\|tracker\.cas" python/scheduler/*.py | grep -v "def \|#\|\.pyc"

echo "=== 检查 ThreadPoolExecutor submit 的参数函数是否调 tracker ==="
grep -rn "pool.submit\|ThreadPoolExecutor" python/scheduler/orchestrator.py

# D7-5: worktree 路径对称性
echo "=== create vs cleanup 调用次数 ==="
echo "create: $(grep -c '_maybe_create_worktree\|wt_create' python/scheduler/*.py)"
echo "cleanup: $(grep -c '_cleanup_wt\|wt_cleanup' python/scheduler/*.py)"
```

## 审计输出格式

每个检查项必须按以下格式输出：

```
| # | 检查项 | 对应文件:行号 | 验证方式 | 实际结果 | 是否通过 | 证据 |
```

**通过**: 完全符合规则  
**未通过**: 不符合，需注明具体问题  
**未验收**: 无法验证，需注明原因（如规则不适用、文件不存在等）  
**豁免**: 不符合但有合理原因，需注明豁免理由

---

## Opus / Codex 分工建议

| 维度 | Opus (核心) | Codex (补充) |
|------|------------|-------------|
| D1 分层违规 | 全部 5 项 | — |
| D2 文件结构 | D2-2 函数行数, D2-3 职责单一 | D2-1 文件行数 |
| D3 重复代码 | D3-1 相似函数体, D3-2 JSON解析 | D3-3 错误处理, D3-4 配置加载 |
| D4 异常处理 | D4-1 裸except, D4-2 吞异常 | D4-3 API路由, D4-4 文件操作 |
| D5 命名风格 | D5-2 模块名, D5-3 导入顺序 | D5-1 私有前缀, D5-4 类型注解 |
| D6 循环导入 | D6-1 显式环, D6-2 延迟导入破环, D6-3 _api↔orchestrator | D6-4 memory↔_lifecycle, D6-5 dispatcher↔executor |
| D7 状态一致性 | D7-1 谁写tracker, D7-2 CAS原子性, D7-3 崩溃恢复 | D7-4 取消竞态, D7-5 worktree泄漏, D7-6 持久化原子性 |

---

## 严重度总结

| 维度 | 严重度 | 项数 | 不通过后果 |
|------|--------|------|-----------|
| D6 循环导入 | **P0** | 5 | 模块加载失败，服务直接炸 |
| D7 状态一致性 | **P0** | 6 | 任务卡死/重复执行/产出丢失 |
| D1 分层违规 | P1 | 5 | 架构腐化，改动蔓延 |
| D4 异常处理 | P1 | 4 | 静默失败，问题不可见 |
| D3 重复代码 | P2 | 4 | 改一处漏一处，bug 来源 |
| D2 文件结构 | P2 | 3 | 可维护性下降 |
| D5 命名风格 | P3 | 4 | 可读性下降 |

**总计: 7 维度, 31 项, P0×11 + P1×9 + P2×7 + P3×4**
