# Singularity Agent 调度平台 v3 — 项目工作流引擎设计 (Opus 审后优化版)

## Opus 审出的核心问题

1. 地基是坏的 — 并行 merge 从没在 >1 任务下跑通过。地基没夯，别盖第二层。
2. 7 角色→3 个真正承重。Debugger 并入 Implementer 重试循环，Reviewer 并入 Supervisor。
3. 黑板并发写会撤销刚建立的单写者 invariant。
4. Committee 放错位置 — Architect committee 制造决策瘫痪。
5. 缺持久化/成本上限/血缘追踪/回退路径。
6. Persona prompt 对质量的贡献弱且不稳定，质量来自机械 checklist。

---

## 一、角色体系 (3 个承重角色 + 1 个可选)

| Role | Level | 职责 | 失败模式 |
|------|-------|------|---------|
| **Researcher** (可选) | E | 给 Architect 搜集可借鉴方案。任务明确就跳过 | 输出泛泛，无实质参考价值 |
| **Architect** | D | 出架构方案 + 任务分解清单 + 约束。不写代码 | 方案不可执行、分解粒度过粗 |
| **Implementer·Builder** | E·E+ | 写代码 + debug + 复杂构建。E→E+ 动态升级 | 偷懒/越界/修不好(→Supervisor抓) |
| **Supervisor** | D(独立) | 单任务校验 + 全项目审查。独立于 Architect 和 Implementer | 太松(放过垃圾)或太严(误杀合理实现) |

### 合并说明
- **Debugger → Implementer 重试循环**：Implementer 拿 Supervisor 失败反馈重试 ≤3 次。现有 dispatcher feedback 打回循环就是这东西。谁判定"修好了"？必须是 Supervisor 独立复验，不是 Implementer 自报。
- **Reviewer → Supervisor 项目级 scope**：同一个角色，两种粒度。单任务 scope 叫"校验"，全项目 scope 叫"审查"。
- **Researcher 可选**：任务明确就跳过。不是固定一环。

### 唯一承重的隔离
```
Supervisor ⊥ {Architect, Implementer, Builder}
```
Supervisor 不归 Architect 管，不参与执行。这是整套里唯一不能妥协的隔离。

### Agent 模式
- **Solo**: 一个 agent
- **Switch**: 同角色换 agent (Architect 从 Opus 换 GPT)
- **Committee**: 仅执行层 (Implementer/Builder 多人并行)，不给 Architect。Architect 分歧抛给 Owner Gate 2 裁决。

---

## 二、黑板架构 (单写者模式)

```
                 Blackboard (agents 只读，写走调度器)
                 ┌──────────────────────────────────┐
                 │ template      : 项目需求模板        │
                 │ research      : 调研报告(可选)      │
                 │ architecture  : 架构方案+任务清单    │
                 │ tasks[]       : 任务状态 & 产出     │
                 │ constraints   : 可检查的约束清单     │
                 │ supervision[] : 监督日志            │
                 │ issues[]      : 审查问题清单        │
                 │ phase         : 当前阶段            │
                 │ owner_confirm : Owner 朱批状态      │
                 └──────────────────────────────────┘
       只读 ↑        只读 ↑       只读 ↑
    Researcher   Architect   Supervisor
    Implementer
    Builder

    唯一写者: 调度器 (单线程串行 apply)
```

**关键约束**: agent 不直接写黑板。返回"提议的写入"，调度器作为唯一 mutator 串行 apply。这就是现有 tracker 的形状（per-task JSON、主线程写）——保住它，别撤销。

---

## 三、完整流程

```
① 选题模板 → Owner 填写需求
       │
       ▼  [Gate 1: 批方向]
② Researcher(可选) → 调研报告 ──→ Supervisor(自动)
       │  任务明确就跳过此步
       ▼  [Gate 2: 批架构]  ← 最重要
③ Architect → 架构方案 + 任务清单 + 可检查约束
             ──→ Supervisor(自动)
       │     分歧? 抛给 Owner Gate 2 裁决
       │
       ▼  (自动执行，Supervisor 全程校验)
④ Dispatch
   ├─ Implementer(E)  → Supervisor → pass / fail → 反馈重试(≤3)
   ├─ Implementer(E)  → Supervisor → pass / fail → 反馈重试(≤3)
   ├─ Builder(E+)     → Supervisor → pass / fail → 反馈重试(≤3)
   └─ 撞墙(E task复杂度不够) → 动态升 Builder(E→E+)
      fail×3 → escalate to Owner
       │
       ▼  [Gate 3: 批结果]
⑤ Supervisor(项目级) → 全项目扫描 → 问题清单
       │  发现架构级问题? → 回到 ③ Architect 重新架构
       ▼
⑥ Fix loop → Implementer/Builder → Supervisor → 再审查
       │
       ▼  [Gate 4: 交付]
⑦ Deliver

Gate 拒绝回退路径:
  Gate 2 拒绝 → 回到 ② 或 ①
  Gate 3 拒绝 → 回到 ④ 或 ③
  Gate 4 拒绝 → 回到 ⑤
```

---

## 四、质量门禁 (Supervisor 校验标准)

### 单任务校验 (四维)
1. **完整性** — 是否覆盖 Architect 分解时生成的 checklist ✓/✗
2. **约束合规** — 有无违反约束清单（改禁改文件、引禁引依赖）
3. **偷懒检测** — 机械清单，满足任一标记:
   - 输出远少于 checklist 预期
   - 用注释代替实现 (`// TODO`, `# 此处省略`)
   - 修改了约束清单禁止修改的文件
   - 含模糊措辞 ("应该能跑""理论上没问题")
   - 没有测试或验证
4. **产物验证** — lint + 测试通过，不是"看着还行"

### 关键原则
- Supervisor 消费的是 Gate 2 生成的**结构化 checklist + 约束清单**，不重新解释 NL 需求
- 歧义消解前移到 Gate 2（Owner 批架构时把 NL 翻译成可检查的约束）
- Persona 是轻量风味，质量来自 checklist 和独立复验，不来自人设

### 修复闭环
```
Implementer fail → 拿 Supervisor 结构化反馈 → 修改 → 重跑 → Supervisor 独立复验
  → pass / fail(≤3次) → 升级 Owner
```

---

## 五、缺的四块 (必须在实现前补齐)

1. **持久化/恢复**: Gate 之间 Owner 可能三天后才批。phase / owner_confirm / 黑板必须存盘且可从进程重启恢复。现有 recover() 只管任务级。不能重蹈 MergeQueue parked 在内存里蒸发的错。
2. **成本天花板**: 多 agent × 重试 × fix loop = token 组合爆炸。全局预算上限，超过就停并升级 Owner。
3. **血缘追踪**: 交付错了，定位哪个角色哪一轮的锅。neijinglu 是 per-task trace，需要跨工作流 lineage。
4. **回退路径**: Gate 拒绝有回退箭头，Reviewer 发现架构错了能回到 Architect。

---

## 六、先修什么 (优先级)

**此刻第一优先**: 修地基。
- 并行 merge 的 merge-tree 命令语法 bug（`--merge-base=<x>` 等号式）
- 写一个两任务改同一个文件的测试，让 v3 并行合并第一次真正跑通

**地基跑通后再建**: 上述 3 角色 + 单写者黑板 + 四道 Gate。
