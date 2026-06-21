import * as vscode from 'vscode';
import { WSClient } from './wsClient';

interface QidianEvent { kind: string; msg: string; ts: number; }

export class EventProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChange = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData = this._onDidChange.event;
    private events: QidianEvent[] = [];
    private maxEvents = 100;

    constructor(private ws: WSClient) {
        ws.onEvent('*', (params: any) => {
            if (params?.kind && params?.msg) {
                this.events.unshift({ kind: params.kind, msg: params.msg, ts: params.ts || Date.now() });
                if (this.events.length > this.maxEvents) this.events.length = this.maxEvents;
                this.refresh();
            }
        });
    }

    addEvent(evt: QidianEvent) { this.events.unshift(evt); if (this.events.length > this.maxEvents) this.events.length = this.maxEvents; }

    refresh() { this._onDidChange.fire(); }

    getTreeItem(e: vscode.TreeItem): vscode.TreeItem { return e; }

    getChildren(): vscode.TreeItem[] {
        const kindIcons: Record<string, string> = {
            task: '$(tasklist)', system: '$(info)', error: '$(error)',
            workflow: '$(git-merge)', memory: '$(database)', idle: '$(history)',
        };
        return this.events.map(e => {
            const time = new Date(e.ts * 1000).toLocaleTimeString('zh-CN');
            const label = `[${time}] ${e.msg.slice(0, 60)}`;
            const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
            item.iconPath = new vscode.ThemeIcon(
                (kindIcons[e.kind] || '$(pulse)').replace('$(', '').replace(')', '')
            );
            item.tooltip = `${e.kind}: ${e.msg}`;
            return item;
        });
    }
}
