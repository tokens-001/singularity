# 给 GLM-5.2 的执行指令 — P2-1 + P2-4

> 这两件 Opus 已经把"判断件"备好(回归测试 + 失效点清单),你只做执行。
> **铁律**:① 改任何文件前先读该文件全文 ② 改完贴运行输出,不准"应该能跑" ③ 测试不绿不算完。
> 工作目录:`/Users/jingzhe/Singularity/python`。全程带 `QIDIAN_SKIP_EMBED=1`,**禁止**起 loop / 触真模型 API。

---

## 任务 A:拆 `_exec.run`(P2-1,~2h)

`scheduler/_exec.py` 的 `run()` 函数 240+ 行、嵌套过深。把它拆成几个小函数,降低复杂度。**但行为必须一字不变。**

### 你的安全网(必须先认识它)
Opus 已写好回归测试 `python/test_exec_run.py`,覆盖 run() 的 8 条退出路径 + worktree 生命周期对称不变量。**当前它 21/21 全绿。**

```bash
cd /Users/jingzhe/Singularity/python
QIDIAN_SKIP_EMBED=1 python3 test_exec_run.py     # 改之前先跑, 确认 21/21
```

### 怎么拆(建议切法,可调整)
- `_try_one_agent(task, ctx, agents, level, agent_cfg, ...)` → 单个 agent 的 turn 循环(现 `for turn in range(...)` 那段),返回一个"结果信号"(pass/retry/fail/decomposed/cancel/break)。
- `_build_effective_task(task, turn, feedback, is_planner)` → 记忆注入 + planner preamble + 项目上下文拼接那段。
- `_handle_validation(...)` → cascade routing(pass / cascade_accept / cascade_skip / 中置信 retry)的决策那段。
- 主 `run()` 只保留 fallback_chain 的 while 升级骨架。

### 红线(违反任何一条 = 拆坏了)
1. **worktree 的 `try/finally` 不准动语义**:每个被创建的 wt 必须在本 while 迭代退出(return/break/异常)时被 `_cleanup_wt`;只有中置信 retry 的 `continue` 复用同一 wt。如果你把 `_cleanup_wt` 挪进/挪出 finally,或把 wt 创建拆到别的函数但清理留在原处,**就会泄漏**——这正是测试路径4要抓的。
2. 不准改任何 `return BatchOutput(...)` 的字段。
3. 不准改 cascade 的置信度阈值(0.75 / 0.35)。

### 验收(三个都要绿,缺一不可)
```bash
QIDIAN_SKIP_EMBED=1 python3 test_exec_run.py                    # 必须仍 21/21
QIDIAN_SKIP_EMBED=1 python3 -c "import sys;sys.path.insert(0,'.');import scheduler._exec;print('import ok')"
# 然后起服务跑 smoke(见文末"跑 smoke 的标准姿势"),必须 43/43
```
**把这三个的输出原样贴出来。** 任一项红 → 回退你的改动,重来,别硬交。

---

## 任务 B:dispatcher skill/MCP 加缓存(P2-4,~1h)

`scheduler/dispatcher.py` 每次 `dispatch()` 都全量重载 skill 和 MCP 工具,浪费。加缓存。**难点是失效时机,Opus 已经把所有失效点查全列在下面,你照着挂钩子即可。**

### 加载点(要缓存的两个函数)
- `dispatcher._load_skills_for_agent(level, model)` —— 结果按 **`(level, model)`** 为 key 缓存。
- `dispatcher._load_mcp_for_agent()` —— 无参数,**全局单条**缓存。

### 失效点清单(关键 —— 漏一个就会读到陈旧缓存)

**Skill 缓存失效:**
| 触发函数 | 文件 | 影响 | 失效范围 |
|---|---|---|---|
| `skill_add` | `scheduler/_api.py:839` | skill 定义变了 | **全部**(清空整个 skill 缓存) |
| `skill_delete` | `scheduler/_api.py:847` | skill 定义变了 | **全部** |
| `agent_skill_update` | `scheduler/_api.py:862` | 某 agent 的 skill 绑定变了 | 只清 **`(level, model)`** 那一条 |

**MCP 缓存失效(全局,任一触发都清空 mcp 缓存):**
| 触发函数 | 文件 |
|---|---|
| `api_mcp_add_server` | `app.py:1130` |
| `api_mcp_delete_server` | `app.py:1135` |
| `api_mcp_reconnect_server` | `app.py:1140` |
| `api_mcp_refresh` | `app.py:1150` |

### 实现要求
1. 缓存对象放 `dispatcher.py` 模块级。提供两个公开失效函数:
   - `invalidate_skill_cache(level: str = None, model: str = None)` —— 不传参 = 清全部;传 (level, model) = 只清那条。
   - `invalidate_mcp_cache()` —— 清 MCP 缓存。
2. **必须线程安全**(这是坑):`dispatch()` 在 worker 线程读缓存,失效函数从 Flask 请求线程调。**缓存的读/写/失效都要用一把 `threading.Lock` 保护**——否则就是我们刚在 tracker 修过的同一类并发 lost-update。参照 `scheduler/tracker.py` 顶部 `_LOCK` 的写法。
3. 失效函数的调用点:在上表每个触发函数里,**写操作成功之后**调一次对应的 invalidate。app.py 的 MCP 路由需要 `from scheduler import dispatcher` 再调 `dispatcher.invalidate_mcp_cache()`。
4. 参考既有缓存模式:`app.py` 已有 `from scheduler._cache import task_cache` 的用法,风格尽量对齐。

### 验收
```bash
QIDIAN_SKIP_EMBED=1 python3 -c "import sys;sys.path.insert(0,'.');import scheduler.dispatcher,scheduler._api;import app;print('import ok')"
QIDIAN_SKIP_EMBED=1 python3 test_exec_run.py        # 不能被你连带改坏, 仍 21/21
# 起服务跑 smoke, 必须 43/43; 并手动验: 加一个 skill → 再调一次 dispatch 相关端点, 确认缓存已刷新
```
贴出全部输出。

---

## 跑 smoke 的标准姿势(两个任务都要用)
```bash
cd /Users/jingzhe/Singularity/python
QIDIAN_SKIP_EMBED=1 python3 app.py > /tmp/app.log 2>&1 &
for i in $(seq 1 15); do curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5050/health 2>/dev/null | grep -q 200 && break; sleep 1; done
QIDIAN_SKIP_EMBED=1 python3 smoke_test.py            # 看最后一行 43/43
lsof -ti :5050 | xargs kill -9     # 跑完关服务
```
smoke 只测 API CRUD、不驱动 loop,**不会触真模型 API**,放心跑。

---

## 出问题怎么办
- 测试红了:看是哪条路径、哪个断言。**别改测试去迁就代码**——测试是基线,红了说明你的代码改变了行为。回退重来。
- 真卡住、判断不清(比如"这两条路径能不能合并语义会不会变"):**停,把问题抛回去**,别猜着改。猜错了 smoke 绿但行为已坏,这种最贵。
