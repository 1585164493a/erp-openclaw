import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,  // 前端开发服务器端口
    proxy: {
      // 将 /api 请求代理到后端
      '/api': {
        target: 'http://127.0.0.1:8091',  // 后端地址（8090 被一个无法回收的僵尸 socket 占着，故改 8091）
        changeOrigin: true,
        // 不需要 rewrite，因为后端路由已经带 /api 前缀
      }
    }
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets'
  }
})
