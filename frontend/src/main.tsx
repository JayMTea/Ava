import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { registerSW } from 'virtual:pwa-register';
import App from './App';
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

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
