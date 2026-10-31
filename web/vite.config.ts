/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev: `npm run dev` serves the UI on 127.0.0.1:5173 and proxies /api to the Python server
// (`python -m jobhunt serve`, port 8000). Build: `npm run build` writes web/dist, which
// `python -m jobhunt serve` serves directly.
const backendPort = process.env.BACKEND_PORT || process.env.PORT || '8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1', // loopback only, never the network
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': { target: process.env.VITE_API_URL || `http://127.0.0.1:${backendPort}`, changeOrigin: false },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
