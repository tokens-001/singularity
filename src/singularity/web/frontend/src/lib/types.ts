// 共享类型 — ponytail: 只加实际用到的字段，不全量建模

export interface ModelInfo {
  id: string; display?: string; provider?: string; cost?: string; speed?: string
  rating?: string; api_available?: boolean; recommended_for?: string[]
  strengths?: string[]; notes?: string; max_turns?: number; reasoning?: boolean
}

export interface ApiStoreItem {
  id: string; provider?: string; base_url?: string; api_key_env?: string
  status?: string; notes?: string
}

export interface AgentItem {
  model: string; type?: string; entry?: string; api_key_env?: string
  max_turns?: number; default?: boolean; roles?: string[]; sandbox?: string
}

export interface AgentsData {
  any?: AgentItem[]; D?: AgentItem[]
  _disabled?: { any?: string[]; D?: string[] }
  _order?: { any?: string[] }
}

export interface ProjectInfo {
  id: string; name: string; description?: string; phase?: string
  task_count?: number; template?: string
}

export interface ProjectDetail extends ProjectInfo {
  lineage?: { action: string; agent?: string; task_count?: number }[]
}

export interface TaskInfo {
  id: string; description: string; status: string; project_id?: string
  route_type?: string; route_role?: string; updated_at?: number
}

export interface TaskDetail extends TaskInfo {
  trace?: any
}

export interface SkillInfo {
  name: string; description?: string; type?: string; content?: string
}
