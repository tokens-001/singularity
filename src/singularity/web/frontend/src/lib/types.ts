// 共享类型 — ponytail: 只加实际用到的字段，不全量建模

export interface ModelInfo {
  id: string; display?: string; provider?: string; cost?: string; speed?: string
  rating?: string; api_available?: boolean; recommended_for?: string[]
  strengths?: string[]; notes?: string; max_turns?: number; reasoning?: boolean
  /** 单价 (USD / 百万 token，混合价)。**null = 未配置**，别当 0 用。 */
  price_per_m?: number | null
}

export interface ApiStoreItem {
  id: string; provider?: string; base_url?: string; api_key_env?: string
  status?: string; notes?: string
}

export interface AgentItem {
  model: string; type?: string; entry?: string; api_key_env?: string
  max_turns?: number; default?: boolean; sandbox?: string
  request_template?: { reasoning_effort?: string; [k: string]: any }
}

export interface AgentsData {
  // 只有 "any" 一档 —— 两档制合并后 D/E 这些档位名已经不存在了，
  // 类型里留着的 D 只是旧字段的影子（全仓无人读，2026-09-11 清掉）。
  any?: AgentItem[]
  _disabled?: { any?: string[] }
  _order?: { any?: string[] }
}

/** 阶段 → 模型。`custom` 里没出现的阶段 = 用整个激活池（旧行为）。
 *  GET /api/phase-models 两个键**总是**返回，所以这里不标可选 —— 标了调用方到处要判空。 */
export interface PhaseModelsData {
  phases: { key: string; label: string; hint?: string }[]
  custom: Record<string, string[]>
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
