import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
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
