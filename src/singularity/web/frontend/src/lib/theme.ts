import type { ThemeConfig } from 'antd'

// ⚠️ 必须和 index.css 的 `--font-sans` 一字不差 —— 那是同一份字体栈的两个出口
// （CSS 变量供手写样式用，这个常量供 antd 用）。改一个就得改另一个。
// 这里原来是把整串字体栈直接写死在 token 里的，和 CSS 各一份，没有对照关系。
export const FONT_SANS =
  "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Segoe UI', sans-serif"

/**
 * antd theme —— 与 index.css 的 :root design token 同源。
 * 改配色时两处一起改（CSS 变量供手写业务展示用，这里供 antd 组件用）。
 * 注意：colorPrimaryHover/Active 等派生色由 seed 自动算出，不等于手写色值。
 */
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: '#2563eb',
    colorLink: '#2563eb',
    colorSuccess: '#16a34a',
    colorError: '#dc2626',
    colorWarning: '#b45309',
    colorTextBase: '#141413',
    colorBgBase: '#faf9f5',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBgLayout: '#faf9f5',
    colorBorder: '#e5e2d8',
    colorBorderSecondary: '#e5e2d8',
    borderRadius: 8,
    // 奇点是 10-13px 的密集界面，antd 默认 14px/32px 会显得"胖"
    fontSize: 13,
    controlHeight: 28,
    fontFamily: FONT_SANS,
  },
  components: {
    Tabs: { itemSelectedColor: '#2563eb', inkBarColor: '#2563eb', titleFontSize: 13 },
  },
}
