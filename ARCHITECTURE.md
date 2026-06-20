# 奇点调度平台 — 全量审计 + 架构文档

> 2026-06-20，15 commits，35 文件，+1910/-842 行，88/88 测试绿。
> 基于 Opus 审计报告 + 全量 48 文件扫描 + 两轮修复。

---

## 一、审计范围

| 维度 | 方法 | 覆盖 |
|------|------|------|
| 首轮审计（Opus） | 7核心+12关联文件深读 | P0-P1 全覆盖 |
| 全量扫描（三代理并行） | 48文件代码质量/架构/安全 | P2-P3 全覆盖 |
| 交叉验证 | 逐条实测，去误报 | 剔除 commit message 幻觉×2、"待做"实际已做完×6 |

## 二、已解决问题（15 commits）

### CRITICAL — 不改会炸

| 问题 | 位置 | 修复 |
|------|------|------|
| `now` 变量未定义 → 全 API 500 | app.py:195 | 补 `now=time.time()` |
| `shell=True` 命令注入 | mcp.py:91 | `shlex.split` + `shell=False` |

### HIGH — 不改会泄漏/绕过

| 问题 | 位置 | 修复 |
|------|------|------|
| worktree 8条退出路径泄漏 | _exec.py | try/finally 框死生命周期 |
| fut.result() 裸调炸整批 | orchestrator.py | try/except 兜底标 FAILED |
| rollback 模块不存在，rollback_all 无守卫 | _api.py | 删死函数，task_rollback 保留 ImportError 守卫 |
| _safe_path 前缀绕过 | openai_agent.py | `+ os.sep` 防 `/a/b` 匹配 `/a/bb/` |

### MEDIUM — 不该腐化

| 问题 | 位置 | 修复 |
|------|------|------|
| tracker 双线程写 lost-update | tracker.py | `_LOCK(RLock)` 封 read-modify-write |
| worktree.py 放错包，7处分层违规 | executors/→scheduler/ | 上移为 `_git_worktree.py` |
| 安全响应头缺失 | app.py | CSP+X-Content-Type+X-Frame+Referrer |
| _RATE_BUCKETS 无线程锁 | app.py | `_RATE_LOCK` 保护读写 |
| _embed 在 SKIP_EMBED 下崩溃 | memory.py | None 守卫 |
| dispatcher 每次全量加载 skill/MCP | dispatcher.py | 按 (level,model) 缓存 + 7 失效点 |
| 40+ 裸 except → 吞 KeyboardInterrupt | 6 文件 | 全部 → except Exception |
| 3 处裸 except | _exec/orchestrator | → except Exception |
| 死代码 _process_batch | orchestrator.py | 删除（与 _finalize_result 90% 重复） |
| skills API 三连 bug | _api.py | args→arguments, dict→list, 补 source |
| 前端技能列表恒空 | app.js | 适配新 API 格式 |
| final_turn 恒为 0 | _exec.py | 每 turn 更新 |

### LOW — 清理

| 问题 | 位置 | 修复 |
|------|------|------|
| 死代码 _model_cost_tier | dispatcher.py | 删除 |
| 死代码 try_parse_json_list | _io.py | 删除 |
| 死代码 rollback_all | _api.py | 删除 |
| JSON 解析重复（conductor+execution_judge） | 2 文件 | 统一到 `_io.try_parse_json`，-33 行 |
| 类型注解 10 文件缺 `from __future__` | 10 文件 | 各加一行 |
| project_id 无格式校验 | app.py | 正则校验 `[a-zA-Z0-9_-]{1,64}` |
| beam/hops/depth 无 try/except | app.py | 补 ValueError 守卫 |
| TOML 加载 5 处重复 | 5 文件 | 统一到 `_io.load_toml` |
| 前端深色主题/字号/层级/状态色 | style.css+app.js | Opus token 体系完整实施 |

### 前端

| 问题 | 修复 |
|------|------|
| 4 个 500 端点 | 路由修正 |
| 字号/层级/状态色/交互态/事件流 | 重设计 |
| 精致深色主题 | Opus token 体系 |
| Skills 面板恒空 | API 格式适配 |
| tool/turn/approval/subagent SSE 事件 | 5 种新事件 + 前端渲染 |
| SSE 错误处理 | try/catch + 重连 |

### 测试

| 套件 | 数量 | 覆盖 |
|------|------|------|
| smoke_test.py | 43 | API CRUD + 前端 + 边界 |
| test_exec_run.py | 21 | run() 8 条路径 + worktree 对称不变量 |
| unit_tests.py | 24 | 项目状态/路由/调度策略/缓存/拓扑 |

---

## 三、遗留问题（有意不修）

### 不能修：修了反而出事

| 问题 | 原因 |
|------|------|
| _api.py 40+ 延迟导入 | 刻意打断 `orchestrator→_exec→dispatcher→orchestrator` 循环依赖。提到顶层 = 启动炸模块 |
| 消灭全部延迟导入（P2-5） | 同上，ROI 极低 |

### 不用修：不会出事

| 问题 | 原因 |
|------|------|
| API 路由统一 try/except | Flask 默认 500 兜底，不泄露堆栈 |
| cancel 竞态孤儿文件 | benign，最多延迟一个 turn，自动回收 |
| executor 内延迟导入 witness | 只用于失败日志，影响极小 |
| _auth/_profiler/_token_budget 静默 pass | 观测失败不影响主流程 |

### 不值修：太贵，感觉不到

| 问题 | 原因 |
|------|------|
| 循环导入架构解耦（真正根治） | 需重新设计模块边界，3-5h Opus 级别判断，当前无预算 |
| thinking delta 字符流 | SSE 扛不住碎片，Web 不需要打字机效果 |
| 实时文件 diff 预览 | TUI 特有能力，Web 做要 Monaco+diff 库，太重 |
| 审批超时自动拒绝 | 先手动审批，写一堆代码几乎用不到 |

### 暂不做：等需要时再做

| 问题 | 原因 |
|------|------|
| Skill `type: flow` 多步骤工作流 | tool+prompt 两种够用，等不够了再加 |
| Skill 市场和下载 | 单人本地工具，无多用户需求 |
| Skill 间依赖声明 | 当前 skill 数量少，手动管理即可 |
| Skill 热加载 | 开发期重启两秒可接受 |
| 前端框架切 React | `app.js` 1700 行仍在可控范围。超过 3000 行后再切——目前原生 JS 够用，切了新增 npm 依赖+构建工具，边际收益不大 |

---

## 三·五、Fusion 多模型融合（下一方向）

### 核心思想

用多个便宜模型并行执行 + 合成器交叉比对，替代单个昂贵模型。借鉴 OpenRouter Fusion 完整设计。

### OpenRouter Fusion 深度解析

**架构**：并行派发 → 裁判分析 → 调用模型定稿。三阶段，服务端一体，API 侧只看到一个调用。

**并行派发**：同一 prompt 同时发给面板所有模型，每个模型拥有相同工具集（web search / web fetch / bash）。工具平等是关键——不能给某个模型更多工具。

**裁判分析**（两阶段合成）：

阶段一——裁判模型输出结构化 JSON，五个维度：
- `consensus`：所有模型一致的点 → 最高置信，直接锁定
- `contradictions`：模型间互相矛盾的点 → 裁判基于证据裁决
- `partial_coverage`：部分模型覆盖但其他人没提到的点 → 标记置信度
- `unique_insights`：只有一个模型提出的独到见解 → 保留但标注来源
- `blind_spots`：所有模型都遗漏的点 → 调用模型补充

阶段二——调用模型基于五维分析写出最终答案。**不是裁判直接写答案，是裁判分析 + 调用模型定稿**。

**Self-Fusion 发现**：Opus 4.8 ×2 自我融合 = 65.5%，单跑 = 58.8%，提升 +6.7 分。说明**合成步骤本身的收益独立于模型多样性**——同一模型跑两遍，推理路径不同，交叉比对仍然增值。

**选择性调用**：Fusion 不是替代所有模型调用。对于编程任务，基础模型处理日常代码，只在遇到架构决策/最佳实践研究时调 Fusion。由模型自己判断何时需要多视角。

**防作弊**：基准测试中模型能通过 web search 找到评分标准。解决方案：web search/fetch 加 `excluded_domains`。

**Token 预算影响**：Opus 4.8 单跑得分低（58.8%）因为它"更饥饿"——需要更多工具调用才能发挥。限制工具调用预算会压缩模型间差距。

### 奇点 Fusion 合成器设计

```
N 个模型各自产出
       ↓
  阶段一：裁判分析（便宜模型，如 DeepSeek V4 Pro）
    输出结构化 JSON：
    {
      "consensus":     ["所有模型一致的点"],
      "contradictions": [{"point": "...", "model_a": "...", "model_b": "...", "resolution": "..."}],
      "partial_coverage": [{"point": "...", "covered_by": ["model_x"], "confidence": "high"}],
      "unique_insights": [{"point": "...", "source_model": "kimi"}],
      "blind_spots":   ["需求要求但所有模型都没覆盖的点"]
    }
       ↓
  阶段二：调用模型基于五维分析定稿
    输入：原始需求 + N 份产出 + 五维分析
    输出：一个融合答案
```

### 三级火力

| 级别 | 执行面板 | 合成裁判 | 成本 | 适用 |
|------|---------|---------|------|------|
| **Budget Fusion** | DeepSeek Chat + GLM-5.2 + Kimi K2.6 | DeepSeek V4 Pro | ~$0.5/任务 | 日常代码改动 |
| **Self Fusion** | DeepSeek Chat ×2（同模型跑两遍） | DeepSeek V4 Pro | ~$0.2/任务 | 最小成本提升 |
| **Standard Fusion** | Opus 4.8 + GPT-5.5 + Claude Sonnet | Opus 4.8 | ~$3/任务 | 架构重构 |
| **Super Fusion** | 全模型池 × 多视角 × 多轮迭代 | 多层裁判 + 最终人工确认 | 无上限 | 关键决策 |

### Super Fusion 展开

- **多视角分解**：同一任务从架构/安全/性能/可维护性 4 个视角独立分析
- **对抗验证**：每份产出经 2 个 red-team 裁判攻击性审查
- **迭代熔合**：第一轮合成 → 各模型审查合成结果 → 第二轮修正 → 最终输出
- **人工卡点**：关键决策（架构选型/安全边界/API 破坏性变更）暂停等人工确认

### 选择性调用策略

不是每个任务都走 Fusion。路由判定：
- `route_type=simple`（简单 bug/单文件改动）→ 单模型
- `route_type=complex`（架构/跨模块/安全）→ Fusion
- 模型可自主决定：coding agent 在处理过程中认为需要多视角时，主动调 Fusion 工具

### 改动范围

| 文件 | 改动 |
|------|------|
| `router.py` | 加 `fusion` 路由类型，判定走 Fusion 还是单模型 |
| `_exec.py` | 加 `_run_fusion()` — 并行派发 + 收集 + 合成 |
| `execution_judge.py` | 加 `fuse_outputs()` — 四步合成裁判 |
| `orchestrator.py` | 加 Fusion 模式的任务分叉逻辑 |
| 前端 | Config tab 加 Fusion 面板配置（模型面板+级别选择） |

### 难点与方案

#### 难点 1：worktree 文件冲突

**问题**：现在每个任务一个 worktree，agent 在里面直接改文件。三个模型并行改同一个 worktree → 文件冲突，git 状态混乱。

**为什么 OpenRouter 没这个问题**：Fusion 做的是文本推理（研究/分析/论证），不是代码修改。模型只输出文字，不操作文件系统。

**方案**：Fusion 模式改为**两阶段执行**：
1. 分析阶段（并行）：各模型读取代码，产出"方案 + 建议 diff"，不实际修改文件
2. 执行阶段（单模型）：裁判选定最优方案后，由调用模型在 worktree 中执行实际改动

这样 worktree 只被一个模型修改，避免了并发写冲突。

#### 难点 2：代码 diff 比对 ≠ 文本分析比对

**问题**：文本任务里裁判比的是"哪个论点更全面"。代码任务里裁判要比的是"哪个改动正确、最小、不引入副作用"。便宜裁判（DeepSeek V4 Pro）能判断论点好坏，但判断代码正确性的能力有限——可能选了一个看起来更短但缺少边界处理的方案。

**方案**：
- 裁判不做 code review——只做**结构化分析**：哪个方案更简洁（行数少）、覆盖了更多边界条件、引入了更少依赖
- Self-Fusion 更安全：同模型审自己的另一份产出，理解和判断能力一致。跨模型时便宜裁判审 Opus 的代码可能误判
- 最终方案仍然过 validator 和 smoke/regression 测试，不依赖裁判的主观判断

#### 难点 3：延迟 ×N

**问题**：三个模型并行，总时间 = 最慢模型的时间 + 裁判时间。如果面板里有慢模型（如 Opus 8 秒），总体延迟 10 秒+，是单模型的 2-3 倍。用户体验差。

**方案**：
- Self-Fusion 先上：同模型 ×2 速度差距小（都是 DeepSeek，1-3 秒），总延迟接近单模型
- 设置面板超时：超过阈值没返回的模型不纳入合成
- 异步 Fusion：长任务可以后台 Fusion，用户先拿到单模型结果，Fusion 结果作为"补充意见"追加

#### 难点 4：合成提示词设计

**问题**：裁判模型需要输出结构化五维分析 JSON。如果提示词设计不好，裁判可能输出散文而非 JSON，或者漏维度。

**方案**：
- 五维 JSON schema 固定：`{consensus:[], contradictions:[{point,model_a,model_b,resolution}], partial_coverage:[{point,covered_by,confidence}], unique_insights:[{point,source_model}], blind_spots:[]}`
- 每个维度独立请求裁判（减少单次复杂度），或用一个精心测试过的合并 prompt
- 先用 DeepSeek Chat 小任务跑通，再上复杂场景

### 开源项目深度借鉴

#### 1. HermesFusion — 模型无关的 Fusion 运行器

**核心设计**：每个面板成员就是一个配置好的命令行。完全不关心模型提供商——`hermes` CLI、`ollama run`、`openai chat`、`curl`，什么都能接。

**可借鉴的配置格式**：
```yaml
providers:
  fast:
    command: ["hermes", "-z", "{prompt}", "--provider", "openai", "--model", "gpt-4o-mini"]
    role: "Quick analyst."
    timeout_seconds: 60
modes:
  lite:
    max_participants: 2      # 硬上限，防配置错误炸配额
    max_calls_per_run: 4
    participants: [fast, strong]
    judge: strong
    synthesizer: strong
```

**安全设计可直接抄**：
- 文件锁（flock）防并发跑两次
- `--prompt-file` 路径沙箱化，拒绝符号链接
- 子进程设 `HERMESFUSION_CHILD=1` 环境变量，防止递归调用炸配额
- `disabled` 映射：关掉某个 provider 不用删配置，标一行 `"out of credits"` 即可

#### 2. model-fusion (0xLeathery) — Self-Fusion + 角色多样性

**核心发现**：75% 的提升来自合成步骤本身，只有 25% 来自模型多样性。基于这个发现，直接用**同一模型 + 不同角色**实现 Self-Fusion。

**三个角色（Persona）**：
| 角色 | 职责 |
|------|------|
| `fusion-skeptic` | 假设显而易见的答案是错的，找反例 |
| `fusion-builder` | 最完整、最具体、最可执行的方案 |
| `fusion-analyst` | 深挖最关键的单一维度 |

**合成提纲（6 项，可抄）**：
1. Claim ledger — 列出所有主张
2. Correlated-error check — 多个模型犯同类错误 → 可能是 prompt 歧义
3. Evidence-based contradiction resolution — 矛盾不靠投票，靠证据
4. Coverage union — 取所有模型的覆盖面并集
5. Calibration — 标注每个结论的置信度
6. Anti-majority guard — 少数派意见如果证据充分，保留不丢弃

**选择性路由**：不调模型，纯启发式判断 prompt 是否值得 Fusion：
- prompt 长度 >320 字符
- 包含 ≥2 个问号
- 包含分析性关键词（analyse/compare/design/architecture/trade-off/evaluate/root cause/prove/why）

`FUSION_MODE` 三档：`off`（不触发）/ `selective`（启发式）/ `always`（全触发）

#### 3. Doubt-Driven Development — 对抗审查 Skill

**五步反证法**（可直接做成奇点 Skill）：

```
CLAIM → EXTRACT → DOUBT → RECONCILE → STOP
```

关键设计：
- **审查者永远看不到 CLAIM**——只给 ARTIFACT（产出）+ CONTRACT（契约/需求），防止审查者被原结论带偏
- 审查者的任务不是验证，是**找问题**："Assume the author is overconfident."
- 跨模型升级强制提示："单模型审查完毕。要跨模型第二意见吗？（Gemini CLI / Codex CLI / 手动 / 跳过）"
- 3 轮上限，防止死循环
- 4 类发现分类：Contract misread（误读需求）/ Valid+Actionable（有效可操作）/ Valid Trade-off（有效但可接受）/ Noise（噪音）

#### 可直接落地的优先级

| 优先级 | 借鉴内容 | 落地方式 |
|--------|---------|---------|
| P0 | Doubt-Driven Development | 做成 `type: prompt` Skill，现在就加 |
| P1 | model-fusion 选择性路由 | `router.py` 加启发式难度判断 |
| P1 | model-fusion 合成提纲 | `execution_judge.py` 的 `fuse_outputs()` 用 6 项提纲 |
| P2 | HermesFusion 配置格式 | 奇点 Fusion 面板配置 YAML 格式 |
| P2 | 角色多样性（skeptic/builder/analyst） | Self-Fusion 的 prompt 模板 |

### 前沿理论深度挖掘

#### 1. 少上下文反而更好（Microsoft，2026.06）

**实验**：GPT-5 在 50 项企业报销任务上，对比四种上下文策略。

| 策略 | 完成率 | Token 消耗 | 耗时 |
|------|--------|-----------|------|
| 无用户模型 | 8.0% | — | — |
| 全量历史 | 71.0% | 1,480,996 | 14.56h |
| 只保留最近 5 次工具调用 | 79.0% | 535,274 | 5.39h |
| **最近 5 次 + 摘要** | **91.6%** | **553,374** | **5.79h** |

**核心结论**：保留全量历史不仅浪费 token（147 万 vs 55 万），而且完成率更低（71% vs 91.6%）。旧的工具调用记录会产生"干扰信号"，让模型在无关历史上迷路。

**对奇点的直接应用**：executor 的 `_exec.run()` 当前保留全部 `all_tool_events`，应该改成：
- 只保留最近 N=5 次工具调用完整记录
- 之前的工具调用压缩为一行摘要（"读取了 3 个文件，修改了 2 个文件"）
- 不影响 worktree 操作——工具事件只是观测数据，不是执行状态

#### 2. LLM-as-Code（KDD 2026 AgenticSE Workshop）

**核心论**：把 LLM 当编排器是根本性的架构错误。token 爆炸、控制流幻觉、不可靠终止——不是 prompt 不够好，是让概率系统做确定性工作必然失败的后果。

**方案**：程序掌控所有控制流（循环/分支/顺序），LLM 只是被调用的自适应组件。LLM 不能改变程序的执行路径。上下文变成 DAG——每次调用的上下文长度由调用深度决定，而不是步骤累积。

**和奇点的关系**：奇点的 orchestrator 已经在正确的方向上——`_run_queue_v3` 用 `ThreadPoolExecutor` 控制并发，`run()` 有明确的 turn 循环上限。但 executor 内部，agent 仍然可以自主决定工具调用顺序和次数。LLM-as-Code 的启示是：进一步收紧——让 orchestrator 决定"现在该调哪个工具"，agent 只负责"这个工具怎么用"。

#### 3. NTILC 神经工具检索（2026.06）

**问题**：工具定义全量塞进 prompt → 上下文消耗随工具数量线性增长 → 不相关工具干扰选择精度。

**方案**：用户意图和工具规格映射到共享嵌入空间。工具选择走外部检索而非上下文内查找。模型只看到被选中的工具 schema。Circle Loss + Functional Margin Loss 确保语义相似但签名不兼容的工具被分离开。

**效果**：上下文减少 >95%，推理延迟降低 74%。

**对奇点的应用**：dispatcher 当前全量加载 skill 和 MCP 工具定义。可以加一个轻量嵌入层——用 `_embed()` 把任务描述和 skill 描述分别向量化，只加载 top-3 相关的 skill 完整定义，其余只给名称和一行描述。

#### 4. DCPM 双进程认知记忆（Tencent，2026.06）

**架构**：借鉴 Kahneman 双进程理论。

**System 1（日间写入器，同步）**：记录信念修订为双向链接的"取代链"——不只是记录"现在相信什么"，还记录"从什么修订而来"。

**System 2（夜间引擎，异步）**：在空闲时从原始事实中归纳 schema、意图和跨域模式，压缩成高层核心 schema。

**认知能力层级**：原始输入 → 原子事实 → 历时信念轨迹/身份 → 领域 schema → 潜在意图 → 跨域核心 schema

**效果**：在需要跨会话隐性推理的任务上 +5.2%（PersonaMem-v2），在简单回忆任务上无提升——完全符合架构预测。

**对奇点的应用**：MAGMA 当前只有 System 1（同步 index）。可以在 loop 空闲时加 System 2——异步跑一个便宜的 DeepSeek Chat，扫近期事件，提取模式："这个用户最近三次任务都是性能优化，建议下次路由偏好性能模型"。产出存入 MAGMA 的 schema 层。

#### 5. HyDRA 能力路由（已部署 GitHub Copilot）

**方案**：多维度能力预测（推理/代码生成/调试/工具使用），用"短板匹配"选模型——不是选总分最高的，是选在任务最需要的维度上最强的。

**效果**：同等质量下节省 54.1% 成本。零重训练——新模型加入只需配置变更。已在 GitHub Copilot 生产环境运行。

**对奇点的应用**：router 当前是简单的层级判定（E/E+/D）+ 复杂性关键词。可以升级为多维度匹配——`model_profile.py` 已有模型画像，加维度评分（推理/代码/工具/速度/成本），任务进来后匹配最合适的模型，不一定要最贵的。

#### 理论整合优先级

| 优先级 | 理论 | 落地方式 | 预期收益 |
|--------|------|---------|---------|
| P0 | 少上下文反而更好 | executor 工具事件只保留最近 5 条+摘要 | 省 63% token + 提 20% 完成率 |
| P1 | NTILC 神经工具检索 | dispatcher 加嵌入匹配，只加载相关 skill | 省 95% skill 上下文 |
| P1 | HyDRA 能力路由 | router 升级为多维度匹配 | 省 54% 成本 |
| P2 | LLM-as-Code | orchestrator 收紧控制流 | 稳定性提升 |
| P2 | DCPM 双进程记忆 | MAGMA 加 System 2 异步模式提取 | 跨任务推理 +5% |

---

## 四、架构

### 分层

```
┌──────────────────────────────────────────────┐
│  app.py            Web 层 (Flask/SSE/限流/鉴权) │
├──────────────────────────────────────────────┤
│  _api.py           API Handler 层              │
│  __main__.py       CLI 入口                    │
├──────────────────────────────────────────────┤
│  orchestrator.py   ── 调度闭环 facade ──        │
│  router.py             任务路由                 │
│  tracker.py            任务状态机 (文件持久化)      │
│  dispatcher.py         模型调度+skill/MCP装配      │
│  _exec.py              执行引擎 (core loop)       │
│  _worktree.py          worktree 生命周期          │
│  _git_worktree.py      git 操作原语              │
│  merge.py              多 worktree 合并          │
│  workflow.py           项目工作流               │
│  _planner.py           任务分解+D层委员会          │
├──────────────────────────────────────────────┤
│  memory.py        ── 数据层 ──                  │
│    MAGMA 多图记忆 (事件/语义/锚点/时序)            │
│  _lifecycle.py         记忆衰减与清理            │
│  project.py            项目状态机               │
│  snapshot.py           写入前快照               │
│  model_registry.py     模型注册表               │
│  model_profile.py      模型画像                 │
│  api_store.py          API Key 库               │
│  roles.py              角色/人格面具             │
│  mcp.py                MCP 集成                 │
│  skills/skill_loader   Skill 系统               │
├──────────────────────────────────────────────┤
│  executors/        ── 执行器层 (隔离) ──          │
│    base.py             抽象基类                 │
│    openai_agent.py     OpenAI function calling │
│    claude_cli.py       Claude CLI (D层)         │
│    zhipu_api.py        智谱 HTTP API (E+层)      │
├──────────────────────────────────────────────┤
│  裁判/质量层                                    │
│    validator.py        校验闭环                 │
│    execution_judge.py  执行裁判                 │
│    goal_loop.py        Goal 循环               │
│    conductor.py        项目自动推进             │
│    supervisor.py       独立校验引擎             │
│    judge_monitor.py    裁判监控                 │
│    chancellor.py       报错总管                 │
│    neijinglu.py        交付完整性报告           │
├──────────────────────────────────────────────┤
│  基础设施                                      │
│    config.py           集中配置                 │
│    _io.py              统一 I/O (TOML+JSON)     │
│    _cache.py           TTL 缓存                 │
│    _auth.py            认证                    │
│    _types.py           数据类型                 │
│    _token_budget.py    Token 预算               │
│    _profiler.py        性能分析                 │
│    permission.py       权限引擎                 │
│    witness.py          心跳+观测                 │
│    log.py              文件日志                 │
│    handoff.py          Agent 交接记录            │
│    pre_search.py       I 层预检                 │
│    task_templates.py   任务模板                 │
│    codegraph.py        代码知识图谱             │
│    snapshot.py         快照                     │
│    router.py           二维路由判定             │
└──────────────────────────────────────────────┘
```

### 数据流

```
Web / CLI 入口
    │
    ▼
API Handler (_api.py)
    │ 创建任务 → tracker.create()
    ▼
orchestrator.run_queue()
    │ 取就绪任务 → tracker.ready_tasks()
    │ 路由判定 → router.assign()
    ▼
_exec.run(task, ctx, agents)
    │
    ├─ worktree 创建 (_git_worktree.py)
    │    └─ git worktree add 隔离环境
    │
    ├─ 执行循环 (最多 N turns)
    │    │
    │    ├─ dispatcher.dispatch()
    │    │    ├─ skill 加载 + MCP 工具装配
    │    │    └─ executor.run() → 模型调用
    │    │
    │    ├─ 收集 tool_events → SSE 推送
    │    │
    │    ├─ validator.validate()
    │    │    └─ Gate 过门 + 置信度评分
    │    │
    │    └─ _decide_cascade()
    │         ├─ pass → return (带 merge_request)
    │         ├─ retry → continue (复用 worktree)
    │         └─ cascade_skip → break (升级模型)
    │
    ├─ planner 分支 (D层)
    │    └─ 分解任务 → 子任务 → 并行执行
    │
    ├─ merge (v2/v3)
    │    └─ 多 worktree 产出合并
    │
    └─ finally: _cleanup_wt(wt)  ← 资源对称
```

### 关键设计决策

**1. 文件持久化而非数据库**

所有任务状态存为 `{task_id}.json` 文件，`os.replace` 原子写入。理由：零依赖、可 grep、可手动修。代价：并发下只有文件级原子性，单文件内 read-modify-write 需应用层锁（tracker._LOCK）。

**2. worktree 隔离**

每个任务在独立 git worktree 中执行，避免任务间文件冲突。类 Docker 的隔离效果，但用 git 原生能力实现，零额外依赖。

**3. 循环依赖用延迟导入破**

`orchestrator → _exec → dispatcher → orchestrator` 形成环。模块顶层互相 import 会炸，解决方式是 dispatcher 在函数体内 `from .orchestrator import ...`。这是刻意的、有文档的、不是 bug。

**4. 三模型分工**

| 模型 | 角色 | 做什么 |
|------|------|--------|
| Opus | 判断 | 审计结论、回归测试、失效点清单、难重构 |
| GLM-5.2 | 执行 | 照规格施工，全栈 |
| DeepSeek | 体力 | 加一行/改一字/删一段 |

**5. SSE 事件驱动前端**

不做轮询。后端主动推送 5 种事件（task/system/tool/turn/approval/subagent），前端按事件类型渲染对应 UI。一条管道复用。

**6. 测试约束**

scheduler 测试绝不调真模型 API。`QIDIAN_SKIP_EMBED=1` 跳过 embedding 下载。smoke 只测 CRUD 不驱动 loop。

### 文件规模

| 层 | 文件数 | 总行数 |
|----|--------|--------|
| Web 层 | 1 | ~1200 |
| API 层 | 2 | ~1800 |
| 调度核心 | 12 | ~5000 |
| 数据层 | 10 | ~3500 |
| 执行器 | 4 | ~1000 |
| 裁判/质量 | 7 | ~1800 |
| 基础设施 | 14 | ~2000 |
| 前端 | 2 | ~2200 |
| 测试 | 3 | ~720 |
| **总计** | **55** | **~17,200** |
