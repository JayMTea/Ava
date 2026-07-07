import { useEffect, useRef, useState } from 'react';
import type { Preview } from '../../lib/types';
import { Icon } from '../../lib/icons';
import { api } from '../../lib/api';
import { ProgressBar } from '../../lib/ProgressBar';

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

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

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

  const src = url.split('?')[0] + '?t=' + Date.now();
  const zoomed = t.scale > 1;
  return (
    <div
      id="lightbox"
      className="open"
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
      <button className="lb-close" type="button" aria-label="Close" onClick={onClose}>
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
  const [dim, setDim] = useState('');
  const [upscaling, setUpscaling] = useState(false);
  const [displayUrl, setDisplayUrl] = useState(url);
  const timer = useRef<ReturnType<typeof setTimeout>>();

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

  const upscale = async () => {
    setUpscaling(true);
    try {
      const j = await api.upscale(displayUrl.split('?')[0].split('/').pop() || '');
      if (j.url) {
        setDisplayUrl(j.url);
        if (chatId) api.attachImageToChat(chatId, j.url, caption || '').catch(() => {});
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
          src={displayUrl + (displayUrl.indexOf('?') < 0 ? '?t=' : '&t=') + Date.now()}
          loading="lazy"
          alt={caption || ''}
          onClick={open}
          onLoad={(e) => {
            const im = e.currentTarget;
            if (im.naturalWidth) setDim(`${im.naturalWidth}\u00d7${im.naturalHeight}`);
          }}
        />
        {dim && <div className="dim">{dim}</div>}
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

export function GenProgress({
  progress,
  status,
  error,
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
  return (
    <div className="media">
      <div className="imgbox">
        <img
          loading="lazy"
          alt={cap}
          src={disp + (disp.indexOf('?') < 0 ? '?t=' : '&t=') + Date.now()}
          onClick={() => onOpen(disp)}
        />
      </div>
      <div className="cap">{cap}</div>
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
