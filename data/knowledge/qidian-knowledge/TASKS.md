# P1-P4 任务清单 (Architect分解)

## P1: 项目状态机

| ID | 任务 | 描述 | 复杂度 | 依赖 |
|----|------|------|--------|------|
| T1.1 | `scheduler/project.py` — ProjectState CRUD | dataclass + save/load + phase枚举 + owner_confirm字段 | low(E) | - |
| T1.2 | Gate确认API | `POST /api/projects/<id>/gate-confirm` — Owner批gate | low(E) | T1.1 |
| T1.3 | Phase状态机 | 前进/回退/拒绝+架构返工回planning | medium(E+) | T1.1 |
| T1.4 | 重启恢复 | 启动时load ProjectState → 从中断phase继续 | low(E) | T1.1 |
| T1.5 | 项目API | GET/POST /api/projects + template list | low(E) | T1.1 |

## P2: 角色工作流

| ID | 任务 | 描述 | 复杂度 | 依赖 |
|----|------|------|--------|------|
| T2.1 | Architect增强 | planner preamble注入调研报告+结构化输出schema | medium(E+) | T1.1 |
| T2.2 | 任务分解输出 | JSON Schema约束: architecture+tasks+constraints | low(E) | T2.1 |
| T2.3 | Implementer重试循环 | feedback→修改→重跑,Supervisor判定修好 | medium(E+) | T2.2 |
| T2.4 | Supervisor校验引擎 | 4维机械checklist+硬/软证据分流 | D | T2.2 |
| T2.5 | Researcher可选触发 | 任务含"调研/参考/借鉴"关键词→先research | low(E) | T2.1 |
| T2.6 | E→E+动态升级 | Implementer撞墙→升Builder(复用现有escalate) | low(E) | T2.3 |

## P3: 保障层

| ID | 任务 | 描述 | 复杂度 | 依赖 |
|----|------|------|--------|------|
| T3.1 | 成本追踪(按$) | per-task token→$换算+项目累计+超限暂停 | medium(E+) | T1.1 |
| T3.2 | 血缘日志 | lineage log追加,parent链式追溯 | low(E) | T1.1 |
| T3.3 | 阶段快照 | Gate前自动snapshot,Gate3拒绝可回滚 | medium(E+) | T1.3 |
| T3.4 | Owner降级自治 | Owner不在时:其他任务继续,冲突park,不全局阻塞 | low(E) | T1.3 |
| T3.5 | Agent注册表API | 现有roles.py→CRUD API+切换/Committee | low(E) | - |
| T3.6 | 模型隔离硬锁 | Supervisor≠Implementer,启动时校验 | low(E) | T2.4 |

## P4: 前端工作流UI

| ID | 任务 | 描述 | 复杂度 | 依赖 |
|----|------|------|--------|------|
| T4.1 | 单项目视图 | 替换4Tab→流程步骤可视化+当前阶段高亮 | medium(E+) | T1.1 |
| T4.2 | Gate确认面板 | 每Gate输入/输出+批准/拒绝按钮 | low(E) | T1.2 |
| T4.3 | 黑板只读视图 | 项目状态+artifact+lineage展示 | low(E) | T1.1 |
| T4.4 | Agent管理面板 | 注册表CRUD+切换+Committee配置 | low(E) | T3.5 |

## 分配汇总

| 层级 | 任务 | 数量 |
|------|------|------|
| E | T1.1,T1.2,T1.4,T1.5,T2.2,T2.5,T2.6,T3.2,T3.4,T3.5,T3.6,T4.2,T4.3,T4.4 | 14 |
| E+ | T1.3,T2.1,T2.3,T3.1,T3.3,T4.1 | 6 |
| D | T2.4(Supervisor校验引擎——最核心) | 1 |

**总计: 21 tasks。先E后E+,D收尾。**
