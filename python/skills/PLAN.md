# Agent Skill 系统 v2 — 借鉴 Scream Code 后修订

## 借鉴了什么

| 借鉴点 | Scream Code 做法 | 奇点怎么用 |
|--------|-----------------|-----------|
| Skill 载体 | Markdown + YAML frontmatter | 直接抄，比 TOML 可读性好 |
| 多来源 | project/user/builtin/extra 四层 | .qidian/skills/(用户) + python/skills/(系统内置) |
| 参数展开 | $ARGUMENTS, $0, $1 | openai-agent function calling 参数映射 |
| 命名冲突 | plugin:name 前缀 | 后加载覆盖先加载 |
| Marketplace | JSON 索引 + 下载 | 暂时不做，先用内置+用户自写 |

## 不改的

- claude-cli (D层) 有自己的 skill 系统，不碰
- 现有 4 个内置工具不动，skill 是追加
- 路由/调度逻辑不动

## Skill 文件格式

```markdown
---
name: code-review
description: 审查代码的 bug、性能和风格问题
type: tool
arguments: file_path focus
---

# Code Review

审查文件 ${file_path}，关注点：$focus。

## 执行方式

```bash
python3 -m skills.code_review $file_path $focus
```
```

- `type: tool` → 注册为 function calling 工具，模型可调用
- `type: prompt` → 拼入 system prompt，增强模型能力
- `type: flow` → 多步骤工作流，暂不支持

## 存放位置

```
python/skills/          ← 系统内置 (Git 管理)
  code-review/
    SKILL.md

.qidian/skills/         ← 用户自定义 (Web 端管理)
  my-skill/
    SKILL.md
```

## 工作流

```
Web 端: 给 Kimi(E层) 勾选 code-review
  │
  ▼
agents_custom.json: _skills: {"E": {"kimi-k2.7-code": ["code-review"]}}
  │
  ▼
openai-agent 启动时:
  1. 扫描 skills 目录 → 解析 SKILL.md
  2. type=tool 的 → 注册为 function calling 定义
  3. type=prompt 的 → 拼入 system prompt
  │
  ▼
模型调用 review_code(file_path="app.py", focus="bug")
  │
  ▼
executor 执行 → 结果返回模型
```

## 分步

### 1. Skill 引擎 (skill_loader.py)
- 扫描两个目录，解析 YAML frontmatter
- 返回 `{name: SkillDef}` 
- 校验：name 必填、type 合法、command 不危险

### 2. API
- `GET /api/skills` — 所有可用 skill
- `POST /api/skills` — 创建/上传
- `DELETE /api/skills/<name>` — 删除
- `GET/PUT /api/agents/<level>/<model>/skills` — agent 的 skill 绑定

### 3. openai-agent 集成
- 启动时加载 skill → type=tool 生成 function calling 定义
- type=prompt 拼到 system prompt
- _execute_tool 里新增 skill 分支

### 4. Web 面板
- Config → Skills 子面板
- 左侧 skill 列表 (增删)
- 右侧 agent×skill 勾选矩阵

## 暂不做
- type=flow 多步骤工作流
- skill 市场和下载
- skill 间依赖
- 热加载（改完需刷新）
