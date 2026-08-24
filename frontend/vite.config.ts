import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// The SPA is served by the FastAPI bridge in production (static files under
// frontend/dist, mounted at "/"). In dev, Vite proxies the API/back-end paths to
// the running bridge on :8096 so the same-origin cookie auth keeps working.
const BRIDGE = process.env.AVA_BRIDGE || 'http://127.0.0.1:8096';
const proxy = Object.fromEntries(
  ['/api', '/apps', '/uploads', '/internal', '/login', '/logout'].map((p) => [
    p,
    { target: BRIDGE, changeOrigin: true },
  ]),
);

// PWA: installable app shell only. The service worker precaches the built
// bundle and serves navigations offline; every live surface (/api, /uploads,
// connector /apps) stays network-only so nothing stale is ever shown.
// Root-level artifacts (sw.js, manifest.webmanifest) are served by
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
    // favicon.svg is a glob hit (svg is listed above) and must not be. The
    // server route 404s it for a branded install so the browser falls back to
    // the branded .ico; a precached copy would answer from cache instead and
    // pin Ava's own mark on every installed branded client. This is the same
    // trap the webmanifest note below describes, reached by a different door —
    // .ico stays safe only because the pattern never matched it.
    // assets/brand/** is an OWNER's brand pack, gitignored at .gitignore:119.
    // Workbox globs the OUTPUT directory, so a maintainer who has one gets those
    // PNGs baked into dist/sw.js while a fresh checkout does not — and the build
    // artifact stops being a function of the tracked source. That is not a
    // theoretical drift: it is what the frontend-dist-drift job caught, and the
    // symptom is unreadable (a sw.js whose whole minified line differs) while the
    // cause is four files nobody can see in `git status`.
    //
    // A build must depend only on what is committed. Ignoring them here is also
    // correct on its own terms: brand assets are served by the bridge and change
    // without a rebuild, so a precached copy would pin a stale logo on every
    // installed client — the same argument favicon.svg is ignored for.
    globIgnores: ['favicon.svg', 'assets/brand/**'],
    navigateFallback: '/index.html',
    navigateFallbackDenylist: [
      /^\/api/, /^\/apps/, /^\/uploads/, /^\/internal/,
      /^\/login/, /^\/logout/, /^\/setup/, /^\/legacy/,
    ],
    // NOTE: the webmanifest is dropped from the precache by the companion
    // plugin below, NOT here. `manifestTransforms` only sees the manifest
    // workbox builds from globPatterns; the webmanifest arrives separately via
    // additionalManifestEntries and a transform never sees it. Measured: adding
    // a filter here left `grep -c manifest.webmanifest dist/sw.js` at 1.
  },
});

// Keep the webmanifest OUT of the service-worker precache.
//
// ava_bridge/pages.py:pwa_manifest() rewrites the manifest from config AT
// REQUEST TIME, precisely so a re-brand needs no rebuild. That was silently
// defeated for every INSTALLED client: vite-plugin-pwa pushes
// `{url: manifestFilename}` into workbox.additionalManifestEntries, so a
// controlling service worker served the BUILT manifest — the one that says
// "Ava" — and the server's branded version never reached the browser. The
// home-screen icon label is the single most visible place a wrong brand shows
// up, and the least expected to be wrong.
//
// It has to be done through the plugin's own extendManifestEntries API:
// `globIgnores` cannot reach it (globPatterns does not list .webmanifest, so it
// is not a glob hit), and `workbox.manifestTransforms` never sees it either —
// that only transforms the manifest workbox builds from globs, while this entry
// is concatenated separately. Both were tried and both left the entry in place.
// The plugin populates the entries during option resolution, so any later hook
// can filter them.
//
// Verified from the ARTIFACT, not the config: `grep -c manifest.webmanifest
// dist/sw.js` must be 0. tests/test_brand_pwa.py asserts it on every run.
const dropManifestFromPrecache = {
  name: 'ava:manifest-not-precached',
  apply: 'build' as const,
  buildStart() {
    const api = (pwa as any[]).find((p) => p?.api?.extendManifestEntries)?.api;
    api?.extendManifestEntries((entries: (string | { url: string })[]) =>
      entries.filter((e) => (typeof e === 'string' ? e : e.url) !== 'manifest.webmanifest'));
  },
};

export default defineConfig({
  plugins: [react(), pwa, dropManifestFromPrecache],
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
          // recharts is only reached through the lazy Vitals/Ops boundary, but
          // BOTH of them reach it — without a named chunk each carries its own
          // copy. xterm is the same story for the terminal panel.
          if (id.includes('node_modules/recharts') || id.includes('node_modules/d3')) return 'charts';
          if (id.includes('node_modules/@xterm')) return 'term';
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
