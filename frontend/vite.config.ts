import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

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

// PWA: installable app shell only. The service worker precaches the built
// bundle and serves navigations offline; every live surface (/api, /media,
// /uploads, connector /apps) stays network-only so nothing stale is ever
// shown. Root-level artifacts (sw.js, manifest.webmanifest) are served by
// explicit bridge routes — see phone_bridge.py.
const pwa = VitePWA({
  registerType: 'autoUpdate',
  manifest: {
    name: 'Ava',
    short_name: 'Ava',
    description: 'Self-hosted AI assistant — governed cockpit for your agent.',
    start_url: '/',
    display: 'standalone',
    background_color: '#262624',
    theme_color: '#262624',
    icons: [
      { src: '/assets/icons/pwa-192.png', sizes: '192x192', type: 'image/png' },
      { src: '/assets/icons/pwa-512.png', sizes: '512x512', type: 'image/png' },
      { src: '/assets/icons/pwa-maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
  },
  workbox: {
    // Single-file worker: no separate workbox-*.js chunk for the bridge to serve.
    inlineWorkboxRuntime: true,
    globPatterns: ['**/*.{js,css,html,png,svg,woff2}'],
    navigateFallback: '/index.html',
    navigateFallbackDenylist: [
      /^\/api/, /^\/apps/, /^\/media/, /^\/uploads/, /^\/internal/,
      /^\/login/, /^\/logout/, /^\/setup/, /^\/legacy/,
    ],
  },
});

export default defineConfig({
  plugins: [react(), pwa],
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
