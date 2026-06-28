import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface Task {
  id: string; description: string; status: string; route_type: string;
  route_gate: string; route_role: string; project_id: string;
  updated_at: number; created_at: number;
}
export interface TaskDetail extends Task { trace?: any; timeline?: any }

interface AppState {
  sidebarCollapsed: boolean
  toggleSidebar: () => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      toggleSidebar: () => set(s => ({ sidebarCollapsed: !s.sidebarCollapsed })),
    }),
    { name: 'qidian-app', partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed }) }
  )
)
