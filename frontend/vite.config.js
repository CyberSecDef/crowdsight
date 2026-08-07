import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/* In production the gateway serves the built bundle and proxies /api on the
   same origin, so the app never needs a backend host. `npm run dev` has no
   gateway in front of it, so it proxies /api itself — to the backend's direct
   :5000 listener, which exists for exactly this kind of tooling. Same-origin
   in both cases, which keeps CORS out of the project entirely. */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.CROWDSIGHT_API || 'http://127.0.0.1:5000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // Nothing is fetched from a CDN, so the whole bundle is local anyway.
    // Splitting the vendor chunk keeps the app chunk small on rebuilds.
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['vue', 'vue-router', 'pinia'],
        },
      },
    },
  },
})
