import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { connectAppTheme } from './embedTheme';

let theme: string;
let onMutation: () => void;
let events: EventTarget;
let cleanup: () => void;
const postMessage = vi.fn();
const disconnect = vi.fn();
const frame = { postMessage } as unknown as Window;

beforeEach(() => {
  theme = 'light';
  events = new EventTarget();
  postMessage.mockClear();
  disconnect.mockClear();
  vi.stubGlobal('window', events);
  vi.stubGlobal('document', { documentElement: { getAttribute: () => theme } });
  vi.stubGlobal('MutationObserver', class {
    constructor(callback: () => void) { onMutation = callback; }
    observe() {}
    disconnect = disconnect;
  });
  cleanup = connectAppTheme(frame, 'https://apps.example');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function request(source: unknown = frame, origin = 'https://apps.example', type = 'ava:theme-request') {
  events.dispatchEvent(Object.assign(new Event('message'), { source, origin, data: { type } }));
}

describe('embed theme synchronization', () => {
  it('sends the current theme to the exact app origin at connection and on theme changes', () => {
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'ava:theme', theme: 'light' }, 'https://apps.example');
    theme = 'dark';
    onMutation();
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'ava:theme', theme: 'dark' }, 'https://apps.example');
  });

  it('answers a late ready request with the latest theme', () => {
    theme = 'dark';
    request();
    expect(postMessage).toHaveBeenLastCalledWith({ type: 'ava:theme', theme: 'dark' }, 'https://apps.example');
  });

  it('ignores requests from other windows, other origins, and other message types', () => {
    postMessage.mockClear();
    request({});
    request(frame, 'https://unrelated.example');
    request(frame, 'https://apps.example', 'unrelated');
    expect(postMessage).not.toHaveBeenCalled();
  });

  it('disconnects the observer and request listener on unmount', () => {
    cleanup();
    postMessage.mockClear();
    request();
    expect(postMessage).not.toHaveBeenCalled();
    expect(disconnect).toHaveBeenCalled();
  });
});
