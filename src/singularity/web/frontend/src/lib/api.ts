import type { Task, TaskDetail } from '../stores/app'

const BASE = ''

async function request<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(BASE + url, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export type { Task, TaskDetail }

export const api = {
  status: () => request<any>('/api/status'),

  tasks: async (params?: string) => {
    const data = await request<any>(`/api/tasks${params || ''}`)
    return (data?.tasks || data) as any[]
  },
  task: (id: string) => request<TaskDetail>(`/api/tasks/${id}`),
  taskTrace: (id: string) => request<any>(`/api/tasks/${id}/trace`),
  createTask: (desc: string, level = 'E') =>
    request('/api/tasks', { method: 'POST', body: JSON.stringify({ description: desc, level }) }),
  cancelTask: (id: string) => request(`/api/tasks/${id}/cancel`, { method: 'POST' }),
  retryTask: (id: string) => request(`/api/tasks/${id}/retry`, { method: 'POST' }),
  holdTask: (id: string) => request(`/api/tasks/${id}/hold`, { method: 'POST' }),
  releaseTask: (id: string) => request(`/api/tasks/${id}/release`, { method: 'POST' }),
  deleteTask: (id: string) => request(`/api/tasks/${id}/delete`, { method: 'POST' }),
  applyTask: (id: string) => request(`/api/tasks/${id}/apply`, { method: 'POST' }),
  rollbackTask: (id: string) => request(`/api/tasks/${id}/rollback`, { method: 'POST' }),
  approveTask: (id: string) => request(`/api/tasks/${id}/approval`, { method: 'POST' }),

  projects: async () => { const d = await request<any>('/api/projects'); return (d?.projects || d) as any[] },
  project: (id: string) => request<any>(`/api/projects/${id}`),
  createProject: (data: any) => request('/api/projects', { method: 'POST', body: JSON.stringify(data) }),
  runPhase: (id: string) => request(`/api/projects/${id}/run-phase`, { method: 'POST' }),
  startProject: (id: string) => request(`/api/projects/${id}/start`, { method: 'POST' }),
  gateConfirm: (id: string, gate: string, verdict: string) =>
    request(`/api/projects/${id}/gate-confirm`, { method: 'POST', body: JSON.stringify({ gate, verdict }) }),

  observerChat: (question: string) =>
    request<any>('/api/observer/chat', { method: 'POST', body: JSON.stringify({ question }) }),

  agents: () => request<any>('/api/agents'),
  deleteAgent: (level: string, model: string) =>
    request(`/api/agents/${level}/${model}`, { method: 'DELETE' }),

  models: async () => { const d = await request<any>('/api/models'); return Object.values(d || {}) as any[] },

  mcpServers: async () => { const d = await request<any>('/api/mcp/servers'); return (d?.servers || d || []) as any[] },
  mcpTools: async () => { const d = await request<any>('/api/mcp/tools'); return (d?.tools || d || []) as any[] },

  startLoop: () => request('/api/loop/start', { method: 'POST' }),
  stopLoop: () => request('/api/loop/stop', { method: 'POST' }),
  loopStatus: () => request<any>('/api/loop/status'),

  tokenUsage: () => request<any>('/api/token-usage'),
}
