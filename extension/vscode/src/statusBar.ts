import * as vscode from 'vscode';
import { WSClient } from './wsClient';

export class StatusBarManager {
    private item: vscode.StatusBarItem;

    constructor(private ws: WSClient) {
        this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
        this.item.text = '$(server) 奇点: 连接中...';
        this.item.show();
        ws.onEvent('connected', () => this.update());
        ws.onEvent('disconnected', () => {
            this.item.text = '$(circle-slash) 奇点: 断开';
            this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
        });
    }

    update() {
        if (!this.ws.connected) return;
        const baseUrl = vscode.workspace.getConfiguration('qidian').get('baseUrl', 'http://127.0.0.1:5050');
        fetch(`${baseUrl}/api/loop/status`).then(r => r.json()).then(d => {
            const running = d.running ? '●' : '○';
            const conc = d.concurrent || 1;
            this.item.text = `$(server) 奇点: ${running} 调度器 (x${conc})`;
            this.item.backgroundColor = d.running
                ? undefined
                : new vscode.ThemeColor('statusBarItem.warningBackground');
        }).catch(() => {
            this.item.text = '$(circle-slash) 奇点: 离线';
        });
    }

    dispose() { this.item.dispose(); }
}
