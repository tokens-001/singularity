const BASE = ''
async function request<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(BASE + url, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  })
  if (!res.ok) {
    // 后端错误体统一是 {"error": "..."}，优先把中文原因透出来（否则调用方只能看到 500 Internal Server Error）
    let detail = ''
    try { const body = await res.json(); detail = body?.error || body?.message || '' } catch { /* 非 JSON 响应 */ }
    throw new Error(detail || `${res.status} ${res.statusText}`)
  }
  return res.json()
}

export interface Task { id: string; description: string; status: string; route_type: string; route_gate: string; route_role: string; project_id: string; execution_mode?: string; updated_at: number; created_at: number }
export interface TaskDetail extends Task { trace?: any; timeline?: any }

export const api = {
  status: () => request<any>('/api/status'),
  tasks: async (p?: string) => { const d = await request<any>(`/api/tasks${p||''}`); return (d?.tasks||d) as any[] },
  task: (id: string) => request<any>(`/api/tasks/${id}`),
  taskTrace: (id: string, section?: string) => request<any>(`/api/tasks/${id}/trace${section ? `?section=${section}` : ''}`),
  revealFile: (path: string, projectId?: string) => request('/api/files/reveal', { method: 'POST', body: JSON.stringify({ path, project_id: projectId || '' }) }),
  createTask: (desc: string, project_id = '') => request('/api/tasks',{method:'POST',body:JSON.stringify({description:desc, project_id})}),
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
  projectsRoot: async () => { const d = await request<any>('/api/projects-root'); return d?.root || '' },
  setProjectsRoot: (path: string) => request('/api/projects-root',{method:'PUT',body:JSON.stringify({path})}),
  fsList: (path: string) => request(`/api/fs/ls?path=${encodeURIComponent(path)}`),
  fsMkdir: (path: string, name: string) => request('/api/fs/mkdir',{method:'POST',body:JSON.stringify({path,name})}),
  fsPick: () => request('/api/fs/pick',{method:'POST'}),
  gateConfirm: (id: string, gate: string, decision: string) =>
    request(`/api/projects/${id}/gate-confirm`,{method:'POST',body:JSON.stringify({gate,decision})}),
  traceability: (id: string) => request<any>(`/api/projects/${id}/traceability`),

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
  benchmarkModel: (id: string) => request(`/api/models/${id}/benchmark`,{method:'POST'}),
  // 单价走独立端点：它存 model_prices.json，不写 ModelEntry —— 这样「跑基准」
  // 「扫描导入」这些会整行重建模型的操作擦不掉用户手填的价。null = 清除。
  setModelPrice: (id: string, price: number | null) =>
    request(`/api/model-price/${id}`,{method:'PUT',body:JSON.stringify({price_per_m: price})}),

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
    // 键名必须是 skill_names：后端读的是 body.get("skill_names", [])。
    // 原来发 {skills} → 后端永远取到 [] → 把该模型的技能**覆盖成空数组**：
    // 界面上标签高亮成已选中、刷新即失效，而服务端数据已经被清掉了。
    request(`/api/agents/any/${model}/skills`,{method:'PUT',body:JSON.stringify({skill_names: skills})}),

  fusionConfig: () => request<any>('/api/fusion/config'),
  updateFusionConfig: (data: any) => request('/api/fusion/config',{method:'PUT',body:JSON.stringify(data)}),

  tokenUsage: () => request<any>('/api/token-usage'),
  // 跨天历史。与 tokenUsage 分开：后者被侧边栏每 30s 轮询，不能让它开始背 30 天的序列。
  usageHistory: (range: string) => request<any>(`/api/usage-history?range=${range}`),
  updateTokenBudget: (data: any) => request('/api/token-budget',{method:'PUT',body:JSON.stringify(data)}),

  startLoop: () => request('/api/loop/start',{method:'POST'}),
  stopLoop: () => request('/api/loop/stop',{method:'POST'}),
  loopStatus: () => request<any>('/api/loop/status'),
  conflicts: () => request<any>('/api/conflicts'),

  mcpServers: async () => { const d = await request<any>('/api/mcp/servers'); return (d?.servers||d||[]) as any[] },
  mcpTools: async () => { const d = await request<any>('/api/mcp/tools'); return (d?.tools||d||[]) as any[] },
  mcpRefresh: () => request<any>('/api/mcp/refresh',{method:'POST'}),
  addMcpServer: (data: any) => request('/api/mcp/servers',{method:'POST',body:JSON.stringify(data)}),

  dagMetrics: () => request<any>('/api/dag-metrics'),

  roles: () => request<any>('/api/roles'),
  updateRole: (key: string, data: any) => request(`/api/roles/${key}`, { method: 'PATCH', body: JSON.stringify(data) }),
  createRole: (data: any) => request<any>('/api/roles', { method: 'POST', body: JSON.stringify(data) }),
  deleteRole: (key: string) => request<any>(`/api/roles/${key}`, { method: 'DELETE' }),
  phaseRoles: () => request<any>('/api/phase-roles'),
  updatePhaseRoles: (map: Record<string, string>) =>
    request<any>('/api/phase-roles', { method: 'PUT', body: JSON.stringify({ map }) }),
  phaseModels: () => request<any>('/api/phase-models'),
  // 响应可能带 { warning }（提取员撞委员会 / 委员会退化）—— useRun 会把它 toast 出来，
  // 所以这里别把返回值丢掉。
  updatePhaseModels: (map: Record<string, string[]>) =>
    request<any>('/api/phase-models', { method: 'PUT', body: JSON.stringify({ map }) }),
}
