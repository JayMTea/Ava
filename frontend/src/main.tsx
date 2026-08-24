import { registerSW } from 'virtual:pwa-register';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { GatewayProvider } from './hooks/useGateway';
import { api } from './lib/api';
import { revalidate } from './lib/brand';
import { BrandProvider } from './lib/brandContext';
// @font-face first: tokens.css names the family, this declares where it lives.
import './styles/fonts.css';
import './styles/tokens.css';
import './styles/global.css';
import './styles/claude.css';
import './styles/dashboard.css';
import './styles/hub.css';
import './styles/agent.css';
import './styles/data.css';
import './styles/tour.css';
import './styles/hwbubble.css';

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
      {/* Above the router on purpose: the socket must survive a view switch.
          Mounting it inside the Agent view would re-handshake and drop every
          subscription each time somebody looked at Vitals — and `useChat` needs
          it from the Chats tab, which is a different view entirely. */}
      <GatewayProvider>
        <App />
      </GatewayProvider>
    </BrandProvider>
  </StrictMode>,
);
