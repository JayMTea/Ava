import { registerSW } from 'virtual:pwa-register';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { api } from './lib/api';
import { revalidate } from './lib/brand';
import { BrandProvider } from './lib/brandContext';
import './styles/tokens.css';
import './styles/global.css';
import './styles/claude.css';
import './styles/dashboard.css';
import './styles/hub.css';
import './styles/data.css';

// Installable PWA: register the app-shell service worker (auto-updates on new
// deploys). Browsers only run service workers in a secure context, so this is
// a silent no-op on plain-HTTP LAN hosts — docs/MOBILE.md covers the HTTPS path.
registerSW({ immediate: true });

// Off the critical path on purpose. index.html already stamped the cached
// brand before first paint; this only corrects it if the server disagrees.
// A failure is silent — an offline or unauthenticated client keeps the cache,
// which beats reverting to Ava's blue because one request 401'd.
void revalidate(() => api.brand());

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrandProvider>
      <App />
    </BrandProvider>
  </StrictMode>,
);
