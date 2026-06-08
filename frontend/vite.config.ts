import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// 端口与代理目标支持环境变量覆盖：默认值固定（前端 3000 / 后端 8000），
// 但实际监听端口与后端地址对外可配置，避免与本机其它服务冲突。
//   FRONTEND_PORT         —— dev server 监听端口（默认 3000）
//   BACKEND_PROXY_TARGET  —— /api、/v1 反代的后端地址（默认 http://localhost:8000）
const frontendPort = Number(process.env.FRONTEND_PORT) || 3000
const backendTarget = process.env.BACKEND_PROXY_TARGET || 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: frontendPort,
    proxy: {
      '/api/': {
        target: backendTarget,
        changeOrigin: true,
      },
      '/v1': {
        target: backendTarget,
        changeOrigin: true,
      },
    },
  },
})
