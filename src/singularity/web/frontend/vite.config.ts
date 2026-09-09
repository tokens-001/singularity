import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/static/dist/',
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:5050' } },
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
    // 拆分 vendor：antd/antd-x 体积大且很少变，单独成 chunk 便于浏览器缓存
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          antd: ['antd', '@ant-design/x'],
        },
      },
    },
  },
})
