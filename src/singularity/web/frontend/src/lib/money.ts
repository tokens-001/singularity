/**
 * money.ts — 金额显示的唯一出口。
 *
 * 存在的理由：后端在**没配单价**时会返回 `null`，而 `(x || 0).toFixed(2)` 会把它
 * 渲染成一个看着可信的 `$0.00` —— 那就是在编造金额。这类"缺失值伪装成 0"的写法
 * 曾经散在三处，本模块是它们唯一的替代：**缺失一律显式显示"未配置价格"**。
 *
 * 别在这里加默认值/兜底价。后端 `model_prices.price_for()` 查不到就返回 None，
 * 前端如实转述即可。
 */

const UNPRICED = '未配置价格'

/** 金额（美元）。null / undefined → "未配置价格"。 */
export function fmtCost(c: number | null | undefined): string {
  if (c === null || c === undefined || !Number.isFinite(c)) return UNPRICED
  // 单模型日费用常在亚分位：保留 4 位，否则 $0.0031 会显示成 $0.00 —— 又变成谎言
  return '$' + c.toFixed(4)
}

/** 单价的单位。写全，不用 '/M' —— 那个缩写对不熟的人就是天书（实测被问过"m 是啥意思"）。 */
export const PRICE_UNIT = '/百万token'

/** 单价数值（**不含单位**）。表格里单位写在表头 —— 逐格重复既撑宽列、也是噪声。 */
export function fmtPriceValue(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return UNPRICED
  return '$' + p.toFixed(2)
}

/** 单价（数值 + 单位）。**没有表头的单值场景**用（如模型卡片上的单价入口）。 */
export function fmtPrice(p: number | null | undefined): string {
  return fmtPriceValue(p) === UNPRICED ? UNPRICED : fmtPriceValue(p) + PRICE_UNIT
}

/** 是否未配置（供调用方决定要不要加警示色）。 */
export function isUnpriced(v: number | null | undefined): boolean {
  return v === null || v === undefined || !Number.isFinite(v)
}

export const UNPRICED_LABEL = UNPRICED
