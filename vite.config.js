import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  root: 'frontend',
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    proxy: {
      '/api': 'http://127.0.0.1:8890',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        app: resolve(import.meta.dirname, 'frontend/index.html'),
      },
    },
  },
});
