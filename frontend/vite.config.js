import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backendPort = process.env.VITE_BACKEND_PORT || '8001'
let backendUnavailableWarned = false

function configureProxy(proxy, label) {
  proxy.on('error', (err) => {
    if (err?.code === 'ECONNREFUSED') {
      if (!backendUnavailableWarned) {
        backendUnavailableWarned = true
        console.warn(
          `[vite] backend not ready yet (${label} -> 127.0.0.1:${backendPort}); waiting for backend startup...`
        )
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
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
        configure(proxy) {
          configureProxy(proxy, 'http')
        },
      },
      '/ws': {
        target: `ws://127.0.0.1:${backendPort}`,
        ws: true,
        changeOrigin: true,
        configure(proxy) {
          configureProxy(proxy, 'ws')
        },
      },
    },
  },
})
