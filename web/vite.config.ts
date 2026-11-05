import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Built into app/static/next and served by FastAPI at /next/ while the classic page stays at /.
// In development the Python API runs on :8002 (the test port); Vite proxies the API to it.
const api = 'http://127.0.0.1:8002'
export default defineConfig({
  base: '/next/',
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  build: { outDir: '../app/static/next', emptyOutDir: true },
  server: { proxy: { '/api': api, '/login': api, '/callback': api, '/logout': api } },
})
