import * as vscode from 'vscode';

type EventHandler = (params: any) => void;

export class WSClient {
    private ws: WebSocket | null = null;
    private handlers = new Map<string, EventHandler[]>();
    private reconnectTimer: NodeJS.Timeout | null = null;
    private backoff = 1000;
    private _connected = false;

    constructor(private url: string) {}

    get connected(): boolean { return this._connected; }

    connect() {
        const token = vscode.workspace.getConfiguration('qidian').get('token', '');
        try {
            this.ws = new WebSocket(this.url);
            this.ws.onopen = () => {
                this._connected = true;
                this.backoff = 1000;
                this.ws!.send(JSON.stringify({ jsonrpc: '2.0', method: 'auth', params: { token } }));
                this.emit('connected', {});
            };
            this.ws.onmessage = (e) => {
                try {
                    const msg = JSON.parse(e.data);
                    if (msg.method === 'event') {
                        const kind = msg.params?.kind || '*';
                        this.emit(kind, msg.params);
                        this.emit('*', msg.params);
                    } else if (msg.method === 'auth_ok') {
                        this.emit('auth_ok', msg.params);
                    }
                } catch { /* ignore malformed */ }
            };
            this.ws.onclose = () => {
                this._connected = false;
                this.emit('disconnected', {});
                this.scheduleReconnect();
            };
            this.ws.onerror = () => { /* onclose will fire */ };
        } catch {
            this.scheduleReconnect();
        }
    }

    disconnect() {
        if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        this.ws?.close();
    }

    private scheduleReconnect() {
        if (this.reconnectTimer) return;
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.backoff = Math.min(this.backoff * 2, 30000);
            this.connect();
        }, this.backoff);
    }

    onEvent(pattern: string, handler: EventHandler) {
        const list = this.handlers.get(pattern) || [];
        list.push(handler);
        this.handlers.set(pattern, list);
    }

    private emit(kind: string, params: any) {
        for (const [pattern, handlers] of this.handlers) {
            if (pattern === '*' || this.match(pattern, kind)) {
                for (const h of handlers) h(params);
            }
        }
    }

    private match(pattern: string, kind: string): boolean {
        if (pattern === kind) return true;
        if (pattern.endsWith('.*')) return kind.startsWith(pattern.slice(0, -2));
        return false;
    }
}
