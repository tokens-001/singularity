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
  // 对话: project_id → 消息列表, _default 为无项目时的通用对话
  conversations: Record<string, ChatMsg[]>
  activeProjectId: string
  setActiveProject: (id: string) => void
  /** `projectId` 缺省 = 当前打开的那个项目（旧行为）。
   *  ⚠️ **服务端主动推的消息必须显式给 projectId** —— 不给就会塞进"用户此刻正开着的
   *  那个项目"的对话里：你在看 A，B 的汇报插进 A（2026-09-17 发现，在接
   *  "观察者主动汇报"之前先修，否则一推就串台）。 */
  addChatMsg: (msg: ChatMsg, projectId?: string) => void
  clearConversation: (id: string) => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      sidebarCollapsed: false,
      toggleSidebar: () => set(s => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      conversations: {},
      activeProjectId: '_default',
      setActiveProject: (id) => set({ activeProjectId: id }),
      addChatMsg: (msg, projectId) => {
        const pid = projectId || get().activeProjectId
        set(s => {
          const convs = { ...s.conversations }
          const msgs = [...(convs[pid] || []), msg].slice(-200)
          convs[pid] = msgs
          return { conversations: convs }
        })
      },
      clearConversation: (id) => set(s => {
        const convs = { ...s.conversations }
        delete convs[id]
        return { conversations: convs }
      }),
    }),
    {
      name: 'qidian-chat-v2',
      partialize: (s) => ({ sidebarCollapsed: s.sidebarCollapsed, conversations: s.conversations }),
    }
  )
)
