# 奇点 Singularity

AI-native 软件开发流水线。用自然语言描述需求，自动走完：调研 → 架构 → 实现 → 集成 → 审查 → 交付（三个 GATE 人工确认门）。

## 快速开始

```bash
# 安装
pip install -e .

# 启动服务
python3 -m singularity.web.app
# → http://127.0.0.1:5050

# 运行测试
python3 -m pytest tests/test_scheduler/ -q
```

## 配置

1. 打开 http://127.0.0.1:5050/config
2. 在「API 连接」添加 API key（DeepSeek/智谱/Kimi/通义千问/OpenAI/Anthropic）
3. 在「智能体」启用模型
4. 回到「对话」，说「帮我做一个xxx」开始

环境变量（可选，API 连接页面手动填更方便）：
```bash
export DEEPSEEK_API_KEY=sk-xxx
export ZHIPU_API_KEY=xxx
export KIMI_API_KEY=sk-xxx
export DASHSCOPE_API_KEY=sk-xxx
```

## 架构

```
用户 → Observer 对话
     → 流水线
        调研(GATE1) → 架构(GATE2) → 实现 → 集成合并 → 审查(GATE3) → 交付
     → 10 个角色
     → 模型能力推荐制（rating 评级 + recommended_for 推荐）
     → 架构阶段多模型委员会（任务文本命中强短语时触发）
```

> ⚠️ 这段**曾经写"六阶段 / 16 角色专家团队"**，是 2026-06 的旧描述（2026-09-11 更正）。
> 现状权威说明见 `docs/生产流现状.md`；逐节带源码行号的详解见 `docs/工作流详解-外派评审.md`。

## 项目结构

```
src/singularity/
├── scheduler/       # 核心调度引擎（72 文件，~20K 行）
│   ├── orchestrator.py   # 调度循环 + 阶段自动流转
│   ├── workflow.py       # 人手触发的阶段推进
│   ├── dispatcher.py     # Agent 调度 + 模型选择
│   ├── _review.py        # 审查层
│   ├── project.py        # 项目状态机（set_phase 单一入口）
│   ├── observer_agent.py # Observer 对话代理
│   └── ...
├── web/             # Flask + React SPA
│   ├── app.py              # Flask API（2115 行）
│   └── frontend/src/       # React（35 文件，3836 行）
├── skills/          # 5 个方法型技能
└── tests/           # 90 个测试文件
```

> 上面这些数字是 **2026-09-11 实测**，会漂。要最新的：
> `find src/singularity -name '*.py' -not -path '*/node_modules/*' | wc -l` 等（见 `docs/现状速写.md` 文末）。

## 常用命令

```bash
# 后端
pip install -e ".[dev]"
python3 -m pytest tests/test_scheduler/ -q           # 测试
ruff check src/singularity/                           # 代码检查
mypy src/singularity/                                 # 类型检查

# 前端
cd src/singularity/web/frontend
npm install
npm run build     # 构建到 static/dist/

# Git
git status
# 改代码前先 grep 确认影响范围
```

## 文档

| 文档 | 内容 |
|------|------|
| `docs/生产流现状.md` | **生产流现状（权威）** —— 对着源码核过 |
| `docs/工作流详解-外派评审.md` | 工作流详解，逐节带源码行号 |
| `docs/防御模式.md` | 踩过的坑：症状 → 根因 → 规则 |
| `docs/frontend-spec.md` | 前端规范 |
| `docs/现状速写.md` | 现状速写（2026-09，部分数字已旧） |

## 技术栈

**后端：** Python 3.14, Flask, httpx, ChromaDB
**前端：** React 19, react-router-dom 7, zustand 5, Vite 6
**模型：** DeepSeek / 智谱 GLM / Kimi / 通义千问 / OpenAI / Anthropic

## 当前状态

- **814 测试全绿**（`pytest tests/test_scheduler/ -q`；全量 `pytest tests/` 是 823）
  > 原写 276（2026-09-11 更正）。数字会漂，自己测：`.venv/bin/python -m pytest tests/ --collect-only -q | tail -1`
- 模型能力快照 + 推荐制（rating 评级 SSS+~A + recommended_for 推荐用途），已替代旧 E/E+/D 三层与 cheap/strong 两档
- 出厂零模型：用户自选厂家、输 key、扫描导入
- 端到端已验证（hello.py 任务 5s 交付、3 模型委员会碰撞、Observer 对话→执行）
- 自部署：Dockerfile + docker-compose
