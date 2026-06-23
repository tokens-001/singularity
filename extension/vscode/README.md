# Singularity Dispatch面板 — VS Code 扩展

## 安装

```bash
cd extension/vscode
npm install
npm run compile
```

然后在 VS Code 中按 F5 启动调试。

## 配置

在 VS Code 设置中添加:

```json
{
  "qidian.baseUrl": "http://127.0.0.1:5050",
  "qidian.token": "your-admin-token"
}
```

## 功能

- **侧边栏**: 任务列表、项目进度、Agent 状态、实时事件流
- **命令面板** (Ctrl+Shift+P): 提交任务、启动/停止调度、刷新
- **状态栏**: 调度器连接状态 + 运行任务数
- **WebSocket 实时推送**: 自动重连、指数退避

## 依赖

- Singularity Dispatch运行在 http://127.0.0.1:5050
- WebSocket 服务器运行在 ws://127.0.0.1:5051
