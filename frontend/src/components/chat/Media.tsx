import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { Preview } from '../../lib/types';
import { Icon } from '../../lib/icons';
import { api } from '../../lib/api';
import { AppDot, appAccent, appForUrl } from '../../lib/appColor';
import { ProgressBar } from '../../lib/ProgressBar';
import { fixForCode, type FixAction } from '../../lib/fixes';

const BLUR_DELAY_MS = 30000;

// Fullscreen viewer. Tap backdrop / X to close; pinch-to-zoom + drag-to-pan on
// touch, wheel-to-zoom + double-tap on desktop. Optional `info` renders a
// collapsible details overlay (e.g. the models + prompt used for the image).
export function Lightbox({ url, onClose, info }: { url: string; onClose: () => void; info?: React.ReactNode }) {
  const [t, setT] = useState({ scale: 1, x: 0, y: 0 });
  const [showInfo, setShowInfo] = useState(false);
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

  // No cache-buster: generated images are immutable (a filename's bytes never
  // change), so the browser can and should cache the full image.
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
      {info && (
        <button
          className={'lb-info-btn' + (showInfo ? ' on' : '')}
          type="button"
          aria-label="Image details"
          onClick={(e) => {
            e.stopPropagation();
            setShowInfo((v) => !v);
          }}
        >
          <Icon name="sparkles" />
        </button>
      )}
      {info && showInfo && (
        <div className="lb-info" onClick={(e) => e.stopPropagation()}>
          {info}
        </div>
      )}
    </div>
  );
}

export function ImageMessage({
  url,
  caption,
  allowUpscale = true,
  chatId,
  onOpen,
}: {
  url: string;
  caption?: string;
  allowUpscale?: boolean;
  chatId?: string | null;
  onOpen: (url: string, onClose: () => void) => void;
}) {
  const [blurred, setBlurred] = useState(false);
  const [upscaling, setUpscaling] = useState(false);
  const [displayUrl, setDisplayUrl] = useState(url);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const arm = () => {
    setBlurred(false);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setBlurred(true), BLUR_DELAY_MS);
  };
  useEffect(() => {
    arm();
    return () => clearTimeout(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const open = () => onOpen(displayUrl, arm);
  // Inline bubbles load a small cached WebP thumbnail (backend /thumb route),
  // not the full 16 MB 4K PNG; the lightbox (open()) still opens the full image.
  const thumbUrl = displayUrl.startsWith('/media/')
    ? '/thumb/' + displayUrl.slice('/media/'.length)
    : displayUrl;

  const upscale = async () => {
    setUpscaling(true);
    try {
      // /api/upscale is an async job: poll it until the 4K render lands. The
      // bridge persists the result to the chat itself; we only update the view.
      const start = await api.upscale(displayUrl.split('?')[0].split('/').pop() || '', chatId || undefined, caption || '');
      const jobId = start.job?.id;
      const deadline = Date.now() + 5 * 60 * 1000;
      while (jobId && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1000));
        const j = await api.job(jobId);
        if (j.status === 'done' && j.url) {
          setDisplayUrl(j.url);
          break;
        }
        if (j.status === 'error') break;
      }
    } catch {
      /* ignore */
    }
    setUpscaling(false);
  };

  return (
    <div className="media">
      <div className={'imgbox' + (blurred ? ' blurred' : '')}>
        <img
          src={thumbUrl}
          loading="lazy"
          alt={caption || ''}
          onClick={open}
        />
        <div className="privacy" onClick={open}>
          <Icon name="eyeOff" />
          <span>Tap to view</span>
        </div>
      </div>
      {caption && <div className="cap">{caption}</div>}
      {allowUpscale && (
        <div className="imgtools">
          <button className="upbtn" type="button" disabled={upscaling} onClick={upscale}>
            <Icon name="expand" />
            <span>{upscaling ? 'upscaling to 4K…' : 'Upscale to 4K'}</span>
          </button>
        </div>
      )}
    </div>
  );
}

// The fix-it link: click navigates, hover/focus shows a popover saying where
// it leads (reuses the dashboard's .info-pop, portalled to <body> so the chat
// bubble can't clip it). Fixes are derived from the error code's PATTERN by
// lib/fixes.ts, so any newly registered capability gets this automatically.
export function FixLink({ fix }: { fix: FixAction }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const ref = useRef<HTMLAnchorElement>(null);
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (r) setPos({ x: r.left + r.width / 2, y: r.top });
    setOpen(true);
  };
  return (
    <>
      <a
        ref={ref}
        className="gen-fix"
        href={`#${fix.hash}`}
        onMouseEnter={show}
        onMouseLeave={() => setOpen(false)}
        onFocus={show}
        onBlur={() => setOpen(false)}
      >
        {fix.label} →
      </a>
      {open && createPortal(
        <div className="info-pop" role="tooltip" style={{ left: pos.x, top: pos.y }}>{fix.tip}</div>,
        document.body,
      )}
    </>
  );
}

export function GenProgress({
  progress,
  status,
  error,
  errorCode,
  prompt,
  stage,
  elapsedSec,
  queueHint,
  cancelable,
  onCancel,
}: {
  progress: number;
  status: 'running' | 'done' | 'error';
  error?: string;
  errorCode?: string;
  prompt?: string;
  stage?: string;
  elapsedSec?: number;
  queueHint?: string;
  cancelable?: boolean;
  onCancel?: () => void;
}) {
  const pct = Math.max(0, Math.min(100, Math.round(progress || 0)));
  const stageLabel = stage || (pct <= 0 ? 'queued' : pct >= 75 ? 'upscaling' : 'rendering');
  const elapsed = elapsedSec != null ? `${Math.max(0, Math.round(elapsedSec))}s` : null;
  const fix = status === 'error' ? fixForCode(errorCode) : undefined;
  return (
    <div className="gen">
      <div className="lab">
        <Icon name={status === 'error' ? 'alert' : 'image'} />
        <span>
          {status === 'error'
            ? `generation failed: ${error || 'unknown'}`
            : `generating image… ${pct}%`}
        </span>
      </div>
      {fix && <div className="gen-fixrow"><FixLink fix={fix} /></div>}
      {status === 'running' && (
        <div className="gen-meta">
          <span>{stageLabel}</span>
          {queueHint && <span>{queueHint}</span>}
          {elapsed && <span>elapsed {elapsed}</span>}
          {cancelable && onCancel && (
            <button type="button" className="gen-cancel" onClick={onCancel}>
              Cancel
            </button>
          )}
        </div>
      )}
      {prompt && (
        <details className="gen-prompt">
          <summary>Prompt details</summary>
          <div>{prompt}</div>
        </details>
      )}
      <ProgressBar progress={progress} error={status === 'error'} />
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
