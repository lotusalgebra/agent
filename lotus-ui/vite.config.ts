import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  base: '/lotus-ui/dist/',
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api':      { target: 'http://localhost:8766', changeOrigin: true },
      '/projects': { target: 'http://localhost:8766', changeOrigin: true },
    },
  },
})
