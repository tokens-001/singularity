import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    // ⚠️ **样板目录不参与默认测试收集**（2026-09-14）：`src/pages/__rewrite_sample__/`
    // 是外派窗口写的**审阅用样板**（不是发布代码，同 `tsconfig.exclude` 的理由），
    // 而它自带测试、且**正在被那个窗口迭代** —— 一个还在写的东西挂进默认收集里，
    // 会让"前端全绿"这个信号变成假的（实测：它一条在挂，`vitest run` 就报 1 failed）。
    // ⚠️ **想跑它的测试得先把这行 `exclude` 注释掉**（实测 `--exclude` 覆盖不管用、
    // 指名文件也找不到）。注释掉之后：`npx vitest run src/pages/__rewrite_sample__`
    // —— 实测那一刻它是 **6 条挂**（那个窗口的活还没干完），所以更不能进默认收集。
    exclude: ['**/node_modules/**', '**/dist/**', 'src/pages/__rewrite_sample__/**'],
  },
  base: '/static/dist/',
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:5050' } },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
    // 只固定 react：antd 不写进 manualChunks，交给 Rollup 按动态 import 自动切，
    // 否则 antd 全量被打进一个 chunk，路由级 lazy 就白做了。
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
})
