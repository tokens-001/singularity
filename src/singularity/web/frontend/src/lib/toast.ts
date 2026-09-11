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

/**
 * 把 `request()` 抛出的**后端中文原因**格式化进提示文案。
 *
 * `request()`（lib/api.ts）已经把后端错误体里的 `error` 提出来了 —— 直接写
 * `catch { toast('加载失败') }` 等于把它扔掉，界面上只剩"失败"两个字，
 * 用户和排查的人都无从下手。实例：点文件「在文件夹中显示」只弹「定位失败」，
 * 而后端明明说了「文件不存在」—— 少这一句，问题多花了一轮才查出来。
 *
 * `useRun()` 是整段流程包这个纪律；这里给"只想包一句 toast"的场景用。
 */
export function errText(e: unknown, fallback: string): string {
  const m = e instanceof Error ? e.message : String(e ?? '')
  return m ? `${fallback}：${m}` : fallback
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
