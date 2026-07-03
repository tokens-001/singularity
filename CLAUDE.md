# Singularity — AI Agent 调度平台

多模型协作开发平台。用户提需求 → 6阶段流水线自动拆解/分配/执行/审查/交付。v4.2，276测试绿，封存中。

> 重启先读：`docs/项目速查.md`（架构/数据流/API/前端/配置全在里面）

## 硬约束

- **改后验证**：贴运行输出，不准"应该能跑"
- **外科手术**：只改目标代码，diff 只含必要变更
- **声称前核实**：说"已修复"→ grep 确认；说"N 行"→ wc -l
- **测试绿≠对**：手工测一条边缘路径
- **并行避冲突**：subagent 修改文件不交叠，改前 git status
- **危改前存档**：跨 3+ 文件或安全/架构改动前自动 `/checkpoint`
- **先读蓝图**：改奇点架构/流程前必须先读 `docs/专家团队架构.md` + `docs/ARCHITECTURE.md`
- **对比标注**：做技术对比时，推测和确认要分开标注，不确定就说"不确定"
- **禁止编造**：没读到源码/文档的实现细节不准编，直接说"没看到代码，不确定"

## 项目

- 代码：`src/singularity/`（scheduler / web / skills）
- 安装：`pip install -e .`
- 测试：`pytest tests/test_scheduler/ -q` + `python3 tests/test_exec_run.py`
- 服务：`python3 -m singularity.web.app` → 127.0.0.1:5050
- 记忆：`~/.claude/projects/-Users-jingzhe/memory/MEMORY.md`

## 恢复

```bash
cd ~/奇点
python3 -m singularity.web.app          # 本地
docker compose up -d --build            # Docker
```

## 指针

- 重启速查 → `docs/项目速查.md`
- 架构 → `docs/ARCHITECTURE.md` + `docs/专家团队架构.md`
- 演化史 → `docs/演化史.md`
- 个人知识 → `~/知识/`
