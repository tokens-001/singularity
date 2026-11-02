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

## 项目状态与已知限制

**这是个人项目，一个人维护，不承诺支持。** 下面这些是真实状况，写在这里是为了不让它被读成「已经能全自动交付」：

- **端到端验收没过**：项目定义过 8 条验收判据（`docs/验收-端到端-20260921.md` §三），截至 2026-11-02 **累计多轮全部未达成**；最近一轮（2026-10-01）也没有达成。
- **成本口径未核**：账本按「tokens/1e6 × 混合单价」估算，不区分输入/输出/缓存，与厂商实际计费口径有已知偏差 —— 作者手上有过约 2 倍量级的估计，但**没拿真实账单核过**。**跑起来会真的花钱**：作者自己一轮小验证约几毛美元，一个真实项目一轮在 5 元人民币上下。
- **需要自备模型 API key**：出厂不带模型，所有调用由使用者自己付费。
- **很多路径只在真机撞过**：`docs/防御模式.md`（3,700 余行，症状 → 根因 → 规则）记录了大量失败的形态，说明稳定性离「随便跑」还有距离。
- **数字会过期**：上面所有测试数、行数都标了采集日期，别当现状读。

**目前能稳定演示的**：单文件小任务走完流水线、Observer 对话、三个 GATE 人工确认门。
**不能承诺的**：一个中等规模项目从需求到可交付产物全自动跑通。

## 许可证

[Apache License 2.0](LICENSE)，Copyright 2026 tokens-001。

允许商用、修改、再分发，保留版权与许可证声明即可。软件按「原样」提供，不附带任何担保 —— 见许可证第 7、8 条。
