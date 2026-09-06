# 奇点 Singularity

AI-native 软件开发流水线。用自然语言描述需求，自动走完六阶段：定义→架构→实现→集成→审查→验收→交付。

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
     → 六阶段流水线
        定义(GATE1) → 架构(GATE2) → 实现 → 集成合并 → 审查(GATE3) → 验收 → 交付
     → 16 个角色专家团队
     → 模型能力推荐制（rating 评级 + recommended_for 推荐）
```

详见 `docs/专家团队架构.md`。

## 项目结构

```
src/singularity/
├── scheduler/       # 核心调度引擎（76 文件，~19K 行）
│   ├── orchestrator.py   # 六阶段流水线主控
│   ├── workflow.py       # 阶段执行器
│   ├── dispatcher.py     # Agent 调度 + 模型选择
│   ├── _review.py        # 审查循环（D1-D3 防线）
│   ├── project.py        # 项目状态机
│   ├── observer_agent.py # Observer 对话代理
│   └── ...
├── web/             # Flask + React SPA
│   ├── app.py              # Flask API（~1600 行）
│   └── frontend/src/       # React（10 文件，~800 行）
├── skills/          # 6 个方法型技能
└── tests/           # 36 个测试文件
```

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
| `docs/专家团队架构.md` | 完整架构设计（36KB） |
| `docs/frontend-spec.md` | 前端规范 |
| `docs/ARCHITECTURE.md` | 审计 + 架构文档 |
| `docs/现状速写.md` | 当前现状速写（权威） |

## 技术栈

**后端：** Python 3.14, Flask, httpx, ChromaDB
**前端：** React 19, react-router-dom 7, zustand 5, Vite 6
**模型：** DeepSeek / 智谱 GLM / Kimi / 通义千问 / OpenAI / Anthropic

## 当前状态

- 276 测试全绿（`pytest tests/test_scheduler/ -q`）
- 模型能力快照 + 推荐制（rating 评级 SSS+~A + recommended_for 推荐用途），已替代旧 E/E+/D 三层与 cheap/strong 两档
- 出厂零模型：用户自选厂家、输 key、扫描导入
- 端到端已验证（hello.py 任务 5s 交付、3 模型委员会碰撞、Observer 对话→执行）
- 自部署：Dockerfile + docker-compose
