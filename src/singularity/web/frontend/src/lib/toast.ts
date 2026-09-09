import { App as AntApp } from 'antd'

type Kind = 'info' | 'error' | 'success'

/**
 * 全局提示 —— antd message 的薄封装，保留原 useToast 的调用姿势（toast(msg, kind)）。
 * 必须在 <AntApp>（main.tsx）内使用：静态 message.success() 读不到 ConfigProvider 的 theme，
 * 用 App.useApp() 拿到的实例才会吃 theme。
 */
export function useToast() {
  const { message } = AntApp.useApp()
  return (msg: string, kind: Kind = 'info') => message[kind](msg)
}

/** antd 的 Modal 实例（同样必须来自 App.useApp() 才吃 theme）。 */
export function useModal() {
  return AntApp.useApp().modal
}

/**
 * 包一层写操作：失败时把后端返回的中文原因 toast 出来，返回是否成功。
 * 不包的话，request() 抛出的异常没人接，界面上会「点了没反应」。
 * 用法：if (!(await run(() => api.createProject(form)))) return
 */
export function useRun() {
  const toast = useToast()
  return async (fn: () => Promise<unknown>, okMsg?: string): Promise<boolean> => {
    try { await fn(); if (okMsg) toast(okMsg, 'success'); return true }
    catch (e) { toast(e instanceof Error ? e.message : String(e), 'error'); return false }
  }
}
