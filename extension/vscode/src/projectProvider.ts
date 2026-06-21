import * as vscode from 'vscode';
import { WSClient } from './wsClient';

export class ProjectProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
    private _onDidChange = new vscode.EventEmitter<void>();
    readonly onDidChangeTreeData = this._onDidChange.event;
    private projects: any[] = [];

    constructor(private ws: WSClient) { this.fetch(); }

    async fetch() {
        try {
            const baseUrl = vscode.workspace.getConfiguration('qidian').get('baseUrl', 'http://127.0.0.1:5050');
            const r = await fetch(`${baseUrl}/api/projects`);
            const d = await r.json();
            this.projects = d.projects || [];
        } catch { }
        this.refresh();
    }

    refresh() { this._onDidChange.fire(); }

    getTreeItem(e: vscode.TreeItem): vscode.TreeItem { return e; }

    getChildren(): vscode.TreeItem[] {
        const phaseIcons: Record<string, string> = {
            gate1: '$(pass)', gate2: '$(pass-filled)', gate3: '$(shield)',
            planning: '$(edit)', executing: '$(run)', done: '$(check-all)',
        };
        return this.projects.map(p => {
            const icon = phaseIcons[p.phase] || '$(folder)';
            const item = new vscode.TreeItem(p.name, vscode.TreeItemCollapsibleState.None);
            item.id = p.id;
            item.description = `${p.phase} | ${p.template || ''}`;
            item.iconPath = new vscode.ThemeIcon(icon.replace('$(', '').replace(')', ''));
            item.tooltip = p.description?.slice(0, 200) || p.name;
            return item;
        });
    }
}
