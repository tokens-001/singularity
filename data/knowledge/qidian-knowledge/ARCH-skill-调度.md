# Skill 模式 vs Workflow 模式 —— 调度机制拆解

> 拆解对象：`.claude/skills/ui-ux-pro-max/SKILL.md`
> 目的：搞清「被动触发」和「显式调用」如何在同一个 skill 里分离，再映射到多 Agent 平台
> 写于 2026-06-17 · 只分析，不写代码

---

## 0. 一句话先立靶

一个 SKILL.md 文件里其实压着**两套独立的调度逻辑**：

- **frontmatter 的 `description`** —— 决定「**什么时候**这个 skill 被加载进上下文」（被动、模糊匹配、召回优化）
- **body 里的 CLI 步骤** —— 决定「加载之后**执行什么**」（主动、确定性、精度优化）

触发和执行是**解耦**的。description 不知道 CLI 长什么样，CLI 也不知道自己是被哪个关键词触发的。理解这一点，整个调度机制就透了。

---

## 1. description 字段怎么做到自动触发

看 ui-ux-pro-max 的 description（节选）：

```
UI/UX design intelligence. 67 styles, 96 palettes, 57 font pairings...
Actions: plan, build, create, design, implement, review, fix, improve, optimize, enhance, refactor, check UI/UX code.
Projects: website, landing page, dashboard, admin panel, e-commerce, SaaS, portfolio, blog, mobile app, .html, .tsx, .vue, .svelte.
Elements: button, modal, navbar, sidebar, card, table, form, chart.
Styles: glassmorphism, claymorphism, minimalism, brutalism...
Topics: color palette, accessibility, animation, layout, typography...
Integrations: shadcn/ui MCP...
```

### 1.1 机制：description 是「触发面」，不是「说明文」

Claude Code 启动时把**所有** skill 的 description 读进上下文。用户发请求时，模型拿请求去和每个 description 做语义/关键词重叠匹配，重叠够强的 skill 被激活。

所以 description 的工程目标不是「讲清楚这个 skill 干嘛」，而是**最大化和任意一句 UI/UX 请求的关键词交集**。这是个召回（recall）优化问题。

### 1.2 三个具体手法

**① 分面枚举（faceted enumeration）**
description 不是一段话，是按**用户可能从哪个角度发起请求**切成的多个枚举面：
- `Actions:` —— 用户用什么**动词**（plan/build/fix/review…）
- `Projects:` —— 用户做什么**产物**（landing page/dashboard/SaaS…）
- `Elements:` —— 用户提到什么**组件**（button/modal/navbar…）
- `Styles: / Topics:` —— 用户提到什么**风格/话题**
- 文件后缀（`.tsx .vue .svelte`）—— 在用户根本没说"设计"、只是在改某个文件时也能触发

一个请求无论从动词、产物、组件还是文件类型哪个角度进来，都能命中某一面。这是**用枚举换召回**。

**② 关键词密度拉满**
description 里塞了几十个领域词。每多一个高频词，就多一条触发路径。代价是 token，但 description 是一次性加载、收益是触发覆盖，划算。

**③ 领域身份锚（开头那句数字）**
`67 styles, 96 palettes...` 不只是炫耀数据量——它给模型一个强信号：**这个 skill 在 UI/UX 域是权威**。当多个 skill 都沾边时，这种"领域纵深"声明帮模型判断谁更该被选。

### 1.3 AI 在多个 skill 之间怎么选

本质是 **max-overlap 仲裁**：请求 vs 每个 description 的关键词/语义重叠，重叠最强者胜。

- description 越**密**，召回越高，但**精度**会掉（容易过度触发）
- 多个 skill 抢同一片关键词领地 → 冲突 → 靠外部仲裁（你的 `skill-registry.md` 就是干这个的）
- 所以好的 description 体系要求各 skill 的关键词领地**尽量互斥**——精度来自描述之间不打架，不是来自单个描述写得多好

**核心张力：description 是召回-精度的旋钮。** 枚举越多越容易被触发（高召回低精度），领地越互斥越不会误触发（高精度）。

---

## 2. Workflow 模式怎么被显式调用——CLI 管线如何跟 Skill 触发解耦

SKILL.md body 里是一条确定性流水线：

```
Step 1: 分析需求（产品类型/风格/行业/栈）
Step 2: python3 scripts/search.py "<query>" --design-system   ← REQUIRED
Step 2b: ... --persist [--page "dashboard"]
Step 3: python3 scripts/search.py "<kw>" --domain <domain>
Step 4: python3 scripts/search.py "<kw>" --stack html-tailwind
```

### 2.1 解耦点：CLI 是独立程序，对 skill 一无所知

`search.py` 是个普通 Python CLI。它**不读 frontmatter、不知道自己被哪个关键词触发、也不在乎调用者是不是 Claude**。你可以在任何终端直接：

```
python3 search.py "fintech crypto" --design-system
```

skill 从头到尾没参与。**触发层（description）和执行层（CLI）物理隔离在两个地方：description 在 frontmatter，逻辑在 scripts/。**

### 2.2 这带来的三个性质

- **可寻址**：workflow 有明确入口（CLI + flag + 参数），可以被任何东西按名调用——人、脚本、另一个 Agent、cron——不依赖 skill 被触发
- **确定性**：同样的 `--design-system "X"` 永远走同一条多域检索→套规则→合成的路径。flag 就是显式的 mode switch（`--design-system` / `--domain` / `--stack` / `--persist` 各是一条岔路）
- **可测**：CLI 能脱离 skill 单独跑评测（你的 `eval.py` 就是这么测 core.py 的，根本不经过触发层）

### 2.3 body 的真实角色：触发→执行的「翻译器」

body 不是文档，是**把一次模糊触发翻译成一串确定 CLI 调用的脚本说明**。它规定了：哪步 REQUIRED、参数怎么填、flag 怎么选、输出怎么往下一步喂。模型读 body = 拿到一份"现在按这个顺序敲这些命令"的指令。

---

## 3. 两种模式怎么交互——Skill 自动激活后怎么切入 Workflow

完整链路：

```
用户请求
  │
  ▼  ① 触发层（被动）：请求 ⨯ 所有 description → max-overlap → ui-ux-pro-max 激活
  │     模型此时只是把整个 SKILL.md body 读进了上下文
  ▼  ② 桥接层：body 的 Step 1「分析需求」把模糊请求结构化（产品/风格/行业/栈）
  │     —— 这一步是从"模糊触发"转向"确定参数"的关键拐点
  ▼  ③ 执行层（主动）：body 指示模型显式调用 search.py --design-system "结构化query"
  │     CLI 跑确定性管线，吐结构化结果
  ▼  ④ 反馈循环：模型读结果 → 按 body 决定要不要补 Step 3/4（--domain/--stack）
  │
  ▼  结果合成 → 交付
```

关键：**激活是模糊的、一次性的；激活之后立刻收敛成确定流程。** body 的 Step 1 是漏斗颈——把"用户大概想要个落地页"翻译成 `"beauty spa wellness elegant"` 这种能喂给 CLI 的确定查询。

换句话说：
- description 负责**抓住**请求（宁可多触发）
- body Step 1 负责**收窄**成参数（把模糊变明确）
- CLI 负责**确定地执行**（同输入同输出）

模糊→明确→确定，一层层收紧。这正好和 CLAUDE.md 的「模糊是敌人，先把模糊变明确」是同一个结构。

---

## 4. 映射到多 Agent 平台：谁该 Skill、谁该 Workflow

判据一句话：

> **触发条件模糊、跨场景、错过了也不致命 → Skill 模式（被动守门）。**
> **在管线里有固定位置、必须执行、不能漏 → Workflow 模式（显式调用）。**

### 4.1 该用 Skill 模式（被动守门）的

这些东西的共性：**你事先不知道用户哪句话会需要它**，靠 description 在环境里"蹲守"，命中条件才现身。

| 组件 | 触发条件（模糊、context-driven） | 为什么是 Skill |
|------|-------------------------------|--------------|
| `teach-me` | 用户想学编程/知识 | 触发面宽，靠关键词蹲守 |
| `coding` | 用户在写/改代码 | 同上，按动词+文件类型触发 |
| `scope-guardian` | 用户提新功能 | 典型守门——蹲守"想加功能"这个信号 |
| `cognitive-quick-ref` | 用户表现出特定认知状态 | 蹲守状态信号，模糊判断 |

这些**错过一次不致命**（下次还能触发），所以用召回优化的被动触发合适。

### 4.2 该用 Workflow 模式（显式调用）的

共性：**在一条已知管线里有确定位置，漏掉就是结构性漏洞**。

| 组件 | 在管线里的位置（确定） | 为什么是 Workflow |
|------|----------------------|-----------------|
| **I 层校验**（qidian-knowledge） | D→**I**→E 中间，每次必过 | 校验层一旦"有时不触发"就等于没校验。必须 CLI 显式调用，不能靠关键词蹲守 |
| `ingest.py` 重建索引 | 语料改动后 / 归档后 | 数据管线步骤，按位置调用 |
| `eval.py` 评测 | 改 tokenizer/core 之后 | 确定性测试步骤 |
| D→I→E 主链本身 | 整条编排 | 编排顺序就是 workflow 的定义 |

### 4.3 最关键的一条：I 层绝不能做成 Skill

这是整个映射里最该钉死的判断。

`scope-guardian` 可以是 Skill——它守的是"用户提新功能"这个**模糊信号**，偶尔漏一次，下次补上，无所谓。

但 **I 层（校验）守的是 D→E 之间的硬关口，必须每次都过**。如果把它做成 description 触发的被动 skill，就会出现"这次请求没命中触发词、校验层没激活、错误方案直接进了 E 层执行"——这正是 I010 批的「伪校验」的系统级版本：**一个有时不在岗的校验，比没有校验更危险，因为你以为它在**。

所以：
- **守门的模糊信号** → Skill（召回优先，漏了不致命）
- **管线的硬关口** → Workflow（确定调用，绝不能漏）

I 层属于后者。它的入口必须是 E 层执行前**无条件**调用的一步，而不是一个挂在 description 上、看天吃饭的触发。

---

## 5. 一页纸结论

| 维度 | Skill 模式 | Workflow 模式 |
|------|-----------|--------------|
| 触发 | 被动（description 关键词蹲守） | 主动（CLI/按名显式调用） |
| 优化目标 | 召回（宁可多触发） | 精度+确定性（同入同出） |
| 入口 | frontmatter description | scripts/ 里的可寻址 CLI |
| 漏触发的后果 | 不致命（下次补） | 致命（管线破洞） |
| 适用 | 跨场景守门、辅助、提醒 | 硬关口、数据管线、编排步骤 |
| 平台对应 | teach-me / coding / scope-guardian / cognitive-quick-ref | **I 层校验** / ingest / eval / D→I→E 主链 |

description 抓得宽、body 收得窄、CLI 跑得准——三段式收紧。把这套搬到平台上，就是：**模糊的事交给 Skill 蹲守，致命的事交给 Workflow 钉死。**
