import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The SPA is served by the FastAPI bridge in production (static files under
// frontend/dist, mounted at "/"). In dev, Vite proxies the API/back-end paths to
// the running bridge on :8096 so the same-origin cookie auth keeps working.
const BRIDGE = process.env.AVA_BRIDGE || 'http://127.0.0.1:8096';
const proxy = Object.fromEntries(
  ['/api', '/apps', '/media', '/uploads', '/internal', '/login', '/logout'].map((p) => [
    p,
    { target: BRIDGE, changeOrigin: true },
  ]),
);

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      // Split the React runtime into its own chunk so the main bundle stays
      // lean and browser-cacheable across app updates.
      output: {
        manualChunks(id: string) {
          if (id.includes('node_modules/react')) return 'react';
        },
      },
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy,
  },
});
