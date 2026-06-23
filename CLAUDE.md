# Singularity

## 硬约束

- **改后验证**：贴运行输出，不准"应该能跑"
- **外科手术**：只改目标代码，diff 只含必要变更
- **声称前核实**：说"已修复"→ grep 确认；说"N 行"→ wc -l
- **测试绿≠对**：手工测一条边缘路径
- **并行避冲突**：subagent 修改文件不交叠，改前 git status
- **危改前存档**：跨 3+ 文件或安全/架构改动前自动 `/checkpoint`

## 项目

- 代码：`src/singularity/`（scheduler / web / skills）
- 安装：`pip install -e .`
- 测试：`pytest tests/test_scheduler/ -q`（75）+ `python3 tests/test_exec_run.py`（21）
- 服务：`python3 -m singularity.web.app` → 127.0.0.1:5050
- 记忆：`~/.claude/projects/-Users-jingzhe/memory/MEMORY.md`
- 架构：`docs/ARCHITECTURE.md`

## 指针

- 日常执行 → `docs/日常执行.md`
- 镜子模式 → `docs/context/mirror-mode.md`
- 知识库 → `docs/knowledge/体系边疆.md`
- Python教学 → 第一步 Skill("teach-me")

## Agent skills

### Issue tracker

Local markdown under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CLAUDE.md` + `docs/adr/`. See `docs/agents/domain.md`.
