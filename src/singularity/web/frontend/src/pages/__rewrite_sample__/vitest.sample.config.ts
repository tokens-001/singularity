/**
 * 样板目录专用的 vitest 配置 —— 解决"根配置 exclude 了样板目录、又不想为跑样板
 * 去改根配置"的矛盾（根 vite.config.ts 的注释写的办法是注释掉那行 exclude，
 * 那要动一个现有文件；这个文件让你一行都不用动）。
 *
 * 用法（在 frontend/ 目录下）：
 *   npx vitest run --config src/pages/__rewrite_sample__/vitest.sample.config.ts
 *
 * 根配置里的 exclude 指向本目录（理由见那条提交：样板不是发布代码，
 * 不该混进"前端全绿"的默认信号）—— 本配置只覆盖收集范围，其余全默认。
 */
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    // ⚠️ `root` **不是**配置文件所在目录，仍是 `process.cwd()`（= frontend/），
    // 所以这条命令实际收的是**全量**：14 文件 / 92 用例 = 默认的 71 + 样板的 21。
    // （写这份配置时的注释说"只收本目录自己的测试"，是错的 —— 2026-09-14 复核时实测更正。）
    // 只想跑样板：`npx vitest run --config <本文件> __rewrite_sample__`
    include: ['**/*.test.{ts,tsx}'],
    exclude: ['**/node_modules/**', '**/dist/**'],
  },
})
