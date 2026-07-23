import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { api } from '../lib/api';
import { ProgressBar } from '../lib/ProgressBar';
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

const statusLabel = (s?: string) => {
  if (!s) return 'Unknown';
  if (s === 'loaded') return 'Loaded';
  if (s === 'offline') return 'Offline (not in memory)';
  if (s === 'empty') return 'Empty';
  return s.charAt(0).toUpperCase() + s.slice(1);
};

const activityState = (m: { status?: string; gpu_util?: number | null }) => {
  if (m.status !== 'loaded') return 'offline';
  const u = m.gpu_util;
  if (u == null) return 'idle';
  if (u >= 30) return 'active';
  if (u > 0) return 'low';
  return 'idle';
};

const activityDotStyle = (m: { status?: string; gpu_util?: number | null }): CSSProperties => {
  const s = activityState(m);
  const color = s === 'active' ? '#34d27a' : s === 'low' ? '#e6b85c' : s === 'idle' ? '#8b98ad' : '#596172';
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
  const selectedModel = models.find((m) => m.id === selectedModelId) || models[0] || null;

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
          <div style={{ marginTop: 10, borderTop: '1px solid var(--line)', paddingTop: 9 }}>
            <div style={{ color: 'var(--muted)', marginBottom: 6 }}>Loaded models in memory</div>
            {models.length === 0 ? (
              <div style={{ color: 'var(--muted)', fontSize: 11 }}>No model process detected yet.</div>
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
                      {m.model}
                    </option>
                  ))}
                </select>
                {selectedModel && (
                  <div style={{ fontSize: 11.5, lineHeight: 1.35 }}>
                    <div>
                      <b>Model:</b> <span style={activityDotStyle(selectedModel)} />{selectedModel.model}
                    </div>
                    {selectedModel.role && <div><b>Role:</b> {selectedModel.role}</div>}
                    <div><b>Runtime:</b> {selectedModel.name}</div>
                    <div><b>Memory:</b> {selectedModel.memory_gb != null ? `${selectedModel.memory_gb.toFixed(2)} GB` : '—'}</div>
                    <div><b>GPU activity:</b> {selectedModel.gpu_util != null ? `${Math.round(selectedModel.gpu_util)}%` : (selectedModel.status === 'loaded' ? 'idle/unknown' : 'offline')}</div>
                    <div><b>Status:</b> {statusLabel(selectedModel.status)}</div>
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
      <button
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
