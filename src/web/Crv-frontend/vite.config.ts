import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // FastAPI backend (Phase 3). Override target with a reverse proxy / same-origin
      // deployment in production (Phase 5) instead of relying on this dev-only proxy.
      '/chat': 'http://localhost:8000',
      '/conversations': 'http://localhost:8000',
      '/images': 'http://localhost:8000',
      '/documents': 'http://localhost:8000',
      '/media': 'http://localhost:8000',
      '/uploads': 'http://localhost:8000',
      '/feedback': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/healthz': 'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
