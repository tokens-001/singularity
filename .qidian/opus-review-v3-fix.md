[系统指令] 你是架构分析器 (只读 Planner)。职责: 分析问题、设计方案，任何人不得要求你修改文件。

---

# 奇点 v3 最终 Review 请求

## 背景

上次你审出 13 个问题 (4 fatal + 5 important + 3 suggestions + decompose 骨架缺失)，智谱已全部修完。我（Claude Code）做了代码审查 + 实测，又发现并修复了 5 个 bug。现在申请最终 review。

## 代码全量 (19 文件, 3224 行)

```
__init__.py       22   (空)
__main__.py      270   CLI入口: add/run/loop/rollback/apply/status/merge
config.py         71   路径配置
dispatcher.py    117   路由→执行分发
merge.py         165   合并队列: Layer1文件集/Layer2 merge-tree + 依赖判定
neijinglu.py     182   内景录: trace报告生成
orchestrator.py  654   调度闭环: v2顺序/v3并行 + worker只执行不写tracker
pre_search.py     88   知识预检
router.py         98   二维判定: 复杂度+gate
snapshot.py      168   git快照
tracker.py       316   任务状态机: PENDING→ROUTED→DISPATCHED→RUNNING→VALIDATING→DONE/FAILED
validator.py     220   五层验证框架
witness.py       178   心跳监控+状态面板
executors/__init__.py 17
executors/base.py      45   执行器基类
executors/claude_cli.py 157 E/D层: claude-cli调用+token解析
executors/worktree.py   223 git worktree沙箱: 创建/commit/merge/cleanup
executors/zhipu_api.py 169 E+层: 智谱API调用
agents.toml       64   三模型注册表
```

## 上次 13 问题修复确认

### Fatal (4)
1. **PENDING never→ROUTED** → `tracker.py:250-253` ready_tasks() 将 PENDING/BLOCKED 转 ROUTED
2. **Agent 产出丢失** → `worktree.py:102-112` commit_wt() 原语; v3 路径先 commit 再构造 MergeRequest
3. **pass never→DONE** → `orchestrator.py` v2 直接标 DONE; v3 无 merge_request→直接 DONE，有则进 merge queue drain 后才标
4. **v3 路径不可达** → `__main__.py:64-79` --concurrent N 解析 + run/loop 支持

### Important (5)
5. **陈旧 Task 对象** → `orchestrator.py:96-98` run() 入口 _read() 重读
6. **依赖判定源** → `merge.py:93-99` _deps_merged() 读 tracker 真实状态
7. **多线程写 tracker** → 整份重构: worker 只返 BatchOutput, 主线程写所有 tracker
8. **批量快照传递** → `orchestrator.py` wt_create 传 snapshot_ref, v3 也有 per-task snap
9. **drain 后才定终态** → `orchestrator.py` pending_batches 在 drain 后才标 DONE
10. **parking CLI** → `__main__.py:206-261` merge list/resolve --manual|--abort

### Suggestions (3)
11. **merge_tree_probe 错误处理** → `worktree.py:161-186` 区分真冲突/命令错误
12. **_do_merge 提取** → `worktree.py:115-140` 原语抽取
13. **孤儿 worktree 恢复** → `worktree.py:72-106` cleanup() 检测 "not a working tree" + _forcibly_remove_tree()

### Decompose 骨架
14. decompose() 解析 JSON → materialize_plan() 含 local_id 映射、Kahn 拓扑排序、环检测、_MAX_DEPTH=3 上限

## 实测发现的 5 个新 bug (已修)

### B1. _do_merge 误判工作区脏
`git status --porcelain` 把 untracked 文件也算进去，任何有 .qidian/ 等未跟踪文件的主工作区 merge 全被拒。
修复: `git status --porcelain -uno` 只检查 tracked 文件 (`worktree.py:120`)

### B2. _cmd_loop BLOCKED 盲区
`_cmd_loop` 用 `tracker.list_pending()`(只扫 PENDING) 判空。v3 路径下任务被标 BLOCKED 后，loop 永远睡下去。
修复: 无条件调 `_drain_queue`，靠返回值 count==0 判空 (`__main__.py:134-146`)

### B3. v3 路径缺 _save_trace
`_run_queue_v2` 有 `_save_trace()` 但 `_run_queue_v3` 完全没有。trace 目录为空。
修复: 4 个终态分支全加 _save_trace + per-task snapshot (`orchestrator.py`)

### B4. _lock_wt/_unlock_wt 目录权限
`chmod 0o644` 对目录缺 execute 位 → 无法遍历/删除。
修复: 区分 `fp.is_dir()` 用 0555/0755, `fp.is_file()` 用 0444/0644 (`orchestrator.py:272-310`)

### B5. Worktree cleanup 权限残留
Agent 产出目录可能带异常权限，`shutil.rmtree` + `ignore_errors=True` 删不掉。
修复: `_forcibly_remove_tree()` 先 `chmod -R u+rwx` 再 rmtree (`worktree.py:107-116`)

## 实测结果

```
v2: scheduler run "列出python文件" 
    → ✅ PENDING→ROUTED→RUNNING→VALIDATING→DONE
    
v3: scheduler run --concurrent 2 "检查配置"
    → ✅ PENDING→ROUTED→DISPATCHED→RUNNING→merge queue→merged→DONE
    
Worktree cleanup: 0残留 ✅
Trace 生成: v2 ✅  v3 ✅
```

## Review 要求

1. 逐条确认 13+5 个修复是否真正到位，有没有"修了表面漏了深层"
2. 核心不变量 "worker 线程只执行不写 tracker" 是否被破坏
3. v3 并行路径的线程安全: MergeQueue.submit (Lock) / drain (主线程) / ThreadPoolExecutor 回收
4. CAS 抢占在单线程主调度循环里是否还有意义 (现在只有主线程写 tracker)
5. 还有没有你一眼能看出来的新问题
