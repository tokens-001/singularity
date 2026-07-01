# 奇点前端规范

## 技术栈

| 层 | 选型 | 版本 |
|---|------|------|
| 框架 | React | ^19.0 |
| 路由 | react-router-dom | ^7.0 |
| 状态 | zustand + persist | ^5.0 |
| 图标 | lucide-react | ^0.400 |
| 构建 | Vite | ^6.0 |
| 语言 | TypeScript | ^5.6 |

无 UI 组件库，全部 CSS inline style。无 CSS-in-JS 库。

## 文件结构

```
frontend/src/
├── main.tsx            # 入口，挂载 BrowserRouter
├── App.tsx             # 路由表：/ /tasks /projects /config
├── index.css           # CSS 变量 + 全局 reset
├── components/
│   └── AppLayout.tsx   # 侧边栏 + 主内容区（Outlet）
├── pages/
│   ├── Chat.tsx        # 首页：Observer 对话 + SSE 实时状态
│   ├── Tasks.tsx       # 任务：列表 + 状态筛选 + 行内操作
│   ├── Projects.tsx    # 项目：列表 + 阶段管线 + GATE 审批
│   └── Config.tsx      # 配置：模型/智能体/技能三 Tab
├── lib/
│   ├── api.ts          # 全部后端 API 封装
│   └── useSSE.ts       # EventSource 实时事件 hook
└── stores/
    └── app.ts          # 全局状态（侧边栏折叠）
```

**文件数：** 10 个源文件，~800 行 TSX/TS

## 路由

| 路径 | 页面 | 说明 |
|------|------|------|
| `/` | Chat | Observer 对话，首页 |
| `/tasks` | Tasks | 任务列表 + 管理 |
| `/projects` | Projects | 项目列表 + GATE 审批 |
| `/config` | Config | 模型/智能体/技能配置 |

SPA 模式，后端 Flask 有 `/<path:path>` 全部 fallback 到 `index.html`。

## CSS 规范

### 全局变量（`index.css`）

```css
:root {
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #21262d;
  --border: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #8b949e;
  --text-muted: #6e7681;
  --accent: #58a6ff;
  --accent-green: #3fb950;
  --accent-red: #f85149;
  --accent-yellow: #d2991d;
  --accent-purple: #a371f7;
  --radius: 6px;
  --font-mono: 'SF Mono', 'Fira Code', monospace;
}
```

暗色主题，GitHub 风格配色。

### 样式原则

1. **全部 inline style** — 不用 CSS Module / styled-components / Tailwind。直接 `style={{...}}`。
2. **变量优先** — 颜色/圆角用 `var(--xxx)`，不硬编码
3. **抽取模式变量** — 重复的 style 对象提为组件外常量或简短变量名（如 `sInp`、`smBtn`、`greenBtn`）
4. **不设断点** — 无响应式设计，面向桌面端（>=1024px）
5. **无动画库** — 仅 CSS `transition: 0.15s` 做侧边栏折叠

## 组件规范

### 页面组件

每个页面文件 export default 一个函数组件。职责边界：

- **Chat** — 对话 UI + 系统状态栏 + 事件流
- **Tasks** — 任务 CRUD + 状态筛选 + 行内操作按钮
- **Projects** — 项目列表 + GATE 审批按钮（仅 gate 阶段显示）
- **Config** — 三 Tab 纯展示 + 操作表单

### 状态管理

zustand 仅存全局 UI 状态（侧边栏折叠）。业务数据由各页面组件内部 `useState` + `useEffect` 自行管理，不做全局数据层。

### SSE 实时推送

```typescript
// lib/useSSE.ts
useSSE(callback)  // callback 接收 {kind, msg, ts, ...}
```

Chat 和 Tasks 页面都用 `useSSE` 监听后端事件自动刷新。

## API 封装（`lib/api.ts`）

### 规范

- 统一 `request<T>(url, opts)` 基函数，自动 `Content-Type: application/json`
- 每个端点一个方法，返回类型通过泛型约束
- 错误处理在调用处（try/catch），不在 api 层

### 端点清单

| 方法 | HTTP | 路径 | 说明 |
|------|------|------|------|
| status | GET | /api/status | 系统状态 |
| tasks | GET | /api/tasks?status= | 任务列表 |
| task | GET | /api/tasks/:id | 任务详情 |
| createTask | POST | /api/tasks | 创建任务 |
| cancelTask | POST | /api/tasks/:id/cancel | 取消 |
| retryTask | POST | /api/tasks/:id/retry | 重试 |
| holdTask | POST | /api/tasks/:id/hold | 暂停 |
| releaseTask | POST | /api/tasks/:id/release | 释放 |
| dailyAuto | POST | /api/daily/auto | 日报自动模式 |
| projects | GET | /api/projects | 项目列表 |
| project | GET | /api/projects/:id | 项目详情 |
| createProject | POST | /api/projects | 创建项目 |
| runPhase | POST | /api/projects/:id/run-phase | 手动推进阶段 |
| gateConfirm | POST | /api/projects/:id/gate-confirm | GATE 批准/驳回 |
| observerChat | POST | /api/observer/chat | Observer 对话 |
| agents | GET | /api/agents | 智能体列表 |
| deleteAgent | DELETE | /api/agents/any/:model | 禁用智能体 |
| addAgent | POST | /api/agents | 启用/添加 |
| models | GET | /api/models | 模型目录 |
| modelsImport | POST | /api/models/import | 批量导入 |
| apiStore | GET | /api/api-store | API 连接列表 |
| addApiStore | POST | /api/api-store | 添加 API |
| scanApiStore | POST | /api/api-store/:id/scan | 扫描模型 |
| skills | GET | /api/skills | 技能列表 |
| addSkill | POST | /api/skills | 创建技能 |
| agentSkills | GET | /api/agents/any/:model/skills | 智能体技能 |
| updateAgentSkills | PUT | /api/agents/any/:model/skills | 更新技能绑定 |
| tokenUsage | GET | /api/token-usage | Token 用量 |
| startLoop | POST | /api/loop/start | 启动调度循环 |
| stopLoop | POST | /api/loop/stop | 停止调度循环 |

## 构建 & 部署

```bash
npm run build   # tsc + vite build → static/dist/
```

产物 3 文件：`index.html` + `assets/index-XXX.js`（~277KB）+ `assets/index-XXX.css`（~1KB）。Gzip 后约 85KB。

Flask 直接 serve `static/dist/index.html`，无 Nginx/反向代理。

## 新增页面 CheckList

1. 在 `pages/` 新建 `XxxPage.tsx`
2. 在 `App.tsx` 加 `<Route path="/xxx" element={<XxxPage />} />`
3. 在 `AppLayout.tsx` 的 `NAV` 数组加 `{ to: '/xxx', icon: XxxIcon, label: '标签' }`
4. 需要 API 的在 `lib/api.ts` 加方法
5. 执行 `npm run build` 更新 dist

## 已知限制

- 无分页/虚拟滚动，任务列表大量数据时会卡
- 无表单校验库，手动检查
- 无错误边界（ErrorBoundary），API 失败静默处理
- 无国际化，全硬编码中文
- 无 E2E/组件测试
- 无响应式，移动端不可用
