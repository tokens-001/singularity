import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface Task {
  id: string; title: string; status: string; level: string;
  agent: string; updated_at: number; project_id?: string;
}

interface AppState {
  sidebarCollapsed: boolean
  tasks: Task[]
  taskFilters: { status: string; level: string }
  toggleSidebar: () => void
  setTasks: (tasks: Task[]) => void
  setTaskFilters: (filters: Partial<AppState['taskFilters']>) => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      tasks: [],
      taskFilters: { status: '', level: '' },
      toggleSidebar: () => set(s => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setTasks: (tasks) => set({ tasks }),
      setTaskFilters: (f) => set(s => ({ taskFilters: { ...s.taskFilters, ...f } })),
    }),
    { name: 'qidian-app', partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed }) }
  )
)
