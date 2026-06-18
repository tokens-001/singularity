---
name: coding
description: "编程开发 skill。写代码/改bug/加功能/建项目/重构/查代码/调试/Bash脚本/Python/web开发。规则：先对齐再动手、改前读文件、报错四阶段诊断、写前定验证、超3步列计划、验证完再说完成、收工清理。工具链：claude(DeepSeek日常)、claude-ops(Opus架构)、Codex+GLM-5.2(待配)。关键词：写、改、修、代码、bug、报错、加功能、重构、新建、查、找、文件、脚本、Bash、Python、HTML、CSS。"
---
# Coding Skill

## 硬规则（通用行为准则见 CLAUDE.md，此处只列编程特有规则）

### 1. 先对齐再动手
不确定用户要什么直接问，不说"我猜"。改功能前说清楚要改什么、为什么、预期结果。用户确认再动手。

### 2. 报错四阶段诊断（systematic-debugging）
**不许跳过。** 一阶段没完成不进下一阶段。
- **Reproduce** — 复现 bug，确认触发条件
- **Diagnose** — 找到根因，不是看症状。给具体文件和行号
- **Fix** — 最小修改，不动无关代码
- **Verify** — 确认修好了，确认没引入新问题

### 3. 写功能前先定验证方式（test-driven-development）
怎么写不重要，怎么判断做对了才重要。说不清验证方式 = 没想清 = 不动手。

### 4. 超 3 步先列计划（writing-plans + executing-plans）
每步 2-5 分钟能完成。列出改哪些文件、为什么改、改完怎么验证。用户确认 → 逐项打勾 → 不跳步。

### 5. 证据在前、断言在后（verification-before-completion iron law）
说"做完了"之前先跑验证命令，贴输出。没跑过 = 没做完。

### 6. 收工清理（finishing-branch）
删调试 print、临时文件、注释掉的旧代码。说清楚改了什么、在哪、为什么。

### 7. 独立任务可并行（dispatching-agents）
两个互不依赖的修改可以同时做。互相依赖的必须按顺序。

### 8. 技术判断不是表演（receiving-code-review）
用户说"这样对吗"或给出不同意见时，验证后再回应。不确定就说"我验一下"，不硬撑。

## 工作流

```
对齐意图 → 超3步列计划 → 动手(读→写→自检) → 验证(跑通+贴输出) → 收工清理
```

## 别名速查（不是路由——路由由 alias 承担，无路由代码）

| 场景 | 后端 | 命令 |
|------|------|------|
| 日常开发 | DeepSeek V4 Pro | `claude` |
| 架构/方向决策 | Anthropic Opus | `claude-ops` |
| 复杂代码生成 | GLM-5.2 via ZCode | 待配 |

> **注意**：`claude` alias 指向的二进制被 `.zsh_secrets` 劫持到 DeepSeek。Claude Code 自己 spawn 的子代理/Task 都会静默走 DeepSeek。别让劫持悄悄改变了执行人格的底座——需要 Opus 级判断时开 `claude-ops`。

## 知识库搜索

```bash
cd python/qidian-knowledge && python3 scripts/search.py "<查询>" [--domain principle|decision|insight|case]
```

**改引擎后必须过闸**（改 core.py / tokenizer.py / 域路由词表 / ingest 后）：

```bash
cd python/qidian-knowledge && python3 scripts/eval.py --gate || echo "❌ 退化，拒绝变更"
```

闸借鉴 SkillOpt Gate 模式：对比基线 Recall@3=88.24%（30条），只接受不退化。
