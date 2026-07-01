import { create } from 'zustand'
import { persist } from 'zustand/middleware'

export interface Task {
  id: string; description: string; status: string; route_type: string;
  route_gate: string; route_role: string; project_id: string;
  updated_at: number; created_at: number;
}
export interface TaskDetail extends Task { trace?: any; timeline?: any }

export interface ChatMsg { role: 'user' | 'assistant'; content: string; ts: number }

interface AppState {
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  chatMsgs: ChatMsg[]
  setChatMsgs: (msgs: ChatMsg[]) => void
  addChatMsg: (msg: ChatMsg) => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      toggleSidebar: () => set(s => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      chatMsgs: [],
      setChatMsgs: (msgs) => set({ chatMsgs: msgs }),
      addChatMsg: (msg) => set(s => ({ chatMsgs: [...s.chatMsgs, msg].slice(-200) })),
    }),
    { name: 'qidian-chat', partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed, chatMsgs: s.chatMsgs }) }
  )
)
