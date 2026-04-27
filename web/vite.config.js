import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'path'

// Backend address for the dev-server proxy. Override with
// ``VITE_API_BASE`` when running the backend on a non-default port:
//   VITE_API_BASE=http://localhost:18000 npm run dev
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backend = env.VITE_API_BASE || 'http://localhost:8000'
  return {
    plugins: [vue(), tailwindcss()],
    resolve: {
      alias: { '@': resolve(__dirname, 'src') },
    },
    server: {
      proxy: { '/api': backend },
    },
  }
})
