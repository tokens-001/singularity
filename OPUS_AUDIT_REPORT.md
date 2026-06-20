# 奇点后端审计报告 — Phase 2 (Opus 4.8)

> 证据全部经过实读 + AST/import 实测验证。`无法确认` 处已标注。
> 验证方式：直接读 7 核心文件全文 + 12 个关联文件 + AST 静态分析 + 实测模块导入（未触发任何真实 API）。

---

## 一、对 GPT（Codex）结论的复核

GPT 共 34 项。复核结论：**真问题 24 项、误判/夸大 5 项、引用错误（含不存在文件）3 项、需重新定级 2 项**。另有 **6 项 GPT 漏报**（见第六节），其中 1 项为真 P0。

| 原结论 | 复核 | 理由（证据） |
|--------|------|------|
| D1-1 claude_cli.py:155 executor 延迟导入 witness ❌ | **成立（降级 P3）** | `executors/claude_cli.py:155` 确在 `except` 内 `from .. import witness`。但仅用于失败日志，非业务依赖，影响极小。 |
| D1-2 executor 导入 skills ✅ | **确认通过** | `grep from skills` 对 executors/*.py 无命中。 |
| D1-3 scheduler 导入 executor 内部函数 ❌ | **成立，但根因被 GPT 看偏** | 实测 7 处导入 `from .executors.worktree import ...`（_exec:30, _worktree:12/59, merge:28, orchestrator:70, +neijinglu:28 导 base.ExecutorResult 属允许）。**真正根因：`executors/worktree.py` 放错了包**——它是 git 基础设施不是 executor 运行时。见 P1-3。 |
| D1-4 skills 导入 scheduler ✅ | **确认通过** | 无命中。 |
| D1-5 函数体内延迟导入 ❌ | **成立（量级被低估）** | 不止 GPT 列的 5 处。`_api.py` 单文件就有 **40+** 处函数内 `from . import ...`（323~887 行密集）。是系统性手法，非个别。 |
| D2-1 app.py 1211 > 1200 ❌ | **成立（边际）** | `wc -l` = 1211，超 11 行。 |
| D2-2 多个函数 > 80 行 ❌ | **成立** | 实测 `_exec.run` 147→390 = **243 行**；`openai_agent.run` 172→346 = **174 行**；`orchestrator._run_queue_v3` 405→512 = **108 行**。属实。 |
| D2-3 MCP CRUD 写在 app.py ❌ | **成立** | app.py:1122-1195 ≈ 69 行业务逻辑直接在路由里，违反"路由 ≤5 行"自定规则。 |
| D3-1 roles.py 重复 ❌ | **成立（minor）** | `_load_personas`(35) 与 `_load_roles`(115) 结构同形 ~10 行。 |
| D3-2 JSON 提取未复用 ❌ | **成立，但建议保留** | execution_judge:131 / goal_loop:129 / conductor 各自 `json.loads`，未用 `workflow._try_parse_json`。但强行复用会让 workflow 变成被四处依赖的工具模块，**新增耦合**。建议下沉到 `config` 或新建 `_jsonutil.py`，而非反向依赖 workflow。 |
| D3-3 except: pass 重复 ❌ | **成立（minor）** | _auth:86 / _profiler:41 / _token_budget:45 均 `except Exception: pass`。 |
| D3-4 TOML 读取未统一 ❌ | **成立（minor）** | dispatcher:42 / mcp:393 / model_registry:81 / roles:42,122 共 5 处裸 `tomllib.load`。 |
| D4-1 裸 except: ❌ | **部分成立 + GPT 引用了不存在的文件** | 真实裸 `except:` 仅 **3 处**：_exec.py:86、_exec.py:412、orchestrator.py:581（均为 `witness.heartbeat` 失败后的兜底）。**GPT 引用的 `verify_laws.py:58,116,189` —— 该文件根本不存在**（`ls scheduler/` 无此文件）。GPT 这一行是幻觉/陈旧引用。 |
| D4-2 吞异常无日志 ❌ | **部分成立** | 确有多处 `except Exception: pass`，但相当一部分前面有 `witness.heartbeat`，非完全静默。需逐处看，不能一刀切。 |
| D4-3 API 路由无 try/except ❌ | **成立但降级 P3** | 路由确实没有 try/except，但 handler 统一返回 `(data, code)`，且 Flask 有默认 500 兜底。属健壮性而非"会炸"。 |
| D4-4 文件操作无异常处理 ❌ | **部分误判** | tracker._write 用 tmp+os.replace；memory._write_json 同。多数核心写已有保护。GPT 列的部分点已被覆盖。 |
| D5-* | 不复核 | 按约束，D5 风格/命名不审。 |
| D6-1 显式导入环→服务炸 P0 ❌ | **夸大，降级 P2** | **实测 `import orchestrator,_exec,dispatcher,_planner,_api` 全部成功，无崩溃。** 环（orchestrator→_exec→dispatcher→orchestrator）被 dispatcher:317 的延迟导入打断。"模块加载失败/服务直接炸"不成立——是隐性环，靠延迟导入续命，属可维护性 P2。 |
| D6-2 延迟导入未标注破环原因 ❌ | **成立** | dispatcher:317 `from .orchestrator import _pending_sse_events` 无注释说明它在破环。 |
| D6-3 _api 顶层导入 orchestrator ❌ | **成立但无害** | _api.py:22 顶层 `from . import orchestrator` 属实。但 orchestrator 不反向导入 _api，**此方向无环**，不致崩。 |
| D6-4 / D6-5 已破环 ✅ | **确认通过** | _lifecycle 参数注入、openai_agent 依赖注入，均无反向导入。 |
| D7-1 多处直接写 tracker ❌ | **成立且比 GPT 说的更严重** | 直接写 tracker 的模块：_api、__main__、merge、_planner、workflow（实测 grep）。**关键：_api 在 Flask 请求线程写，loop 在后台线程写 → 真·双线程写 tracker**，orchestrator docstring 的"只有主线程写"在 Web 运行态下是假的。见 P1-4。 |
| D7-2 CAS 非原子 ❌ | **成立但定级过高** | `cas()` 是 read→check→write 三步非原子（tracker.py:242-251），注释自认单线程。loop 内部确为单写线程，不变量成立；但叠加 D7-1 的 Flask 线程，存在 lost-update 窗口。因每文件 os.replace 原子、且 API 端有状态前置校验，无文件损坏、概率低 → **P2 非 P0**。 |
| D7-3 recover 覆盖 INFLIGHT ✅ | **确认通过** | recover() 只动 _INFLIGHT，BLOCKED/DECOMPOSED/CONFLICT_HELD 不在内（tracker:34,341），正确。 |
| D7-4 cancel 竞态 ❌ | **成立但benign** | _api.task_cancel 对 RUNNING/DISPATCHED **只写 cancel 文件不转态**（_api:277-280），_exec 每 turn 头部读取并 unlink（_exec:211-215）。设计良好，最坏只是延迟一个 turn 或给已完成任务留个孤儿文件。**降级 P3**。 |
| D7-5 worktree 清理不在 try/finally ❌ | **成立，真 P1** | run() 的 _cleanup_wt 散落在 8 条 return 路径，但 create(202) 与 cleanup 之间若 `dispatch()` 抛异常（pick_agent 可 raise RuntimeError），worktree **泄漏**且无 try/finally 兜底。叠加 _run_queue_v3:479 `fut.result()` 未包 try/except，一个坏任务还会炸掉整批循环。见 P1-1。 |
| D7-6 原子写 ✅ | **确认通过** | memory._write_json tmp+replace（53-57），正确。 |

---

## 二、P0/P1 根因分析 & 最优方案

### 问题 P0-1: 限流钩子引用未定义变量 `now`（GPT 漏报）

- **证据**：app.py:195 `window_start = now - _RATE_WINDOW`、:212 `_RATE_BUCKETS[ip].append(now)`。AST 实测 `_guard_rate_limit` 函数体内 `now` 被读取但从未赋值（同名 `now` 只在另一个函数 `_cleanup_rate_buckets` 里定义）。
- **根因**：从 `_cleanup_rate_buckets`（局部 `now=time.time()`）复制限流逻辑到 `_guard_rate_limit` 时漏带 `now=time.time()` 那一行。localhost 在 :189 提前 return，掩盖了 bug——所以本地开发与 43/43 smoke 全绿，**任何非回环 IP 访问 `/api/` 立即 NameError → 500**。
- **后果**：一旦从另一台机/容器/反代（非 127.0.0.1）访问，所有 API 全挂。生产上线即炸，且测试照不出。
- **方案（无争议）**：`_guard_rate_limit` 在 `ip = request.remote_addr ...` 后补一行 `now = time.time()`。估时 2 分钟。
- **涉及文件**：app.py:187 附近。
- **API/行为影响**：是（修复后远程访问才可用）。
- **smoke test**：能过（localhost 路径不变）；建议补一条"伪造非回环 X-Forwarded-For / remote_addr 打一次 /api/status 期望非 500"。

```python
# 改前 (app.py ~187-195)
    ip = request.remote_addr or "127.0.0.1"
    if ip in ("127.0.0.1", "::1", "localhost"):
        return None
    if ip not in _RATE_BUCKETS:
        _RATE_BUCKETS[ip] = []
    window_start = now - _RATE_WINDOW          # ← now 未定义 → NameError

# 改后
    ip = request.remote_addr or "127.0.0.1"
    if ip in ("127.0.0.1", "::1", "localhost"):
        return None
    now = time.time()                          # ← 补这一行
    if ip not in _RATE_BUCKETS:
        _RATE_BUCKETS[ip] = []
    window_start = now - _RATE_WINDOW
```

### 问题 P1-1: worktree 泄漏 + 单任务异常炸全批（D7-5 强化）

- **根因**：`run()`（_exec.py:147）把清理责任分散到每条 return 分支，而非用 try/finally 包住"create→执行→cleanup"整段。设计上把"资源生命周期"和"控制流"耦合了，新增任何 return 分支都可能漏 cleanup。
- **后果**：`disp_mod.dispatch()` 内 `pick_agent` 在"全层 agent 不可用"时 raise RuntimeError（dispatcher.py:216），异常穿过 run() → _run_with_retry → worker，主线程 `_run_queue_v3:479 fut.result()` 未捕获 → **抛出整个 `with ThreadPoolExecutor` 块**，本轮 results 丢失、已建 worktree 残留（_MAX_WORKTREES=50 满后新任务全部静默跳过）。外层 app.py:358 虽 catch 但 5s 后重试又撞同样异常 → 活锁刷错。
- **方案A（最小）**：`_run_queue_v3:478-486` 给 `fut.result()` 包 try/except，异常 → 标 FAILED + `_cleanup_wt` 兜底。估时 0.5h。
- **方案B（最优，无争议方向）**：run() 内把 worktree 用 try/finally 框死：
  ```python
  wt = _maybe_create_worktree(...)
  try:
      ... 所有 turn 循环与 return ...
  finally:
      _cleanup_wt(wt)
  ```
  删掉散落的 8 处 `_cleanup_wt(wt)`，return 前不再各自清理。**同时**给 _run_queue_v3 的 fut.result() 加 try/except（方案A）。估时 1.5h。
- **推荐 B**：A 只堵了"炸全批"，没堵"泄漏"；B 同时根治资源对称性，且让 run() 少 8 行重复。
- **涉及文件**：_exec.py:202-390、orchestrator.py:478-486。
- **API/行为影响**：否。
- **smoke test**：需调整——run() 重构后要验证 8 条原 return 路径行为不变（pass/retry/escalate/cancel/merge_conflict/planner/fail/exhausted）。建议加一条"构造无可用 agent 的任务，期望标 FAILED 且 worktrees 目录不增长"。

### 问题 P1-2: `_embed` 在 QIDIAN_SKIP_EMBED 模式崩溃（GPT 漏报）

- **证据**：实测 `QIDIAN_SKIP_EMBED=1 python -c "memory._embed('x')"` → `AttributeError: 'NoneType' object has no attribute 'encode'`；`memory.find_similar(...)` 同崩。
- **根因**：`_get_embed_model()`（memory.py:119）在 SKIP_EMBED 时 `return None`，但 `_embed`（:144）无条件 `_get_embed_model().encode(...)`，没处理 None。SKIP_EMBED 的设计意图是"跳过 embedding"，实现却变成"崩溃"。
- **后果**：CI/快速模式下凡走 `find_similar`/`traverse`/`index_task` 的路径全炸。`_inject_memory`（_exec:60）有 try/except 兜住会静默退化成空注入；但 `query()→find_similar`（memory:675）和 `_api.memory_query` 无兜底 → `/api/memory` 在 SKIP_EMBED 下 500。
- **方案（无争议）**：`_embed` 加 None 守卫。估时 5 分钟。
  ```python
  def _embed(text):
      if not text or not text.strip():
          return []
      model = _get_embed_model()
      if model is None:            # ← SKIP_EMBED 或加载失败
          return []
      return model.encode(text.strip(), normalize_embeddings=True).tolist()
  ```
- **涉及文件**：memory.py:140-144。
- **API/行为影响**：否（生产开 embed 时行为不变）。
- **smoke test**：能过且更稳——建议 smoke 显式 `QIDIAN_SKIP_EMBED=1` 跑，杜绝下载 420MB 模型 / 误触真实推理（契合"scheduler 测试不碰真 API"约束）。

### 问题 P1-3: `executors/worktree.py` 放错包，制造 7 处分层违规（D1-3 根因）

- **根因**：worktree.py 是 git worktree 基础设施（只依赖 `..config`），却被放进 `executors/` 包。结果 scheduler 核心（_exec/_worktree/merge/orchestrator）必须 `from .executors.worktree import ...` 反向伸进 executor 包，违反"scheduler 不依赖 executor 内部"红线。GPT 把它当成 4 个独立违规点，其实是 1 个错位。
- **后果**：架构图谎报"executors 隔离 ✅"；任何人想动 executor 层会被 worktree 的反向依赖绊住；分层红线名存实亡。
- **方案A（最小）**：在 AUDIT_SPEC 的 D1-3 允许清单显式豁免 `executors.worktree`，承认现状。估时 10 分钟（仅改文档）。
- **方案B（最优）**：把 `executors/worktree.py` 上移为 `scheduler/_git_worktree.py`，5 个 import 点改路径，executors/ 不再被 scheduler 反向依赖。估时 1h。
- **推荐 B**：A 是粉饰，B 才真正恢复分层。worktree.py 只依赖 config，上移零风险。
- **涉及文件**：新 scheduler/_git_worktree.py；改 _exec:30、_worktree:12/59、merge:28、orchestrator:70。
- **API/行为影响**：否。
- **smoke test**：能过（纯路径迁移，import 实测可验证）。

### 问题 P1-4: tracker 双线程写（D7-1/D7-2 合并重定级）

- **根因**：orchestrator 契约写着"只有主线程写 tracker"，但 Web 层把任务管理（hold/retry/override/cancel-非running/delete）放在 `_api.py` 的 Flask 请求线程里直接 `tracker.transition`。Flask 默认多线程，loop 又是独立后台线程——**两类线程并发写同一批 task 文件**。`cas()` 是非原子 read-check-write。
- **后果**：极端时序下 lost-update：loop 读到 RUNNING、Flask 把它 retry 成 PENDING、loop 写回 DONE 覆盖了 retry。因每文件 os.replace 原子（无损坏）+ API 端有状态前置校验（task_retry 只接受 FAILED/ROLLED_BACK），窗口窄、概率低，但契约是假的。
- **方案A（最小）**：把 docstring 的"只有主线程写"改成实话"写入分散在 loop 线程 + Flask 线程，靠 os.replace 原子写 + API 状态前置校验避免损坏"，并给 `_write` 加一把进程内 `threading.Lock`（tracker 模块级）保护 read-modify-write。估时 0.5h。
- **方案B（最优）**：Web 的写操作不直接调 tracker，改为投递"意图文件"（类似 cancel 的 CANCEL_DIR 机制）到 loop，由 loop 单线程消费落库。估时 3h+。
- **推荐 A**：B 改动面大、收益边际（当前无实测竞态故障）。A 用一把锁封死 read-modify-write 即可消除 lost-update，性价比最高。
- **涉及文件**：tracker.py（_write/transition/cas 加锁）、orchestrator.py docstring。
- **API/行为影响**：否。
- **smoke test**：能过。

### 问题 P1-5: 静默吞异常 + 裸 except（D4-1/D4-2 收敛）

- **根因**：核心路径用 `try: witness.heartbeat(...) except: pass`（_exec:86,412、orchestrator:581）兜底，连日志写失败都吞。设计动机是"记忆/观测不能拖垮主流程"，但裸 `except:` 连 KeyboardInterrupt/SystemExit 都吞。
- **后果**：问题不可见；调试时连"为什么没记录"都查不到。
- **方案（无争议）**：3 处裸 `except:` → `except Exception:`（放过 BaseException）。`_auth/_profiler/_token_budget` 的 `except Exception: pass` 至少补一行 `# 观测失败不影响主流程` 注释 + 可选 debug 日志。估时 0.5h。
- **涉及文件**：_exec.py:86,412、orchestrator.py:581、_auth.py:86、_profiler.py:41、_token_budget.py:45。
- **API/行为影响**：否。
- **smoke test**：能过。

---

## 三、核心文件深度审查（GPT 漏掉的）

| 文件 | 函数/行 | 问题 | 严重度 |
|------|---------|------|--------|
| orchestrator.py | `_process_batch` (214-255) | **死代码**：与 `_finalize_result`(323) 近乎逐行重复（~42 行），全仓无任何调用方（grep 实证）。改一处漏一处的隐患源。 | P2 |
| orchestrator.py | `_finalize_result` vs `_process_batch` | 二者 90% 重复（状态机分支、_save_trace、SSE、escalation）。应删 `_process_batch`，单留 `_finalize_result`。 | P2 |
| _exec.py | `run` 173,389 | `final_turn` 初始化为 0 后**从未更新**，失败兜底分支 `return BatchOutput(... turn_count=final_turn)` 永远报 0 轮，观测/画像里失败任务轮次失真。 | P3 |
| _exec.py | `run` 383-384 | `feedback = ""` 连写两行（手滑），无害但是漏改信号。 | P3 |
| _exec.py | `run` 195-390 | 单函数 243 行、6 层嵌套（while→for→if validation→cascade）。圈复杂度过高，是全仓最难维护的函数。 | P2 |
| dispatcher.py | `dispatch` 357-364 | 每次 dispatch 都重新 `_load_skills_for_agent`+`_load_mcp_for_agent`+`_make_permission_checker`（含 skills/mcp 全量加载），无缓存。高并发/多 turn 下重复 IO。 | P2 |
| openai_agent.py | `_execute_tool` 278 | `except (json.JSONDecodeError, Exception)` —— `Exception` 已涵盖 `JSONDecodeError`，前者冗余，且等于裸捕获所有异常。 | P3 |
| openai_agent.py | `_tool_run` 455 | `safe_env` 用 `any(p in k.upper() ...)` 过滤密钥，但 `entry`/`base_url` 等非密钥配置可能含 token 仍漏（启发式不完备）。子进程仍继承 PATH 等。可接受但非密闭。 | P3 |
| tracker.py | `_next_id` 116-125 | 每次建任务都 `glob("*.json")` 全表扫描求 max id。任务量大时建任务变慢 O(n)。 | P3 |
| memory.py | `_rrf_anchors` 338 | 形参名 `query_tokens: set[str]`，实参传的是 `_embed()` 返回的 `list[float]`（traverse:480）。类型注解与实际相悖，误导性强。 | P3 |
| memory.py | `index_task` 300-308 | 新任务对**全部历史事件**算 cosine（O(n) 每次建边），事件多时建索引线性变慢。 | P2 |
| _api.py | 全文 | 40+ 处函数内延迟导入 `from . import project/memory/...`。虽破环但已成默认编码风格，违反 ARCHITECTURE 规则3"禁止新延迟导入"的精神。 | P2 |

---

## 四、渐进式修复计划（按依赖排序，每步后 43/43 必须绿）

### Step 1: P0-1 限流 `now` 修复 (risk: 低, 估时: 5min)
- **涉及文件**：app.py:~190
- **改动**：`_guard_rate_limit` 内 `if ip in (...): return None` 之后补 `now = time.time()`
- **Smoke 预估**：能过，localhost 路径零变化。
- **依赖**：无。**独立、可立即上线。**

### Step 2: P1-2 `_embed` None 守卫 (risk: 低, 估时: 5min)
- **涉及文件**：memory.py:140-144
- **改动**：见 P1-2 代码块。
- **Smoke 预估**：能过，建议 smoke 加 `QIDIAN_SKIP_EMBED=1`。
- **依赖**：无。

### Step 3: P1-5 裸 except 收敛 (risk: 低, 估时: 30min)
- **涉及文件**：_exec.py:86,412、orchestrator.py:581 → `except Exception:`
- **Smoke 预估**：能过。
- **依赖**：无。

### Step 4: 删死代码 `_process_batch` (risk: 低, 估时: 15min)
- **涉及文件**：orchestrator.py:214-255 整段删除
- **改前**：两个近重复函数并存。**改后**：仅留 `_finalize_result`。
- **Smoke 预估**：能过（实测无调用方）。先 grep 二次确认再删。
- **依赖**：无。

### Step 5: P1-1 worktree try/finally + fut.result 兜底 (risk: 中, 估时: 1.5h)
- **涉及文件**：_exec.py:202-390、orchestrator.py:478-486
- **改前**：8 处散落 `_cleanup_wt(wt)`；`batch = fut.result()` 裸调。
- **改后**：run() 用 try/finally 框 worktree；fut.result() 包 try/except→标 FAILED。
- **Smoke 预估**：**需调整**——逐一验证 8 条 return 路径；加"无可用 agent → FAILED 且 worktree 不泄漏"用例。
- **依赖**：Step 4（同文件，先删干净再重构）。

### Step 6: P1-3 worktree.py 上移 (risk: 中, 估时: 1h)
- **涉及文件**：新建 scheduler/_git_worktree.py；改 5 处 import。
- **改前**：`from .executors.worktree import`。**改后**：`from ._git_worktree import`。
- **Smoke 预估**：能过；`python -c "import scheduler._exec, scheduler.merge, scheduler.orchestrator"` 实测无 ImportError 即验证。
- **依赖**：Step 5（避免和 run() 重构撞同文件）。

### Step 7: P1-4 tracker 加锁 + 改 docstring (risk: 中, 估时: 0.5h)
- **涉及文件**：tracker.py（模块级 Lock 包 _write 及 transition/cas 的 read-modify-write）。
- **Smoke 预估**：能过（单线程 smoke 无感）。
- **依赖**：无（但建议放最后，避免与其它 tracker 改动交叉）。

### Step 8（可选，P2 整治）: app.py MCP CRUD 下沉 _api、_exec.run 拆分、TOML/JSON 加载统一到 `_io.py`
- risk: 中, 估时: 3-4h。**依赖** Step 6 完成后再做，避免大爆炸。

---

## 五、最终优先级

| 优先级 | 问题 | 方案 | 估时 | 不改后果 |
|--------|------|------|------|----------|
| **不改会炸** | P0-1 限流 `now` NameError | 补 `now=time.time()` | 5min | 任何非回环访问全 API 500，上线即废，测试照不出 |
| 不改会腐 | P1-1 worktree 泄漏+炸全批 | try/finally + fut 兜底 | 1.5h | agent 不可用时整批循环崩、worktree 占满 50 上限后静默拒单 |
| 不改会腐 | P1-2 `_embed` SKIP 崩溃 | None 守卫 | 5min | CI/SKIP 模式 /api/memory 与记忆注入崩或静默退化 |
| 不改会腐 | P1-4 tracker 双线程写 | 加 Lock + 改契约 | 0.5h | 用户操作与 loop 偶发 lost-update，状态错乱难复现 |
| 不改会腐 | P1-3 worktree.py 错位 | 上移到 scheduler/ | 1h | 分层红线名存实亡，executor 层被反向锁死 |
| 不改会腐 | P1-5 裸 except 吞异常 | → except Exception | 0.5h | 观测失败不可见，问题被静默 |
| 改了更好 | P2: _process_batch 死代码 | 删 | 15min | 改一处漏一处的 bug 温床 |
| 改了更好 | P2: _exec.run 243 行 | 拆函数 | 2h | 最难维护点持续腐化 |
| 改了更好 | P2: app.py MCP CRUD 内联 | 下沉 _api | 1h | 违反自定规则，app.py 持续膨胀 |
| 改了更好 | P2: 延迟导入泛滥/TOML/JSON 重复 | 统一 _io 模块 | 2h | 编码风格腐化 |
| 改了更好 | P3: final_turn=0 / 类型注解误导 / _next_id O(n) 等 | 逐个修 | 各 5-15min | 观测失真、性能边际下降 |

---

## 六、GPT（Codex）漏报补充

> GPT 为什么容易漏：它按 grep 规则逐项扫"已知模式"（裸 except、文件行数、import 链），对**跨函数的变量作用域 bug**（需 AST/数据流）和**运行时条件分支 bug**（需实跑特定 env）天然盲。同时它会**幻觉引用不存在的文件**来凑数。

1. **【真 P0，GPT 漏】app.py:195 `now` 未定义** —— `_guard_rate_limit` 用了另一个函数的局部变量名。grep 扫不出作用域问题；localhost 提前 return 让 smoke 全绿，掩盖致命 bug。AST 实证。
2. **【P1，GPT 漏】memory._embed 在 QIDIAN_SKIP_EMBED 崩溃** —— 需要带特定环境变量实跑才暴露，静态扫描看不到。实测 AttributeError。
3. **【P1，GPT 漏】_run_queue_v3:479 `fut.result()` 未捕获异常** —— 单 worker 抛错炸整批循环；GPT 只盯 worktree 对称性，没追异常传播路径。
4. **【P2，GPT 漏】orchestrator._process_batch 是死代码** —— 与 _finalize_result 重复 42 行却无调用方。GPT 的 D3 只比了 roles.py，没发现核心调度文件里的大块死重复。
5. **【P3，GPT 漏】_exec.final_turn 恒为 0** —— 失败任务轮次观测失真。数据流 bug，grep 无能。
6. **【GPT 误报纠正】verify_laws.py 不存在** —— GPT 在 D4-1 引用了 `verify_laws.py:58,116,189` 三个行号，该文件根本不在 `scheduler/` 下。这是 GPT 凭模式幻想的文件，3 个"裸 except"证据无效；真实裸 except 仅 3 处且全在 _exec/orchestrator。

---

### 复核口径声明
- 所有"成立/漏报"均有 文件:行号 或 实测命令支撑；定级以"是否会炸 / 是否静默腐化"为准，不照搬 GPT 的 P 值。
- D6（循环导入）整维 GPT 定 P0×5，实测无一导致加载失败 → 整体降为 P2 可维护性问题。这是 GPT 与本轮最大分歧。
- 未触发任何真实模型 API（遵守 scheduler 测试约束）；验证仅用 import 与 AST。

DONE
