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
 *
 * 后端还会用「**成功 + warning**」表达第三种结果：操作做下去了，但没达到你要的效果。
 * 典型是 `agent_add`（POST /api/agents）—— 启用一个不在模型库里的模型，它会照样
 * 写进配置，同时回 warning「补不出 API 地址和 key → 这条 agent 是空壳，不会被调度」。
 * 只接 catch 的话这句就丢了，用户看到的是"点了没反应"，然后怎么查都查不出原因。
 */
/**
 * 跑一个会改状态的请求，统一报成功/失败。**把响应带回来。**
 *
 * ⚠️ 原来返回 `Promise<boolean>` —— **响应被丢掉了**，于是 `const res = await run(...)`
 * 拿到的是 `true`，`res?.suggested_flow` **永远是 undefined**
 * ⇒ 后端在 200 里回的"看着像小活，要不要走轻量流程"（§47 的人审建议）**是死功能**
 * （2026-09-14，外派④扫前端时对比同仓两种写法抓出：`Chat.tsx` 直接 `await` 就是对的）。
 *
 * 返回值约定：**成功 → 响应本身**（没有响应体时退回 `true`）；**失败 → `false`**。
 * ⇒ 既有的 `if (!(await run(...)))` 判断**一个字都不用改**（成功侧恒为真值）。
 */
export function useRun() {
  const toast = useToast()
  return async (fn: () => Promise<unknown>, okMsg?: string): Promise<unknown> => {
    try {
      const r = await fn()
      const warn = (r as { warning?: string } | null | undefined)?.warning
      if (warn) toast(warn, 'info')            // 有 warning 就不报成功，免得两句话互相打架
      else if (okMsg) toast(okMsg, 'success')
      return r ?? true                         // ← 关键：把响应带出去（`?? true` 保住"成功即真值"）
    }
    catch (e) { toast(e instanceof Error ? e.message : String(e), 'error'); return false }
  }
}
