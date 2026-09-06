const BASE = ''
async function request<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(BASE + url, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export interface Task { id: string; description: string; status: string; route_type: string; route_gate: string; route_role: string; project_id: string; execution_mode?: string; updated_at: number; created_at: number }
export interface TaskDetail extends Task { trace?: any; timeline?: any }

export const api = {
  status: () => request<any>('/api/status'),
  tasks: async (p?: string) => { const d = await request<any>(`/api/tasks${p||''}`); return (d?.tasks||d) as any[] },
  task: (id: string) => request<any>(`/api/tasks/${id}`),
  taskTrace: (id: string, section?: string) => request<any>(`/api/tasks/${id}/trace${section ? `?section=${section}` : ''}`),
  revealFile: (path: string) => request('/api/files/reveal', { method: 'POST', body: JSON.stringify({ path }) }),
  createTask: (desc: string) => request('/api/tasks',{method:'POST',body:JSON.stringify({description:desc})}),
  cancelTask: (id: string) => request(`/api/tasks/${id}/cancel`,{method:'POST'}),
  pauseTask: (id: string) => request(`/api/tasks/${id}/pause`,{method:'POST'}),
  resumeTask: (id: string) => request(`/api/tasks/${id}/resume`,{method:'POST'}),
  setTaskMode: (id: string, mode: string) => request(`/api/tasks/${id}/mode`,{method:'POST',body:JSON.stringify({mode})}),
  updateTask: (id: string, data: any) => request(`/api/tasks/${id}`,{method:'PUT',body:JSON.stringify(data)}),
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
  deleteProject: (id: string) => request(`/api/projects/${id}`,{method:'DELETE'}),
  gateConfirm: (id: string, gate: string, decision: string) =>
    request(`/api/projects/${id}/gate-confirm`,{method:'POST',body:JSON.stringify({gate,decision})}),

  observerChat: (q: string, mode?: string, pid?: string) => request<any>('/api/observer/chat',{method:'POST',body:JSON.stringify({question:q,execution_mode:mode||'auto_edit',project_id:pid||''})}),

  agents: () => request<any>('/api/agents'),
  deleteAgent: (model: string) => request(`/api/agents/any/${model}`,{method:'DELETE'}),
  updateAgent: (model: string, data: any) => request(`/api/agents/any/${model}`,{method:'PUT',body:JSON.stringify(data)}),
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
  observerModel: async () => { const d = await request<any>('/api/observer/model'); return d?.model_id || '' },
  setObserverModel: (modelId: string) => request('/api/observer/model',{method:'PUT',body:JSON.stringify({model_id: modelId})}),

  skills: async () => { const d = await request<any>('/api/skills'); return (d?.skills||d||[]) as any[] },
  addSkill: (data: any) => request('/api/skills',{method:'POST',body:JSON.stringify(data)}),
  deleteSkill: (name: string) => request(`/api/skills/${name}`,{method:'DELETE'}),
  agentSkills: async (model: string) => { const d = await request<any>(`/api/agents/any/${model}/skills`); return { skills: d?.skill_names || d?.skills || [], available: d?.available || [] } },
  updateAgentSkills: (model: string, skills: string[]) =>
    request(`/api/agents/any/${model}/skills`,{method:'PUT',body:JSON.stringify({skills})}),

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
  updateRole: (key: string, data: any) => request(`/api/roles/${key}`, { method: 'PATCH', body: JSON.stringify(data) }),
}
