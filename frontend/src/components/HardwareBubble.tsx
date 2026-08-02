import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { api } from '../lib/api';
import { ProgressBar } from '../lib/ProgressBar';
import { stateCopy, stateOf } from '../lib/modelState';
import type { StatefulRow } from '../lib/modelState';
import type { HardwareStats } from '../lib/types';

// Floating, draggable hardware monitor. Tap to expand a live quick-view of the
// DGX Spark (GPU %, unified memory used/free, CPU, temperature); drag the bubble
// anywhere so it never blocks the UI. Position is remembered across sessions.

const KEY = 'ava.hwbubble.pos';
const SIZE = 52;

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
  const [open, setOpen] = useState(false);
  const [stats, setStats] = useState<HardwareStats | null>(null);
  const [selectedModelId, setSelectedModelId] = useState('');
  const drag = useRef<{ sx: number; sy: number; ox: number; oy: number; moved: boolean } | null>(null);

  useEffect(() => {
    localStorage.setItem(KEY, JSON.stringify(pos));
  }, [pos]);

  useEffect(() => {
    const onResize = () => setPos((p) => clampPos(p));
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

  const rightHalf = pos.x + SIZE / 2 > vw() / 2;
  const bottomHalf = pos.y + SIZE / 2 > vh() / 2;
  const panelStyle: CSSProperties = {
    position: 'fixed',
    width: 236,
    zIndex: 999,
    background: 'var(--panel2)',
    border: '1px solid var(--line)',
    borderRadius: 12,
    padding: '12px 13px',
    boxShadow: '0 10px 34px rgba(0,0,0,.4)',
    fontSize: 12.5,
    ...(rightHalf ? { right: vw() - pos.x + 8 } : { left: pos.x + SIZE + 8 }),
    ...(bottomHalf ? { bottom: vh() - (pos.y + SIZE) } : { top: pos.y }),
  };

  const gpu = stats?.gpu;
  const mem = stats?.mem;
  const disk = stats?.disk;
  const cpu = stats?.cpu;
  const models = stats?.models || [];
  const jobs = stats?.jobs || [];
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
        <div style={panelStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10, fontWeight: 700 }}>
            <ChipGlyph />
            <span>{gpu?.name || 'Compute'}</span>
          </div>
          <Metric label="GPU util" value={pct(gpu?.util)} progress={gpu?.util ?? 0} />
          {/* What's driving the GPU right now — names the job behind a spike. */}
          <div
            style={{
              marginTop: 6,
              marginBottom: 4,
              padding: '6px 8px',
              border: '1px solid var(--line)',
              borderRadius: 8,
              background: jobs.length ? 'rgba(52,210,122,0.07)' : 'transparent',
            }}
          >
            <div style={{ color: 'var(--muted)', fontSize: 11, marginBottom: jobs.length ? 4 : 0 }}>Running now</div>
            {jobs.length === 0 ? (
              <div style={{ color: 'var(--muted)', fontSize: 11 }}>Idle — no active render or job.</div>
            ) : (
              jobs.map((j, i) => (
                <div key={i} style={{ fontSize: 11.5, lineHeight: 1.4, display: 'flex', alignItems: 'baseline', gap: 6 }}>
                  <span style={{ flex: '0 0 auto', width: 6, height: 6, borderRadius: '50%', background: '#34d27a', display: 'inline-block', transform: 'translateY(-1px)' }} />
                  <span>
                    <b>{j.name}</b>
                    {(j.stage || j.progress != null) && (
                      <span style={{ color: 'var(--muted)' }}>
                        {' — '}
                        {j.stage || 'running'}
                        {j.progress != null ? ` (${Math.round(j.progress)}%)` : ''}
                      </span>
                    )}
                  </span>
                </div>
              ))
            )}
          </div>
          <Metric
            label={`Memory · ${gb(mem?.used_gb)} / ${gb(mem?.total_gb)}`}
            value={pct(mem?.used_pct)}
            progress={mem?.used_pct ?? 0}
            sub={`${gb(mem?.free_gb)} free`}
          />
          <Metric
            label={`Disk · ${gb(disk?.used_gb)} / ${gb(disk?.total_gb)}`}
            value={pct(disk?.used_pct)}
            progress={disk?.used_pct ?? 0}
            sub={`${gb(disk?.free_gb)} free`}
          />
          <Metric label="CPU util" value={pct(cpu?.util)} progress={cpu?.util ?? 0} />
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8 }}>
            <span style={{ color: 'var(--muted)' }}>GPU temp</span>
            <span style={{ fontWeight: 700, color: tempColor(gpu?.temp) }}>{temp(gpu?.temp)}</span>
          </div>
          {/* Ava's brain, always — it is named by configuration, so it is
              knowable even when nothing is running. This block used to be
              absent entirely on a machine with no GPU, which told a user whose
              model was serving fine that no model existed. */}
          <div style={{ marginTop: 10, borderTop: '1px solid var(--line)', paddingTop: 9 }}>
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
          <div style={{ marginTop: 10, borderTop: '1px solid var(--line)', paddingTop: 9 }}>
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
                          {String(selectedModel.name || '').toLowerCase().includes('gpusvc')
                            ? 'No gpusvc component breakdown detected yet (it updates once model file mappings are visible).'
                            : 'No component breakdown available for this runtime.'}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </>
            )}
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
