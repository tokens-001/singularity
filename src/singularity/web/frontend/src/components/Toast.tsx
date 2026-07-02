import { create } from 'zustand'

type ToastKind = 'info' | 'error' | 'success'
interface ToastItem { id: number; msg: string; kind: ToastKind }

interface ToastState {
  toasts: ToastItem[]
  add: (msg: string, kind?: ToastKind) => void
  remove: (id: number) => void
}

let _nextId = 0

export const useToast = create<ToastState>((set) => ({
  toasts: [],
  add: (msg, kind = 'info') => {
    const id = ++_nextId
    set((s) => ({ toasts: [...s.toasts, { id, msg, kind }] }))
    setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), 4000)
  },
  remove: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

const KC: Record<ToastKind, string> = {
  info: 'var(--accent)',
  error: 'var(--accent-red)',
  success: 'var(--accent-green)',
}

export function ToastContainer() {
  const toasts = useToast((s) => s.toasts)
  const remove = useToast((s) => s.remove)
  if (!toasts.length) return null
  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div key={t.id} onClick={() => remove(t.id)} className="toast-item"
          style={{ border: `1px solid ${KC[t.kind]}` }}>
          {t.msg}
        </div>
      ))}
    </div>
  )
}
