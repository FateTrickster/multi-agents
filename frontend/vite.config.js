import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Normally use the local FastAPI development server. An explicitly configured
// remote backend may be used for isolated UI previews without touching V1.
const backendPort = process.env.VITE_BACKEND_PORT || '8001'
const backendUrl = process.env.VITE_BACKEND_URL || `http://127.0.0.1:${backendPort}`
const backendHttp = new URL(backendUrl)
const backendWs = new URL(backendUrl)
backendWs.protocol = backendHttp.protocol === 'https:' ? 'wss:' : 'ws:'
let backendUnavailableWarned = false

function configureProxy(proxy, label) {
  proxy.on('error', (err) => {
    if (err?.code === 'ECONNREFUSED') {
      if (!backendUnavailableWarned) {
        backendUnavailableWarned = true
        console.warn(`[vite] backend unavailable (${label} -> ${backendHttp.host}); waiting for backend startup...`)
      }
      return
    }
    console.error(`[vite] ${label} proxy error:`, err)
  })

  proxy.on('proxyRes', () => {
    backendUnavailableWarned = false
  })
}

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.spec.js'],
  },
  server: {
    proxy: {
      '/api': {
        target: backendHttp.origin,
        changeOrigin: true,
        configure(proxy) {
          configureProxy(proxy, 'http')
        },
      },
      '/ws': {
        target: backendWs.origin,
        ws: true,
        changeOrigin: true,
        configure(proxy) {
          configureProxy(proxy, 'ws')
        },
      },
    },
  },
})
