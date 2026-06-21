import * as vscode from 'vscode';
import { WSClient } from './wsClient';
import { TaskProvider } from './taskProvider';

export function registerCommands(
    context: vscode.ExtensionContext,
    baseUrl: string,
    ws: WSClient,
    taskProvider: TaskProvider,
) {
    const api = async (path: string, opts?: RequestInit) => {
        try {
            const r = await fetch(`${baseUrl}${path}`, {
                headers: { 'Content-Type': 'application/json', ...opts?.headers },
                ...opts,
            });
            return await r.json();
        } catch (e: any) {
            vscode.window.showErrorMessage(`API 错误: ${e.message}`);
        }
    };

    context.subscriptions.push(
        vscode.commands.registerCommand('qidian.submitTask', async () => {
            const desc = await vscode.window.showInputBox({
                prompt: '输入任务描述',
                placeHolder: '描述你要执行的任务...',
            });
            if (desc) {
                const r = await api('/api/tasks', { method: 'POST', body: JSON.stringify({ description: desc }) });
                if (r?.task_id) vscode.window.showInformationMessage(`任务已创建: ${(r.task_id as string).slice(-8)}`);
                taskProvider.fetch();
            }
        }),

        vscode.commands.registerCommand('qidian.startLoop', async () => {
            await api('/api/loop/start', { method: 'POST', body: JSON.stringify({ concurrent: 4 }) });
            vscode.window.showInformationMessage('调度循环已启动');
        }),

        vscode.commands.registerCommand('qidian.stopLoop', async () => {
            await api('/api/loop/stop', { method: 'POST' });
            vscode.window.showInformationMessage('调度循环已停止');
        }),

        vscode.commands.registerCommand('qidian.refreshAll', () => {
            taskProvider.fetch();
            vscode.commands.executeCommand('workbench.action.webview.reloadWebviewAction');
        }),

        vscode.commands.registerCommand('qidian.taskDetail', async (taskId: string) => {
            const r = await api(`/api/tasks/${taskId}`);
            if (r) {
                const doc = await vscode.workspace.openTextDocument({
                    content: JSON.stringify(r, null, 2),
                    language: 'json',
                });
                await vscode.window.showTextDocument(doc, { preview: true });
            }
        }),
    );
}
