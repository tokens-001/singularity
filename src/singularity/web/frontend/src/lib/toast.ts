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
