const BASE = ''
async function request<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(BASE + url, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export interface Task { id: string; description: string; status: string; route_level: string; route_gate: string; route_role: string; project_id: string; updated_at: number; created_at: number }
export interface TaskDetail extends Task { trace?: any; timeline?: any }

export const api = {
  status: () => request<any>('/api/status'),
  tasks: async (p?: string) => { const d = await request<any>(`/api/tasks${p||''}`); return (d?.tasks||d) as any[] },
  task: (id: string) => request<any>(`/api/tasks/${id}`),
  taskTrace: (id: string) => request<any>(`/api/tasks/${id}/trace`),
  createTask: (desc: string, level='E') => request('/api/tasks',{method:'POST',body:JSON.stringify({description:desc,level})}),
  cancelTask: (id: string) => request(`/api/tasks/${id}/cancel`,{method:'POST'}),
  retryTask: (id: string) => request(`/api/tasks/${id}/retry`,{method:'POST'}),
  holdTask: (id: string) => request(`/api/tasks/${id}/hold`,{method:'POST'}),
  releaseTask: (id: string) => request(`/api/tasks/${id}/release`,{method:'POST'}),
  deleteTask: (id: string) => request(`/api/tasks/${id}/delete`,{method:'POST'}),
  applyTask: (id: string) => request(`/api/tasks/${id}/apply`,{method:'POST'}),
  approveTask: (id: string) => request(`/api/tasks/${id}/approval`,{method:'POST'}),
  rollbackTask: (id: string) => request(`/api/tasks/${id}/rollback`,{method:'POST'}),

  projects: async () => { const d = await request<any>('/api/projects'); return (d?.projects||d) as any[] },
  project: (id: string) => request<any>(`/api/projects/${id}`),
  createProject: (data: any) => request('/api/projects',{method:'POST',body:JSON.stringify(data)}),
  runPhase: (id: string) => request(`/api/projects/${id}/run-phase`,{method:'POST'}),
  gateConfirm: (id: string, gate: string, verdict: string) =>
    request(`/api/projects/${id}/gate-confirm`,{method:'POST',body:JSON.stringify({gate,verdict})}),

  observerChat: (q: string) => request<any>('/api/observer/chat',{method:'POST',body:JSON.stringify({question:q})}),

  agents: () => request<any>('/api/agents'),
  deleteAgent: (level: string, model: string) => request(`/api/agents/${level}/${model}`,{method:'DELETE'}),
  updateAgent: (level: string, model: string, data: any) => request(`/api/agents/${level}/${model}`,{method:'PUT',body:JSON.stringify(data)}),
  addAgent: (data: any) => request('/api/agents',{method:'POST',body:JSON.stringify(data)}),

  models: async () => { const d = await request<any>('/api/models'); return Object.values(d||{}) as any[] },
  addModel: (data: any) => request('/api/models',{method:'POST',body:JSON.stringify(data)}),
  updateModel: (id: string, data: any) => request(`/api/models/${id}`,{method:'PUT',body:JSON.stringify(data)}),
  deleteModel: (id: string) => request(`/api/models/${id}`,{method:'DELETE'}),
  importModels: (data: any) => request('/api/models/import',{method:'POST',body:JSON.stringify(data)}),

  apiStore: async () => { const d = await request<any>('/api/api-store'); return Object.values(d||{}) as any[] },
  addApiStore: (data: any) => request('/api/api-store',{method:'POST',body:JSON.stringify(data)}),
  deleteApiStore: (id: string) => request(`/api/api-store/${id}`,{method:'DELETE'}),
  scanApiStore: (id: string) => request(`/api/api-store/${id}/scan`,{method:'POST'}),

  skills: async () => { const d = await request<any>('/api/skills'); return (d?.skills||d||[]) as any[] },
  addSkill: (data: any) => request('/api/skills',{method:'POST',body:JSON.stringify(data)}),
  deleteSkill: (name: string) => request(`/api/skills/${name}`,{method:'DELETE'}),
  agentSkills: async (level: string, model: string) => { const d = await request<any>(`/api/agents/${level}/${model}/skills`); return { skills: d?.skill_names || d?.skills || [], available: d?.available || [] } },
  updateAgentSkills: (level: string, model: string, skills: string[]) =>
    request(`/api/agents/${level}/${model}/skills`,{method:'PUT',body:JSON.stringify({skills})}),

  fusionConfig: () => request<any>('/api/fusion/config'),
  updateFusionConfig: (data: any) => request('/api/fusion/config',{method:'PUT',body:JSON.stringify(data)}),

  tokenUsage: () => request<any>('/api/token-usage'),
  updateTokenBudget: (data: any) => request('/api/token-budget',{method:'PUT',body:JSON.stringify(data)}),

  startLoop: () => request('/api/loop/start',{method:'POST'}),
  stopLoop: () => request('/api/loop/stop',{method:'POST'}),
  loopStatus: () => request<any>('/api/loop/status'),

  mcpServers: async () => { const d = await request<any>('/api/mcp/servers'); return (d?.servers||d||[]) as any[] },
  mcpTools: async () => { const d = await request<any>('/api/mcp/tools'); return (d?.tools||d||[]) as any[] },
  addMcpServer: (data: any) => request('/api/mcp/servers',{method:'POST',body:JSON.stringify(data)}),

  roles: () => request<any>('/api/roles'),
}
