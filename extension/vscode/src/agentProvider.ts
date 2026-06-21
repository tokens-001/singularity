import * as vscode from 'vscode';
import { WSClient } from './wsClient';

export class AgentProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChange = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData = this._onDidChange.event;
    private agents: Record<string, any[]> = {};

    constructor(private ws: WSClient) { this.fetch(); }

    async fetch() {
        try {
            const baseUrl = vscode.workspace.getConfiguration('qidian').get('baseUrl', 'http://127.0.0.1:5050');
            const r = await fetch(`${baseUrl}/api/agents`);
            const d = await r.json();
            this.agents = d;
        } catch { }
        this.refresh();
    }

    refresh() { this._onDidChange.fire(); }

    getTreeItem(e: vscode.TreeItem): vscode.TreeItem { return e; }

    getChildren(parent?: vscode.TreeItem): vscode.TreeItem[] {
        if (!parent) {
            return ['E', 'E+', 'D'].map(tier => {
                const count = (this.agents[tier] || []).length;
                const item = new vscode.TreeItem(
                    `层级 ${tier} (${count})`,
                    vscode.TreeItemCollapsibleState.Expanded
                );
                item.id = `tier-${tier}`;
                item.iconPath = new vscode.ThemeIcon(
                    tier === 'E' ? 'zap' : tier === 'E+' ? 'star' : 'rocket'
                );
                return item;
            });
        }
        const tier = parent.id?.replace('tier-', '') || '';
        return (this.agents[tier] || []).map((a: any) => {
            const item = new vscode.TreeItem(a.model || '?', vscode.TreeItemCollapsibleState.None);
            item.description = `${a.type || ''} | max_turns=${a.max_turns || 5}`;
            return item;
        });
    }
}
