# 奇点 Agent 平台调度层

> 本文件以审计后落地的代码为准。
> v1 最小闭环 = 现在能跑的；完整版 = 目标，未实现项明确标注。

---

## v1 最小闭环（已实现，14 文件 1469 行）

```
python3 -m scheduler "任务"
   │
   ① router.py        复杂度 E/D/E+ + gate_required + task_type
   │                  开头动词消歧 (审计 2.2)
   ② pre_search.py    调 search.py 查 decision 域
   │                  强 D (≥2 条 score>15) 升 D, 5s 超时降级
   ③ snapshot.py      写入前 git 全项目快照 (git stash 优先)
   ④ dispatcher.py    读 agents.toml 选 executor, 按需升级 (终止于 D)
   ⑤ validator.py     validate.py (真实 2 档) + gate_required 时 eval.py --gate
   │                  最硬者优先: gate失败 > 人工复核 > 注意
   ⑥ neijinglu.py     变更清单 + verdict + unverified + snapshot_id
```

### v1 能做
- 三类 executor: claude-cli (E/D) / zhipu-api (E+ 含超时/限流/格式异常三类错误)
- gate_required: 命中 core.py/tokenizer.py/graph.py/search.py → 强制 eval.py --gate
- 写入前 git 全项目快照 + rollback 子命令
- E+ 产出不自动落盘 → patch 文件 + apply 子命令 (保安全边界)
- 打回循环: validate 不通过附全文给同 agent, max_turns 耗尽升级
- 诚实交付: `delivered_unverified` 状态把"通过≠已验证"写进报告
- trace 永远带 snapshot_id, 回滚失败可人工恢复

### v1 砍了什么 + 为什么

| 砍的 | 原因 | 状态 |
|------|------|------|
| dependency_graph / affected_area | graph.py 是知识图谱(wikilink)非代码依赖图, 错配 | 完整版待建 (ast/jedi) |
| diff_review LLM 语义层 | "不用 LLM 路由"与"语义审查需 LLM"矛盾; 只留硬规则层 | 硬规则层 v1 未启用 |
| validate.py 的"信息不足/证据不完整"档 | 代码不输出这两档, 映射表对的是幽灵 | 对齐真实 2 档 |
| 任务类型三分支验证链 | refactor 链依赖不存在的 dependency_graph | task_type 仅标注不分支 |
| bug 修复验证 | 项目零测试, 无法验证修复 | 强制标 unverified |
| agents.yaml → agents.toml | 环境 无 pyyaml, tomllib 是 stdlib 零依赖 | 已换 |

### v1 已知粗糙 (非 bug, 标注但不阻塞)
- task_type 标注有噪声: "那个重构我们要审一下" → type=refactor (实为审查)。
  v1 task_type 不驱动验证链分支, 仅做 unverified 标注, 噪声可接受。
- pre_search 强 D 阈值 (≥2 条 score>15) 是经验值, 待 golden_set 校准。

---

## 完整版（目标，未实现）

下列为旧 v3 设计稿内容, 审计后明确标注为"未实现/待建"。
v1 跑通后再逐项评估是否真要做。

### 待建组件
- **dependency_graph**: 代码符号级依赖图 (ast/jedi), 不复用 graph.py。
  refactor 链的影响面分析依赖它。
- **diff_review 硬规则层**: diff 删安全测试/弱化安全边界 → 标红。
  命中后升级 architect(D) 做 LLM 语义审查。
- **测试体系**: bugfix 链验证修复、内景录"全量"都依赖它。
  v1 用 unverified 标记绕过, 是债。

### 待评估决策
- **任务类型真分支**: v1 砍了分支, 完整版是否要恢复取决于 dependency_graph 建不建。
  若建, refactor 链才有意义; 不建则永远 type 仅标注。
- **validate.py 补"信息不足/证据不完整"两档**: v1 对齐了真实 2 档。
  完整版若 validate.py 扩档, 映射表要同步。

---

## 审计历史

- **v3 审计** (本轮): 6 处宣称实现为零/错配; validator 映射表对幽灵 verdict;
  refactor 链建在知识图谱错当代码依赖图; E+ API 集成空白。
  → 落地 v1 最小闭环, 砍掉实现为零项, 对齐真实契约。
- **关键决策**: 路由用规则不用 LLM; GLM 走 HTTP API 不中转;
  写入前强制快照 (借天工 Step 6); 失败兜底不锁死 (降级退出优先);
  单向反馈通道 (scheduler→agent), 第一版不建 agent 间通信。
