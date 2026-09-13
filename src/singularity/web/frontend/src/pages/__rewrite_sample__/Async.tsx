/**
 * Async.tsx —— 异步资源四态的统一出口（样板件，和 McpTab 样板一起评审）。
 *
 * 为什么要有这个文件：本前端已经出过三次同一形状的事故（GATE3 的假绿灯 /
 * 任务卡的「✓ 阻断」/ 用量页的 $0.00），根因全部相同 ——
 * **判据落在"有没有值"而不是"是不是成功"上，而且默认那一侧是好消息。**
 * 这一家族还有一个更安静的变种：加载失败只 toast 一声（甚至不打扰），
 * 原地留着「加载中…」或「暂无数据」—— 把「读不到」渲染成「没有」。
 *
 * 这个文件把判据钉进类型里，让调用方**没得选**：
 *   loading —— 还没拿到答案。文字：「正在加载…」
 *   error   —— 拿到了"失败"这个答案。**它是一个答案，不是"没有数据"**：
 *              渲染失败原因 + 重试入口，绝不落进空态文案。
 *   ready   —— 真拿到了数据。空不空由数据本身决定（length === 0 → 空态文字）。
 *              有数据之后的再取失败 → 旧数据保留 + 琥珀色「刷新失败」条。
 *              （失败不许把"有"渲染成"没有"；但失败也不许被 toast 一声就吞掉。）
 *
 * 配色沿用全站口径（GatePanel / TaskCard / money.ts 同源）：
 *   灰 = 没数据 · 琥珀 = 核不了 / 需要注意 · 绿 = 通过 · 红 = 不通过。
 * 每一态都必须有**文字**，颜色只是辅助 —— 不许只靠颜色区分。
 *
 * 提升路径：评审通过后本文件原样移到 src/components/Async.tsx，
 * 唯一要改的是底部那条 import 的相对路径（见本目录 README.md）。
 */
import { useCallback, useRef, useState, type ReactNode } from 'react'
import { errText } from '../../lib/toast'

/**
 * 一个异步资源只有这三种相。**"空"不是相** —— 它是 ready 相里数据的一种取值。
 * 把空做成第四相的写法（loading/empty/error/data 四个布尔）正是老病的温床：
 * 四个布尔有 16 种组合，其中"全 false"渲染成什么没人说得清。
 */
export type Loadable<T> =
  | { phase: 'loading' }
  | { phase: 'error'; message: string }
  | {
      phase: 'ready'
      data: T
      /** 有数据之后的再取失败：旧数据保留，但失败必须可见（琥珀条），不许只进 toast。 */
      staleError?: string
    }

/**
 * 拉一个异步资源。返回的 `load` 引用稳定，可以放心进 useEffect 依赖。
 *
 * @param fetcher   每次调用都发起新请求；用 ref 包住，内联箭头函数不会导致重拉
 * @param opts.errorLabel 错误文案前缀（如「加载 MCP 服务器失败」）——
 *                        request() 抛出的后端中文原因会接在冒号后面
 */
export function useResource<T>(
  fetcher: () => Promise<T>,
  opts: { errorLabel: string }
): {
  state: Loadable<T>
  /** 首次加载之后的再取进行中（刷新按钮转字用）。首次加载不算。 */
  reloading: boolean
  /**
   * initial=true：进入 loading 相（失败 → error 相）。用于首屏和错误态的「重试」。
   * initial=false：后台再取，**已渲染的数据原样保留**（失败 → staleError）。
   */
  load: (initial?: boolean) => Promise<void>
} {
  const [state, setState] = useState<Loadable<T>>({ phase: 'loading' })
  const [reloading, setReloading] = useState(false)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const labelRef = useRef(opts.errorLabel)
  labelRef.current = opts.errorLabel

  // ⚠️ **请求序号** —— 同仓的标准答案（`Chat.fetchSeq` / `Projects.detailSeq`），
  // 这个原语第一版**漏了它**（2026-09-14 外派⑧反审抓到）。没有它，三路并发
  // （挂载 / SSE / 轮询 / 手动刷新）下"后到说了算"，而且**两个方向都会错**：
  //   · 慢的**成功**后到 → 整包盖掉新数据
  //   · 慢的**失败**后到 → 往新成功的数据头上挂「刷新失败」琥珀条，
  //     而那条横幅写着"下面还是最近一次成功加载的数据" —— **在旧失败后到时它就在撒谎**
  //     （屏幕上恰恰就是最新的数据）。※ 这是铺开会**新增**的一种谎，不是原有洞的照旧。
  const seqRef = useRef(0)

  const load = useCallback(async (initial = false) => {
    const mySeq = ++seqRef.current
    if (initial) setState({ phase: 'loading' })
    else setReloading(true)
    try {
      const data = await fetcherRef.current()
      if (mySeq !== seqRef.current) return   // 已有更新的请求发出 → 这次结果作废
      // 成功覆盖一切（清掉 staleError）：数据是新的，失败条就该消失
      setState({ phase: 'ready', data })
    } catch (e) {
      // ⚠️ 过期的失败**同样要作废** —— 否则它会往新数据上贴一条假的"刷新失败"。
      if (mySeq !== seqRef.current) return
      // error 相带 errorLabel 前缀（它独立成块，需要完整句子）；
      // staleError 存**原文** —— 琥珀条自己已经说了「刷新失败：」，再带前缀就叠两层。
      const raw = e instanceof Error ? e.message : String(e ?? '')
      const msg = errText(e, labelRef.current)
      setState((prev) =>
        prev.phase === 'ready' ? { ...prev, staleError: raw } : { phase: 'error', message: msg }
      )
    } finally {
      // 只有"最新那次"结束才清 —— 否则先结束的旧请求会把还在飞的那次的状态抹掉
      if (mySeq === seqRef.current) setReloading(false)
    }
  }, [])

  return { state, reloading, load }
}

interface AsyncBoundaryProps<T extends readonly unknown[]> {
  state: Loadable<T>
  loadingText: string
  /** ready 且列表为空时的主文案 —— 要说"没有什么"，不许只画个图标 */
  emptyText: string
  /** 空态的"下一步"（怎么做才能不空）。空态只有配上出路才算把话说完。 */
  emptyHint?: string
  /** error 态的出口。错误态没有重试按钮 = 把人堵死在失败里。 */
  onRetry?: () => void
  children: (data: T) => ReactNode
}

/**
 * 四态渲染边界。loading / error / empty 三态在这里统一出文字，
 * ready 的正文由调用方渲染 —— **列表为空的判据是 `length === 0`，
 * 不是"真值与否"**：空数组是真值，`!!data` 会把"没有"渲染成"有"。
 */
export function AsyncBoundary<T extends readonly unknown[]>({
  state,
  loadingText,
  emptyText,
  emptyHint,
  onRetry,
  children,
}: AsyncBoundaryProps<T>) {
  return (
    <>
      {state.phase === 'loading' && (
        <div role="status" className="fs-11 text-muted" style={{ padding: 20, textAlign: 'center' }}>
          {loadingText}
        </div>
      )}
      {state.phase === 'error' && (
        <div
          role="alert"
          style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--accent-red, #dc2626)' }}
        >
          <div>{state.message}</div>
          {onRetry && (
            <button onClick={onRetry} className="btn-sm" style={{ marginTop: 8 }}>
              重试
            </button>
          )}
        </div>
      )}
      {state.phase === 'ready' && (
        <>
          {state.staleError && (
            <div
              role="alert"
              style={{
                marginBottom: 8, padding: '6px 10px', fontSize: 11, color: '#b45309',
                background: '#fffdf5', border: '1px solid #e8dcc0', borderRadius: 6,
              }}
            >
              刷新失败：{state.staleError} —— 下面还是最近一次成功加载的数据，不保证是现在。
            </div>
          )}
          {state.data.length === 0 ? (
            <div style={{ padding: 20, textAlign: 'center' }}>
              <div className="fs-11 text-muted">{emptyText}</div>
              {emptyHint && <div className="fs-10 text-muted" style={{ marginTop: 4 }}>{emptyHint}</div>}
            </div>
          ) : (
            children(state.data)
          )}
        </>
      )}
    </>
  )
}

// 依赖盘点（不新增依赖）：React 内置 + lib/toast 的 errText（现有依赖）+ 现有 css 类。
// lucide 图标都没用上 —— 四态全靠文字，图标留给调用方。
