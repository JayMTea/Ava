import { useEffect, useRef, useState } from 'react';
import type { MediaRef, Preview } from '../../lib/types';
import { Icon } from '../../lib/icons';
import { AppDot, appAccent, appForUrl } from '../../lib/appColor';

// Fullscreen viewer. Tap backdrop / X to close; pinch-to-zoom + drag-to-pan on
// touch, wheel-to-zoom + double-tap on desktop.
export function Lightbox({ url, onClose }: { url: string; onClose: () => void }) {
  const [t, setT] = useState({ scale: 1, x: 0, y: 0 });
  const g = useRef({
    mode: 'none' as 'none' | 'pan' | 'pinch',
    startDist: 0,
    startScale: 1,
    startX: 0,
    startY: 0,
    originX: 0,
    originY: 0,
    lastTap: 0,
    moved: false,
  });

  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Focus management for the modal. Without this, opening the lightbox leaves
  // focus on the thumbnail behind it — a keyboard user tabs through the page
  // underneath while a full-screen overlay covers it — and closing drops focus to
  // the document, losing their place in the conversation. Escape and the close
  // button already worked; this is the half that was missing.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => opener?.focus?.();
  }, []);

  const clampScale = (s: number) => Math.min(6, Math.max(1, s));
  // Reset pan when we drop back to fit-scale so the image re-centers.
  const settle = (next: { scale: number; x: number; y: number }) =>
    next.scale <= 1 ? { scale: 1, x: 0, y: 0 } : next;

  const onTouchStart = (e: React.TouchEvent) => {
    const c = g.current;
    if (e.touches.length === 2) {
      const [a, b] = [e.touches[0], e.touches[1]];
      c.mode = 'pinch';
      c.startDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1;
      c.startScale = t.scale;
      c.originX = (a.clientX + b.clientX) / 2;
      c.originY = (a.clientY + b.clientY) / 2;
      c.startX = t.x;
      c.startY = t.y;
      c.moved = true;
    } else if (e.touches.length === 1) {
      const touch = e.touches[0];
      c.mode = t.scale > 1 ? 'pan' : 'none';
      c.originX = touch.clientX;
      c.originY = touch.clientY;
      c.startX = t.x;
      c.startY = t.y;
      c.moved = false;
    }
  };

  const onTouchMove = (e: React.TouchEvent) => {
    const c = g.current;
    if (c.mode === 'pinch' && e.touches.length === 2) {
      const [a, b] = [e.touches[0], e.touches[1]];
      const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      const scale = clampScale(c.startScale * (dist / c.startDist));
      const midX = (a.clientX + b.clientX) / 2;
      const midY = (a.clientY + b.clientY) / 2;
      setT({ scale, x: c.startX + (midX - c.originX), y: c.startY + (midY - c.originY) });
    } else if (c.mode === 'pan' && e.touches.length === 1) {
      const touch = e.touches[0];
      c.moved = true;
      setT((prev) => ({ ...prev, x: c.startX + (touch.clientX - c.originX), y: c.startY + (touch.clientY - c.originY) }));
    }
  };

  const onTouchEnd = (e: React.TouchEvent) => {
    const c = g.current;
    // Double-tap toggles zoom (only when it was a clean tap, not a drag/pinch).
    if (c.mode === 'none' && !c.moved && e.changedTouches.length === 1) {
      const now = Date.now();
      if (now - c.lastTap < 300) {
        setT((prev) => (prev.scale > 1 ? { scale: 1, x: 0, y: 0 } : { scale: 2.5, x: 0, y: 0 }));
        c.lastTap = 0;
      } else {
        c.lastTap = now;
      }
    }
    setT(settle);
    if (e.touches.length === 0) c.mode = 'none';
  };

  // No cache-buster: a filename's bytes never change, so the browser can and
  // should cache the full image.
  const src = url.split('?')[0];
  const zoomed = t.scale > 1;
  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: backdrop-tap-to-close is a
    // redundant pointer shortcut. The keyboard paths are Escape (above) and the
    // labelled close button, which is focused on mount — so this handler adds no
    // capability a keyboard user lacks.
    <div
      id="lightbox"
      className="open"
      role="dialog"
      aria-modal="true"
      aria-label="Image viewer"
      onClick={(e) => {
        // Only close on a genuine backdrop tap (never mid-pan / when zoomed in).
        if (e.target === e.currentTarget && !zoomed) onClose();
      }}
      onWheel={(e) => setT((s) => settle({ ...s, scale: clampScale(s.scale - e.deltaY * 0.0025) }))}
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
    >
      <img
        alt=""
        src={src}
        draggable={false}
        onDoubleClick={() => setT((prev) => (prev.scale > 1 ? { scale: 1, x: 0, y: 0 } : { scale: 2.5, x: 0, y: 0 }))}
        style={{
          transform: `translate(${t.x}px, ${t.y}px) scale(${t.scale})`,
          cursor: zoomed ? 'grab' : 'zoom-in',
        }}
      />
      <button ref={closeRef} className="lb-close" type="button" aria-label="Close" onClick={onClose}>
        <Icon name="close" />
      </button>
    </div>
  );
}

// Agent/tool-produced media, resolved server-side to a same-origin URL
// (ava_bridge/agent_media.py). Renders the real element for its kind so the
// browser gets a native player — a <video> here supports HTTP Range against
// the /uploads mount, so it seeks. An image opens the same Lightbox as an
// upload; a file becomes a labelled download.
export function MediaCard({
  media,
  onOpen,
}: {
  media: MediaRef;
  onOpen?: (url: string) => void;
}) {
  const { url, kind } = media;
  const name = media.filename || decodeURIComponent(url.split('/').pop() || '') || 'file';
  const app = appForUrl(url);
  const label = app || media.filename ? (
    <div className="media-cap">
      {app && <AppDot accent={appAccent(app)} />}
      <span className="media-name">{name}</span>
      {app && <span className="tool-app">{app.label}</span>}
    </div>
  ) : null;
  return (
    <div className={`media-card kind-${kind}`}>
      {kind === 'video' ? (
        // eslint-disable-next-line jsx-a11y/media-has-caption
        <video className="media-el" controls preload="metadata" src={url} />
      ) : kind === 'audio' ? (
        // eslint-disable-next-line jsx-a11y/media-has-caption
        <audio className="media-el media-audio" controls preload="metadata" src={url} />
      ) : kind === 'image' ? (
        <button type="button" className="media-imgbtn" onClick={() => onOpen?.(url)}>
          <img className="media-el" loading="lazy" src={url} alt={name} />
        </button>
      ) : (
        <a
          className="media-file"
          href={url}
          target="_blank"
          rel="noreferrer noopener"
          download={name}
        >
          <Icon name="file" />
          <span>{name}</span>
        </a>
      )}
      {label}
    </div>
  );
}

export function PreviewCard({
  preview,
  onOpen,
  onQuickSay,
}: {
  preview: Preview;
  onOpen: (url: string) => void;
  onQuickSay: (t: string) => void;
}) {
  // Preview URLs arrive server-resolved (connector chat_pickup rewrites
  // app-relative paths through the same-origin /apps/<id> proxy).
  const disp = preview.url;
  const who = preview.persona ? ' ' + preview.persona : '';
  const cap = [preview.persona || 'preview', preview.theme, preview.seed != null ? 'seed ' + preview.seed : null]
    .filter(Boolean)
    .join(' · ');
  // Pickup cards come from a connected app (the /apps/<id> proxy URL says
  // which) — carry the app's identity accent so the artifact reads as the
  // app's work, not Ava's.
  const app = appForUrl(disp);
  return (
    <div className="media">
      <div className="imgbox">
        <img
          loading="lazy"
          alt={cap}
          src={disp}
          onClick={() => onOpen(disp)}
        />
      </div>
      <div className="cap">
        {app && <AppDot accent={appAccent(app)} />}
        {cap}
        {app && <span className="tool-app">{app.label}</span>}
      </div>
      <div className="imgactions">
        <button
          type="button"
          className="actbtn"
          onClick={() => onQuickSay(`Let’s iterate on that${who} look — adjust it and show me another preview.`)}
        >
          <Icon name="refresh" />
          <span>Iterate</span>
        </button>
        <button
          type="button"
          className="actbtn"
          onClick={() => onQuickSay(`Show me a few more preview variations of that${who} look.`)}
        >
          <Icon name="sparkles" />
          <span>More variations</span>
        </button>
      </div>
    </div>
  );
}
