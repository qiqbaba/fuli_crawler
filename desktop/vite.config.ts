import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // ECharts 已按需注册并由 React.lazy 拆成独立 chunk，仅在打开统计大盘时加载
    chunkSizeWarningLimit: 600,
  },
  server: {
    // 5173 落在 Windows 保留端口段 5109-5208，监听会 EACCES；改用 4173
    host: '127.0.0.1',
    port: 4173,
    strictPort: false,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
