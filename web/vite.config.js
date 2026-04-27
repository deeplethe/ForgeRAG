import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'path'

// Backend address for the dev-server proxy. Override with
// ``VITE_DEV_PROXY`` when running the backend on a non-default port:
//   VITE_DEV_PROXY=http://localhost:18000 npm run dev
//
// IMPORTANT: keep this distinct from ``VITE_API_BASE`` (which the
// browser-side client.js reads). Setting ``VITE_API_BASE`` to a
// concrete URL makes the frontend bypass the proxy and send absolute
// cross-origin requests, which trips a CORS wildcard-vs-credentials
// error. Dev should leave ``VITE_API_BASE`` empty so requests are
// relative (``/api/v1/...``) and resolved through this proxy.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backend = env.VITE_DEV_PROXY || 'http://localhost:8000'
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
