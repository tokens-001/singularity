import * as vscode from 'vscode';
import { WSClient } from './wsClient';

interface TaskItem {
    id: string; description: string; status: string;
    route_level?: string; priority?: number;
}

export class TaskProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChange = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData = this._onDidChange.event;
    private tasks: TaskItem[] = [];

    constructor(private ws: WSClient) {
        this.fetch();
        ws.onEvent('task.*', () => this.fetch());
        ws.onEvent('loop.*', () => this.fetch());
    }

    async fetch() {
        try {
            const baseUrl = vscode.workspace.getConfiguration('qidian').get('baseUrl', 'http://127.0.0.1:5050');
            const r = await fetch(`${baseUrl}/api/tasks`);
            const d = await r.json();
            this.tasks = d.tasks || [];
        } catch { /* offline */ }
        this.refresh();
    }

    refresh() { this._onDidChange.fire(); }

    getTreeItem(element: vscode.TreeItem): vscode.TreeItem { return element; }

    getChildren(): vscode.TreeItem[] {
        const statusIcons: Record<string, string> = {
            pending: '$(circle-outline)', running: '$(sync~spin)',
            done: '$(check)', failed: '$(error)', blocked: '$(debug-pause)',
        };
        return this.tasks.map(t => {
            const label = `${t.description.slice(0, 50)}`;
            const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
            item.id = t.id;
            item.tooltip = `${t.id} | ${t.status} | L${t.route_level || '?'} | P${t.priority || 0}`;
            item.iconPath = new vscode.ThemeIcon(
                statusIcons[t.status]?.replace('$(', '').replace(')', '') || 'circle-outline'
            );
            if (t.status === 'running') {
                item.description = `${t.route_level || ''} ${t.status}`;
            } else {
                item.description = `${t.status} | ${t.route_level || ''}`;
            }
            item.command = {
                command: 'qidian.taskDetail', title: '查看详情',
                arguments: [t.id],
            };
            return item;
        });
    }
}
