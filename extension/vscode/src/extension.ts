import * as vscode from 'vscode';
import { TaskProvider } from './taskProvider';
import { ProjectProvider } from './projectProvider';
import { AgentProvider } from './agentProvider';
import { EventProvider } from './eventProvider';
import { WSClient } from './wsClient';
import { registerCommands } from './commands';
import { StatusBarManager } from './statusBar';

let wsClient: WSClient;

export function activate(context: vscode.ExtensionContext) {
    const baseUrl = vscode.workspace.getConfiguration('qidian').get('baseUrl', 'http://127.0.0.1:5050');
    const wsUrl = baseUrl.replace('http://', 'ws://').replace(':5050', ':5051');

    // WebSocket
    wsClient = new WSClient(wsUrl);
    wsClient.connect();

    // TreeViews
    const taskProvider = new TaskProvider(wsClient);
    const projectProvider = new ProjectProvider(wsClient);
    const agentProvider = new AgentProvider(wsClient);
    const eventProvider = new EventProvider(wsClient);

    context.subscriptions.push(
        vscode.window.registerTreeDataProvider('qidian.tasks', taskProvider),
        vscode.window.registerTreeDataProvider('qidian.projects', projectProvider),
        vscode.window.registerTreeDataProvider('qidian.agents', agentProvider),
        vscode.window.registerTreeDataProvider('qidian.events', eventProvider),
    );

    // Commands
    registerCommands(context, baseUrl, wsClient, taskProvider);

    // Status bar
    const statusBar = new StatusBarManager(wsClient);
    context.subscriptions.push(statusBar);

    // Auto-refresh
    wsClient.onEvent('task.*', () => { taskProvider.refresh(); statusBar.update(); });
    wsClient.onEvent('project.*', () => projectProvider.refresh());
    wsClient.onEvent('agent.*', () => agentProvider.refresh());
    wsClient.onEvent('*', (evt: any) => {
        eventProvider.addEvent(evt);
        statusBar.update();
    });
}

export function deactivate() {
    wsClient?.disconnect();
}
