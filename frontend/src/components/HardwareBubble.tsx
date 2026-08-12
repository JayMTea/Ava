import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode } from 'react';
import { api } from '../lib/api';
import { ProgressBar } from '../lib/ProgressBar';
import { stateCopy } from '../lib/modelState';
import type { HardwareStats } from '../lib/types';
import { appAccent, appById, AppDot } from '../lib/appColor';
import {
  MODEL_RELATION, activityTone, componentMeta, foundVia, groupMemoryGb,
  groupRows, holdsLine, identified, isAvas, isCollapsible,
  listHint, memPhrase, poolOf, relationOf, rowSub, rowTitle, shareOf, tempTone,
} from './hwModels';
import type { MemPool, Row as HwRow } from './hwModels';
import { Icon } from '../lib/icons';

// Floating, draggable hardware monitor. Tap to expand a live quick-view of the
// DGX Spark (GPU %, unified memory used/free, CPU, temperature); drag the bubble
// anywhere so it never blocks the UI. Position is remembered across sessions.
//
// The panel's LOOK lives in styles/hwbubble.css; what stays here is everything
// that depends on where the bubble was dropped — the anchor offsets and the two
// bounds below.

const KEY = 'ava.hwbubble.pos';
// Which sections the owner has folded away, by relation token. Remembered for
// the same reason the bubble's position is: a panel that forgets what you told
// it costs you the same click on every visit. Nothing is collapsed by default —
// see `isCollapsible` for why a 65 GB stranger must never be hidden on the
// first open.
const GROUPS_KEY = 'ava.hwbubble.collapsed';
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

function loadCollapsed(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(GROUPS_KEY) || '[]');
    if (Array.isArray(v)) return v.filter((x) => typeof x === 'string');
  } catch {
    /* ignore */
  }
  return [];
}

const gb = (v: number | null | undefined) => (v == null ? '—' : v >= 1024 ? `${(v / 1024).toFixed(1)} TB` : `${v.toFixed(1)} GB`);
const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v)}%`);
const temp = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v)}°C`);

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
// Taken from the payload contract rather than re-declared, so this panel and
// lib/types.ts cannot drift apart field by field.
type Row = HwRow;

/** One row shape for every model, the brain included.
 *
 *  The brain used to have its own markup and its own two type sizes for the
 *  same three facts. `hero` does not change the shape — it only keeps the
 *  brain's hint on screen without a click, which Setup → Agent's brain
 *  visibility contract requires: the brain is named by configuration, so it is
 *  knowable even on a box where nothing is running and nothing is measurable.
 */
function ModelRow({ m, pool, open, onToggle, children }: {
  m: Row; pool: MemPool; open: boolean; onToggle: (id: string) => void;
  children?: ReactNode;
}) {
  const app = m.app ? appById(m.app) : undefined;
  const share = shareOf(m, pool);
  const hero = m.role_key === 'brain';
  const hint = hero ? listHint(m) : '';
  const sub = rowSub(m);
  return (
    <li>
      <button
        type="button"
        className="hwb-row"
        aria-expanded={open}
        title={rowTitle(m)}
        onClick={() => onToggle(m.id)}
        style={share != null ? ({ '--share': `${share}%` } as CSSProperties) : undefined}
      >
        <span className={`hwb-dot tone-${activityTone(m)}`} aria-hidden="true" />
        <span className="hwb-title">
          <span className="hwb-name">{rowTitle(m)}</span>
          {/* A connected app's row carries the app's own accent, never Ava's
              (CLAUDE.md), beside the name the accent belongs to. */}
          {m.app && <AppDot accent={appAccent(m.app)} title={app?.label || m.app} />}
        </span>
        <span className="hwb-num">{gb(m.memory_gb)}</span>
        {sub && <span className="hwb-sub">{sub}</span>}
        {hint && <span className="hwb-hint">{hint}</span>}
      </button>
      {children}
    </li>
  );
}

/** What a row is, once you ask.
 *
 *  It sits directly beneath the row it describes, which is what lets it carry
 *  no title, no "Model:" line and no relation note — the row above names the
 *  model, and the section heading above that already said whose it is. The old
 *  block, pinned to the bottom of the column, had to restate all three, and
 *  restated the group's "not Ava's" sentence ~200px below the group's own copy.
 */
function ModelDetail({ m, pool }: { m: Row; pool: MemPool }) {
  const app = m.app ? appById(m.app) : undefined;
  const comps = m.components || [];
  const denominator = memPhrase(m, pool);
  // The brain row already shows these; repeating them one line lower is the
  // duplication this redesign exists to remove. `listHint` also drops the
  // resident hint, so a working model never spends a line saying so — the meta
  // line above already reads "In memory · vLLM".
  const hint = m.role_key === 'brain' ? '' : listHint(m);
  return (
    <div className="hwb-detail">
      {/* One state, one line. "GPU activity: offline" used to sit directly above
          "Status: Empty" — two readings of the same fact, in two vocabularies,
          neither actionable. The runtime joins it rather than spending a whole
          labelled row on one word. */}
      <div className="hwb-detail-meta">{stateCopy(m).label} · {m.name}</div>
      {m.memory_gb != null && (
        <div className="hwb-detail-mem">
          {m.memory_gb.toFixed(2)} GB{denominator && ` · ${denominator}`}
        </div>
      )}
      {hint && <div className="hwb-detail-hint">{hint}</div>}
      {/* The full sentence belongs here, where it has room; the row shows the
          short form so it does not ellipse into a truncated error. */}
      {!identified(m) && <div className="hwb-detail-hint">{holdsLine(m)}</div>}
      <dl className="hwb-dl">
        {m.app && (
          <>
            <dt>Belongs to</dt>
            <dd className="hwb-owner">
              <AppDot accent={appAccent(m.app)} />{app?.label || m.app}
            </dd>
          </>
        )}
        {m.role_key !== 'brain' && m.role && (<><dt>Role</dt><dd>{m.role}</dd></>)}
        {m.vram_mb != null && m.vram_mb > 0 && (
          <><dt>On GPU</dt><dd>{(m.vram_mb / 1024).toFixed(1)} GB</dd></>
        )}
        {m.gpu_util != null && (<><dt>GPU use</dt><dd>{Math.round(m.gpu_util)}%</dd></>)}
        {/* "Source: nvidia-smi" was a machine token used as owner copy — the
            backend returns facts, the SPA words them. The PID is its own pair so
            it joins the aligned value column instead of trailing on a bullet. */}
        <dt>Found via</dt><dd>{foundVia(m.source)}</dd>
        {m.pid != null && (<><dt>PID</dt><dd>{m.pid}</dd></>)}
      </dl>
      <div className="hwb-cmp-lab">Components</div>
      {comps.length > 0 ? (
        <div className="hwb-cmp">
          {comps.map((c, i) => (
            <div className="hwb-cmp-row" key={i}>
              <div className="hwb-cmp-name">{c.name}</div>
              <div className="hwb-cmp-meta">{componentMeta(c)}</div>
            </div>
          ))}
        </div>
      ) : (
        <div className="hwb-empty">No component breakdown for this runtime.</div>
      )}
    </div>
  );
}

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
  // '' means browse. The panel used to default this to the brain, which meant a
  // ~200px detail block was ALWAYS open below the list — the single biggest
  // thing making the column unreadable. Now a card is something you asked for.
  const [openId, setOpenId] = useState('');
  const [collapsed, setCollapsed] = useState<string[]>(loadCollapsed);
  const drag = useRef<{ sx: number; sy: number; ox: number; oy: number; moved: boolean } | null>(null);

  useEffect(() => {
    localStorage.setItem(KEY, JSON.stringify(pos));
  }, [pos]);

  useEffect(() => {
    localStorage.setItem(GROUPS_KEY, JSON.stringify(collapsed));
  }, [collapsed]);

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
  // Everything the brain section above does not already show.
  // The panel's top-level cut is Ava's / not Ava's, NOT brain / everything
  // else. A backend the owner registered with Ava is Ava's however unreachable
  // it is, and filing it under "Model use outside Ava" put a heading calling it
  // foreign over a row that then refused to fold because the fold rule knew
  // better. One predicate now decides both.
  const ownEngines = models.filter((m) => relationOf(m) === 'configured');
  const outside = models.filter((m) => !isAvas(relationOf(m)));
  const pool = poolOf(stats);
  const ownGb = groupMemoryGb(ownEngines, pool);
  const outsideGb = groupMemoryGb(outside, pool);
  const toggle = useCallback(
    (id: string) => setOpenId((cur) => (cur === id ? '' : id)),
    [],
  );
  const toggleGroup = (relation: string, rows: Row[]) => {
    const folding = !collapsed.includes(relation);
    setCollapsed(folding ? [...collapsed, relation] : collapsed.filter((r) => r !== relation));
    // Folding a section away must not leave a card open inside it — it would
    // vanish with no way back except re-expanding, and re-expanding would then
    // reopen a card nobody asked for a second time.
    if (folding && rows.some((m) => m.id === openId)) setOpenId('');
  };

  // A process that exits while its card is open closes the card. The list is
  // the truth; a card describing something that is gone is worse than no card.
  useEffect(() => {
    if (openId && !models.some((m) => m.id === openId)) setOpenId('');
  }, [models, openId]);

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
                <span className={'hwb-temp' + (tempTone(gpu?.temp) ? ` tone-${tempTone(gpu?.temp)}` : '')}>
                  {temp(gpu?.temp)}
                </span>
              </div>
              {/* "Memory Ava can free" (components/AllocSection.tsx) rendered here
                  and is deliberately disconnected — the allocator underneath it is
                  not ready to be a button an owner can press by accident.

                  Nothing was deleted. The component, its pure view logic in
                  allocView.ts, the hub client methods, the HTTP routes and
                  `ava alloc release|restore` all still exist and still pass their
                  tests; the CLI remains the way to drive it. Re-enabling is this
                  import and this line.

                  It belongs in THIS column when it comes back, not the right one:
                  this column is what the machine is holding, which is the question
                  the section answers. On the right it made 176px of content on the
                  left sit against 708px, in a panel with ~790px to spend. */}
            </div>
            {/* Right column: the models. Cutting here rather than anywhere else
                is what keeps a subject whole — the panel already drew its own
                dividers in these two places. */}
            <div className="hwb-col hwb-col-models">
              {/* Ava's brain, always — it is named by configuration, so it is
                  knowable even when nothing is running. This block used to be
                  absent entirely on a machine with no GPU, which told a user whose
                  model was serving fine that no model existed.

                  It is the same <ModelRow> as everything below it, not a second
                  markup for the same three facts — it just keeps its hint on
                  screen without a click, because that is the one row whose
                  diagnosis has to be readable on a box with no telemetry. */}
              <div className="hwb-sec">
                <div className="hwb-lab">{MODEL_RELATION.brain.group}</div>
                {brain ? (
                  <ul className="hwb-rows">
                    <ModelRow m={brain} pool={pool} open={openId === brain.id} onToggle={toggle}>
                      {openId === brain.id && <ModelDetail m={brain} pool={pool} />}
                    </ModelRow>
                  </ul>
                ) : (
                  <div className="hwb-empty">
                    No model linked yet — pick one in Setup → Agent → Brain.
                  </div>
                )}
              </div>
              {/* Ava's own engines, on Ava's side of the cut. A top-level
                  section rather than a group inside the one below, because that
                  heading says "outside Ava" and this is not — the owner
                  registered it. Dropped entirely when there is none, so a box
                  with a single brain never grows an empty heading. */}
              {ownEngines.length > 0 && (
                <div className="hwb-sec">
                  <div className="hwb-lab">
                    <span>{MODEL_RELATION.configured.group}</span>
                    <span className="hwb-num">{ownGb != null ? gb(ownGb) : ''}</span>
                  </div>
                  <ul className="hwb-rows">
                    {ownEngines.map((m) => (
                      <ModelRow key={m.id} m={m} pool={pool}
                                open={openId === m.id} onToggle={toggle}>
                        {openId === m.id && <ModelDetail m={m} pool={pool} />}
                      </ModelRow>
                    ))}
                  </ul>
                </div>
              )}
              <div className="hwb-sec">
                {/* Not "on this machine": a cloud or sandbox brain is listed here
                    too, and its own state says it runs elsewhere.

                    Grouped, not a <select>. A dropdown shows ONE row at a time,
                    and this panel sits beside the memory gauge to answer "where
                    did my memory go" — hiding a 65 GB row behind a closed
                    control is the wrong shape for that question, however the
                    options are labelled. Flattening four different
                    relationships into one list is what made a live, correct
                    list read as stale junk. */}
                <div className="hwb-lab">
                  <span>Model use outside Ava</span>
                  {/* The section's own total — everything holding model memory
                      that is not Ava's. It used to sit on the "Other programs"
                      heading, which is gone; putting it here is better than
                      where it was, because "where did my memory go" is a
                      question about the whole section, not one group of it. */}
                  <span className="hwb-num">
                    {outsideGb != null ? gb(outsideGb) : ''}
                  </span>
                </div>
                {models.length === 0 ? (
                  <div className="hwb-empty">No inference engine is running here yet.</div>
                ) : (
                  <>
                    {/* Ava's own rows are excluded here — the brain and any
                        engines the owner registered each have their own section
                        above, and this heading would be lying about them. */}
                    {outside.length === 0 && (
                      <div className="hwb-empty">
                        Nothing else on this machine is holding model memory.
                      </div>
                    )}
                    {/* Sections stay in RELATION_ORDER and are NOT ranked by
                        weight, which would put a stranger's 65 GB process above
                        the app the owner connected. The heading carries the
                        section's total in the rows' own column, so it answers
                        "where did my memory go" without reordering anything:
                        how much is the number, whose is the order. */}
                    {groupRows(outside, pool).map((g) => {
                      const foldable = isCollapsible(g.relation);
                      const shown = !foldable || !collapsed.includes(g.relation);
                      const listId = `hwb-grp-${g.relation}`;
                      // The total rides on the summary, not inside the fold, so
                      // collapsing a section removes its detail and never its
                      // share of the machine's memory.
                      const total = g.memoryGb != null ? gb(g.memoryGb) : '';
                      return (
                        <div className="hwb-grp" key={g.relation}>
                          {foldable ? (
                            <button type="button" className="hwb-grp-head is-toggle"
                                    aria-expanded={shown} aria-controls={listId}
                                    onClick={() => toggleGroup(g.relation, g.rows)}>
                              <span className={'hwb-chev' + (shown ? ' is-open' : '')}
                                    aria-hidden="true"><Icon name="chevronDown" /></span>
                              <span className="hwb-grp-name">{g.copy.group}</span>
                              <span className="hwb-num">{total}</span>
                            </button>
                          ) : (
                            <div className="hwb-grp-head">
                              <span className="hwb-chev-gap" aria-hidden="true" />
                              <span className="hwb-grp-name">{g.copy.group}</span>
                              <span className="hwb-num">{total}</span>
                            </div>
                          )}
                          {shown && (
                            <div id={listId}>
                              {g.copy.note && (
                                <div className="hwb-grp-note">{g.copy.note}</div>
                              )}
                              <ul className="hwb-rows">
                                {g.rows.map((m) => (
                                  <ModelRow key={m.id} m={m} pool={pool}
                                            open={openId === m.id} onToggle={toggle}>
                                    {openId === m.id && <ModelDetail m={m} pool={pool} />}
                                  </ModelRow>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      );
                    })}
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
