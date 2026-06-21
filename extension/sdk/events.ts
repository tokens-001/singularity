/** 奇点 SDK 事件钩子接口 */

import type { QidianTask, QidianProject, QidianEvent } from './types';

export interface QidianEventHooks {
    onTaskCreated?: (task: QidianTask) => void;
    onTaskUpdated?: (task: QidianTask) => void;
    onTaskDeleted?: (taskId: string) => void;
    onProjectPhaseChange?: (project: QidianProject, from: string, to: string) => void;
    onLoopStarted?: () => void;
    onLoopStopped?: () => void;
    onError?: (error: string) => void;
}

export class QidianEventBus {
    private listeners = new Map<string, Set<Function>>();

    on(event: string, fn: Function) {
        const s = this.listeners.get(event) || new Set();
        s.add(fn);
        this.listeners.set(event, s);
    }

    off(event: string, fn: Function) {
        this.listeners.get(event)?.delete(fn);
    }

    emit(event: string, data?: any) {
        this.listeners.get(event)?.forEach(fn => {
            try { fn(data); } catch { /* isolate errors */ }
        });
    }

    handleServerEvent(evt: QidianEvent) {
        this.emit(evt.kind, evt);
        this.emit('*', evt);
    }
}
