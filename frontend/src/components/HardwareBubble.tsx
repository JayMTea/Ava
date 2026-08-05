import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { AllocSection } from './AllocSection';
import { api } from '../lib/api';
import { ProgressBar } from '../lib/ProgressBar';
import { stateCopy, stateOf } from '../lib/modelState';
import type { StatefulRow } from '../lib/modelState';
import type { HardwareStats } from '../lib/types';

// Floating, draggable hardware monitor. Tap to expand a live quick-view of the
// DGX Spark (GPU %, unified memory used/free, CPU, temperature); drag the bubble
// anywhere so it never blocks the UI. Position is remembered across sessions.
//
// The panel's LOOK lives in styles/hwbubble.css; what stays here is everything
// that depends on where the bubble was dropped — the anchor offsets and the two
// bounds below.

const KEY = 'ava.hwbubble.pos';
const SIZE = 52;
// GAP is bubble→panel, EDGE is panel→viewport edge. The panel stops shrinking at
// MIN_W because a 40px-wide panel is not a smaller panel, it is an unreadable
// one. MAX_W is the width at which two columns each get the ~210px the single
// column always had (2×210 + 26 padding + 12 gap + 12 column padding + 1
// divider); past that the lines get too long to scan. Under 430px the container
// query in styles/hwbubble.css stacks the columns again.
const GAP = 8;
const EDGE = 8;
const MIN_W = 236;
const MAX_W = 470;

type Pos = { x: number; y: number };

function vw() { return typeof window !== 'undefined' ? window.innerWidth : 800; }
function vh() { return typeof window !== 'undefined' ? window.innerHeight : 800; }

function clampPos(p: Pos): Pos {
  return { x: Math.max(4, Math.min(p.x, vw() - SIZE - 4)), y: Math.max(4, Math.min(p.y, vh() - SIZE - 4)) };
}

function loadPos(): Pos {
  try {
    const p = JSON.parse(localStorage.getItem(KEY) || 'null');
    if (p && typeof p.x === 'number' && typeof p.y === 'number') return clampPos(p);
  } catch {
    /* ignore */
  }
  return { x: vw() - SIZE - 16, y: vh() - SIZE - 96 };
}

const gb = (v: number | null | undefined) => (v == null ? '—' : v >= 1024 ? `${(v / 1024).toFixed(1)} TB` : `${v.toFixed(1)} GB`);
const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v)}%`);
const temp = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v)}°C`);

function tempColor(t: number | null | undefined): string {
  if (t == null) return 'inherit';
  if (t >= 85) return '#e0364d';
  if (t >= 70) return '#e0a06f';
  return '#7fd0a0';
}

function ChipGlyph({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="6" y="6" width="12" height="12" rx="2" />
      <path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" />
    </svg>
  );
}

// Liveness wording comes from lib/modelState — the one place it is defined, so
// this panel and Setup → Agent cannot drift into describing the same reading in
// two different languages (which is exactly how this panel ended up saying "No
// model process detected yet" about a model Setup was showing as the brain).
type Row = StatefulRow & { gpu_util?: number | null; role_key?: string };

// The dot follows the STATE, and only shades it by GPU busyness when that is
// actually known. Keying it off gpu_util first painted a grey "idle" dot beside
// a green "In memory" — two contradictory readings of one row, and grey on
// every CPU-only box, where per-process GPU util is never available.
const activityDotStyle = (m: Row): CSSProperties => {
  const copy = stateCopy(m);
  const u = m.gpu_util;
  const color = stateOf(m) === 'resident' && u != null && u >= 30 ? '#34d27a'
    : stateOf(m) === 'resident' && u != null && u > 0 ? '#e6b85c'
      : copy.tone;
  return {
    width: 7,
    height: 7,
    borderRadius: '50%',
    display: 'inline-block',
    background: color,
    boxShadow: `0 0 0 1px rgba(0,0,0,.28), 0 0 6px ${color}`,
    marginRight: 6,
    verticalAlign: 'middle',
  };
};

function Metric({ label, value, progress, sub }: { label: string; value: string; progress: number | null; sub?: string }) {
  return (
    <div style={{ marginBottom: 9 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ color: 'var(--muted)' }}>{label}</span>
        <span style={{ fontWeight: 700 }}>{value}</span>
      </div>
      <ProgressBar progress={progress ?? 0} indeterminateAtZero={false} />
      {sub && <div style={{ color: 'var(--muted)', fontSize: 11, marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

export function HardwareBubble() {
  const [pos, setPos] = useState<Pos>(loadPos);
  const [vp, setVp] = useState(() => ({ w: vw(), h: vh() }));
  const [open, setOpen] = useState(false);
  const [stats, setStats] = useState<HardwareStats | null>(null);
  const [selectedModelId, setSelectedModelId] = useState('');
  const drag = useRef<{ sx: number; sy: number; ox: number; oy: number; moved: boolean } | null>(null);

  useEffect(() => {
    localStorage.setItem(KEY, JSON.stringify(pos));
  }, [pos]);

  // The viewport is STATE because the panel's width and max-height are functions
  // of it. This re-rendered on resize before only because clampPos returns a
  // fresh object on every call, so setPos always changed identity — an accident
  // that would have gone quiet the moment anyone made clampPos return `p`
  // unchanged, leaving the panel sized for the window it was opened in.
  useEffect(() => {
    const onResize = () => {
      setVp({ w: vw(), h: vh() });
      setPos((p) => clampPos(p));
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await api.hardware();
        if (alive) setStats(s);
      } catch {
        /* ignore transient errors */
      }
    };
    tick();
    // Poll a little faster while the panel is open, slower when it's just a bubble.
    const id = window.setInterval(tick, open ? 2000 : 5000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [open]);

  const onPointerDown = useCallback(
    (e: ReactPointerEvent) => {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      drag.current = { sx: e.clientX, sy: e.clientY, ox: pos.x, oy: pos.y, moved: false };
    },
    [pos],
  );

  const onPointerMove = useCallback((e: ReactPointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (!d.moved && Math.hypot(dx, dy) > 5) d.moved = true;
    if (d.moved) setPos(clampPos({ x: d.ox + dx, y: d.oy + dy }));
  }, []);

  const onPointerUp = useCallback((e: ReactPointerEvent) => {
    const d = drag.current;
    drag.current = null;
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    if (d && !d.moved) setOpen((o) => !o);
  }, []);

  // The panel opens away from whichever edge the bubble is nearest, so the space
  // it gets is always the larger side — but it is still finite, and the two
  // bounds below are what stop it running off-screen. It had NEITHER: no width
  // clamp (harmless while it was a fixed 236px, not once it can reach two
  // columns) and no height bound at all, which is why a tall panel was simply
  // cut off by the bottom of the window.
  const rightHalf = pos.x + SIZE / 2 > vp.w / 2;
  const bottomHalf = pos.y + SIZE / 2 > vp.h / 2;
  // Height is bounded by the space on the side it grows into, NOT by the
  // viewport: a bubble parked mid-screen gets half a window, and `100dvh` would
  // have let the panel overflow exactly there.
  const availH = (bottomHalf ? pos.y + SIZE : vp.h - pos.y) - EDGE;
  const availW = (rightHalf ? pos.x : vp.w - pos.x - SIZE) - GAP - EDGE;
  const panelStyle: CSSProperties = {
    // Read as: never wider than two readable columns, never wider than the
    // window, and never narrower than MIN_W unless the window itself is.
    width: Math.min(MAX_W, Math.max(MIN_W, availW), vp.w - 2 * EDGE),
    maxHeight: availH,
    ...(rightHalf ? { right: vp.w - pos.x + GAP } : { left: pos.x + SIZE + GAP }),
    ...(bottomHalf ? { bottom: vp.h - (pos.y + SIZE) } : { top: pos.y }),
  };

  const gpu = stats?.gpu;
  const mem = stats?.mem;
  const disk = stats?.disk;
  const cpu = stats?.cpu;
  const models = stats?.models || [];
  // The backend already sorts the brain first; being explicit here means the
  // panel opens on what Ava thinks with even if that ever changes.
  const brain = models.find((m) => m.role_key === 'brain') || null;
  const selectedModel = models.find((m) => m.id === selectedModelId) || brain || models[0] || null;

  useEffect(() => {
    if (!models.length) {
      setSelectedModelId('');
      return;
    }
    if (!models.some((m) => m.id === selectedModelId)) {
      setSelectedModelId(models[0].id);
    }
  }, [models, selectedModelId]);

  return (
    <>
      {open && (
        <div className="hwb-panel" style={panelStyle}>
          <div className="hwb-head">
            <ChipGlyph />
            <span>{gpu?.name || 'Compute'}</span>
          </div>
          <div className="hwb-cols">
            {/* Left column: the machine. */}
            <div className="hwb-col">
              <Metric label="GPU util" value={pct(gpu?.util)} progress={gpu?.util ?? 0} />
              <Metric
                label={`Memory · ${gb(mem?.used_gb)} / ${gb(mem?.total_gb)}`}
                value={pct(mem?.used_pct)}
                progress={mem?.used_pct ?? 0}
                sub={`${gb(mem?.free_gb)} free`}
              />
              {/* The caveat is the frontend's to word (CLAUDE.md): the backend
                  says `capped`, this says what that means for the person
                  deciding whether a model will fit. Without it the panel read
                  "928.5 GB free" on a laptop whose drive had 156 GB. */}
              <Metric
                label={`Disk · ${gb(disk?.used_gb)} / ${gb(disk?.total_gb)}`}
                value={pct(disk?.used_pct)}
                progress={disk?.used_pct ?? 0}
                sub={disk?.capped
                  ? `${gb(disk?.free_gb)} free on Ava's volume — it is a virtual disk, so the drive behind it may have less`
                  : `${gb(disk?.free_gb)} free`}
              />
              <Metric label="CPU util" value={pct(cpu?.util)} progress={cpu?.util ?? 0} />
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8 }}>
                <span style={{ color: 'var(--muted)' }}>GPU temp</span>
                <span style={{ fontWeight: 700, color: tempColor(gpu?.temp) }}>{temp(gpu?.temp)}</span>
              </div>
              {/* Freeing memory belongs in the MACHINE column, and not only for
                  balance: this column is what the machine is holding, which is
                  exactly the question the section answers. It sat on the right,
                  where the brain and the model inspector already lived — 176px of
                  content on the left against 708px on the right, in a panel with
                  ~790px to spend. It read as one long list because it was one. */}
              <AllocSection />
            </div>
            {/* Right column: the models. Cutting here rather than anywhere else
                is what keeps a subject whole — the panel already drew its own
                dividers in these two places. */}
            <div className="hwb-col">
              {/* Ava's brain, always — it is named by configuration, so it is
                  knowable even when nothing is running. This block used to be
                  absent entirely on a machine with no GPU, which told a user whose
                  model was serving fine that no model existed. */}
              <div className="hwb-sec">
                <div style={{ color: 'var(--muted)', marginBottom: 6 }}>Ava's brain</div>
                {brain ? (
                  <button
                    type="button"
                    onClick={() => setSelectedModelId(brain.id)}
                    style={{
                      display: 'block', width: '100%', textAlign: 'left', font: 'inherit',
                      background: 'transparent', border: 0, padding: 0, cursor: 'pointer',
                    }}
                  >
                    <div style={{ fontSize: 12.5, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={activityDotStyle(brain)} />
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {brain.model}
                      </span>
                    </div>
                    <div style={{ fontSize: 11, color: stateCopy(brain).tone, marginTop: 2 }}>
                      {stateCopy(brain).label}
                      {brain.memory_gb != null && ` · ${brain.memory_gb.toFixed(1)} GB`}
                      {brain.vram_mb != null && brain.vram_mb > 0 && ` (${(brain.vram_mb / 1024).toFixed(1)} GB on GPU)`}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2, lineHeight: 1.35 }}>
                      {stateCopy(brain).hint}
                      {/* "It does not have this model" is only half an answer —
                          say what it DOES have, which is usually the whole
                          diagnosis (a tag typo, or a pull that never ran). */}
                      {stateOf(brain) === 'absent' && brain.served && brain.served.length > 0 && (
                        <> It has: {brain.served.slice(0, 3).join(', ')}
                          {brain.served.length > 3 && ` +${brain.served.length - 3} more`}.</>
                      )}
                    </div>
                  </button>
                ) : (
                  <div style={{ color: 'var(--muted)', fontSize: 11, lineHeight: 1.35 }}>
                    No model linked yet — pick one in Setup → Agent → Brain.
                  </div>
                )}
              </div>
              <div className="hwb-sec">
                {/* Not "on this machine": a cloud or sandbox brain is listed here
                    too, and its own state says it runs elsewhere. */}
                <div style={{ color: 'var(--muted)', marginBottom: 6 }}>Models Ava can see</div>
                {models.length === 0 ? (
                  <div style={{ color: 'var(--muted)', fontSize: 11, lineHeight: 1.35 }}>
                    No inference engine is running here yet.
                  </div>
                ) : (
                  <>
                    <select
                      className="st-in full"
                      value={selectedModel?.id || ''}
                      onChange={(e) => setSelectedModelId(e.target.value)}
                      style={{ width: '100%', marginBottom: 7, fontSize: 12 }}
                    >
                      {models.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.model}{m.role_key === 'brain' ? ' · brain' : ''} — {stateCopy(m).label}
                        </option>
                      ))}
                    </select>
                    {selectedModel && (
                      <div style={{ fontSize: 11.5, lineHeight: 1.35 }}>
                        <div>
                          <b>Model:</b> <span style={activityDotStyle(selectedModel)} />{selectedModel.model}
                          {selectedModel.role_key === 'brain' && (
                            <span style={{
                              marginLeft: 6, padding: '0 5px', borderRadius: 4, fontSize: 10,
                              fontWeight: 700, background: 'var(--accent)', color: '#fff',
                            }}>brain</span>
                          )}
                        </div>
                        {/* One state, one line. "GPU activity: offline" used to sit
                            directly above "Status: Empty" — two readings of the same
                            fact, in two vocabularies, neither actionable. */}
                        <div><b>State:</b> {stateCopy(selectedModel).label}</div>
                        {selectedModel.role_key !== 'brain' && selectedModel.role && (
                          <div><b>Role:</b> {selectedModel.role}</div>
                        )}
                        <div><b>Runtime:</b> {selectedModel.name}</div>
                        <div><b>Memory:</b> {selectedModel.memory_gb != null ? `${selectedModel.memory_gb.toFixed(2)} GB` : '—'}</div>
                        {selectedModel.gpu_util != null && (
                          <div><b>GPU activity:</b> {Math.round(selectedModel.gpu_util)}%</div>
                        )}
                        <div><b>Source:</b> {selectedModel.source}{selectedModel.pid != null ? ` · PID ${selectedModel.pid}` : ''}</div>
                        <div style={{ marginTop: 7 }}>
                          <div style={{ fontWeight: 700, marginBottom: 4 }}>Model components</div>
                          {selectedModel.components && selectedModel.components.length > 0 ? (
                            <div style={{ maxHeight: 120, overflow: 'auto', border: '1px solid var(--line)', borderRadius: 8, padding: '5px 7px' }}>
                              {selectedModel.components.map((c, i) => (
                                <div key={i} style={{ fontSize: 11, marginBottom: 2 }}>
                                  {(c.kind_label as string) || c.kind}: {c.name}
                                  {c.in_memory === false
                                    ? ' (configured — not in memory)'
                                    : c.in_memory == null
                                      ? ' (configured — residency unknown)'
                                      : ' (in memory)'}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div style={{ color: 'var(--muted)', fontSize: 11 }}>
                              No component breakdown available for this runtime.
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
      <button type="button"
        aria-label="Hardware monitor"
        title="Hardware monitor (drag to move)"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        style={{
          position: 'fixed',
          left: pos.x,
          top: pos.y,
          width: SIZE,
          height: SIZE,
          borderRadius: '50%',
          zIndex: 1000,
          cursor: 'grab',
          touchAction: 'none',
          border: '1px solid var(--line)',
          background: 'var(--accent)',
          color: '#fff',
          boxShadow: '0 6px 20px rgba(0,0,0,.4)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 1,
          userSelect: 'none',
        }}
      >
        <ChipGlyph size={17} />
        <span style={{ fontSize: 10, fontWeight: 700, lineHeight: 1 }}>{gpu?.util != null ? `${Math.round(gpu.util)}%` : 'HW'}</span>
      </button>
    </>
  );
}
