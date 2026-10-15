/// <reference types="vitest/config" />
import { svelte } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

const backend = process.env.BACKEND_URL ?? 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: '../backend/static',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: false },
      '/healthz': backend,
    },
  },
  test: {
    include: ['src/**/*.test.ts'],
  },
})
