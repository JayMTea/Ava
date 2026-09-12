import { getTheme } from './theme';

export function sendAppTheme(frame: Window, origin: string): void {
  frame.postMessage({ type: 'ava:theme', theme: getTheme() }, origin);
}

/** Sync a kept-alive embed without navigating it or discarding its local state. */
export function connectAppTheme(frame: Window, origin: string): () => void {
  const send = () => sendAppTheme(frame, origin);
  const onRequest = (event: MessageEvent) => {
    if (event.source === frame && event.origin === origin
      && event.data?.type === 'ava:theme-request') send();
  };
  const observer = new MutationObserver(send);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  window.addEventListener('message', onRequest);
  send();
  return () => {
    observer.disconnect();
    window.removeEventListener('message', onRequest);
  };
}
