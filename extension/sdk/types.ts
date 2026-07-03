/** 奇点 SDK 类型定义 */

export interface QidianTask {
    id: string;
    description: string;
    status: 'pending' | 'routed' | 'dispatched' | 'running' | 'done' | 'failed' | 'rolled_back' | 'decomposed' | 'blocked';
    route_level?: 'any';
    route_type?: string;
    priority: number;
    depth: number;
    held: boolean;
    retry_count: number;
    max_retries: number;
    created_at: number;
    updated_at: number;
    error?: string;
    snapshot_id?: string;
    depends_on: string[];
    children: string[];
}

export interface QidianProject {
    id: string;
    name: string;
    template: string;
    phase: string;
    description: string;
    scope: string;
    token_budget_total: number;
    token_spent: number;
    task_ids: string[];
}

export interface QidianAgent {
    model: string;
    type: 'openai-agent' | 'claude-cli' | 'zhipu-api';
    level: 'any';
    max_turns: number;
    default: boolean;
    sandbox: 'worktree' | 'inline' | 'none';
}

export interface QidianEvent {
    kind: string;
    msg: string;
    ts: number;
    task_id?: string;
}

export interface QidianStatus {
    status: 'ok' | 'error';
    disk_free_mb: number;
    loop_running: boolean;
    sse_clients: number;
    projects: number;
}
