// "Memory Ava can free" — the hardware monitor's one actuator.
//
// It lives inside the bubble's panel, which is a SIBLING of the draggable circle
// and carries no pointer handlers of its own, so a button here neither drags the
// bubble nor toggles it. (The circle itself would be hostile: it captures the
// pointer, and a "click" inside it becomes an open/close.)
//
// Built from the Hub's shared pieces — `useResource` / `useAction`, `Badge`,
// `HubMessage`, the `.tone-*` system — rather than hand-rolled ones. `hub.css` is
// imported app-wide in main.tsx and its selectors are top-level, not scoped under
// `.hub`, so this costs no CSS and forks no design system. `data/DataView.tsx` sets
// the precedent for reaching into `hub/` from outside it, and states it as policy.
//
// Every judgement about what to offer and what to claim lives in `allocView.ts`,
// which is pure and tested. This file renders it.
import { useCallback, useEffect, useRef, useState } from 'react';

import { receipt, rowOrder, rowView, heldLabel } from './allocView';
import { hub } from './hub/hubApi';
import type { AllocJob, AllocModel } from './hub/hubApi';
import { useAction, useResource } from './hub/hooks';
import { Badge } from './hub/ui/Badge';
import { HubMessage } from './hub/ui/HubMessage';
import { ResourceError } from './hub/ui/ResourceState';
import { ProgressBar } from '../lib/ProgressBar';

const muted = { color: 'var(--muted)', fontSize: 11, lineHeight: 1.35 } as const;

function Elapsed({ from }: { from: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, []);
  const s = Math.max(0, Math.round(now - from));
  return <>{Math.floor(s / 60)}:{String(s % 60).padStart(2, '0')}</>;
}

/** The in-flight run. A moving step reads as working; a disabled button reads as
 *  broken — the lesson AgentRuntimePanel already learned for provisioning. */
function RunView({ job }: { job: AllocJob }) {
  const lines = job.log || [];
  return (
    <div style={{ marginTop: 6 }}>
      <div style={{ fontSize: 11, marginBottom: 4 }}>
        {job.verb === 'restore' ? 'Loading it back' : 'Freeing memory'}
        {job.started_at != null && <> · <Elapsed from={job.started_at} /></>}
      </div>
      {/* Indeterminate at zero, deliberately: a bar waiting on model memory must
          never look frozen, and there is no percentage to report — a container
          stop plus kernel reclaim has no progress, only a duration. */}
      <ProgressBar progress={0} />
      {lines.length > 0 && (
        <div style={{ ...muted, marginTop: 4 }}>{lines[lines.length - 1]}</div>
      )}
    </div>
  );
}

function Row({ m, breaker, busy, running, onAct }: {
  m: AllocModel;
  breaker: Parameters<typeof rowView>[1];
  busy: boolean;
  running: AllocJob | null;
  onAct: (m: AllocModel, verb: 'release' | 'restore') => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const v = rowView(m, breaker);
  const isMe = running?.model === m.id && running?.status === 'running';

  return (
    <div style={{ padding: '6px 0', borderTop: '1px solid var(--line)' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap' }}>
        <b style={{ fontSize: 11.5 }}>{m.label || m.id}</b>
        {m.is_brain && <Badge tone="accent">brain</Badge>}
        <Badge tone={v.tone}>{v.state}</Badge>
      </div>
      <div style={muted}>
        {m.driver} · holding {heldLabel(m)}
      </div>
      {v.hint && <div style={{ ...muted, marginTop: 2 }}>{v.hint}</div>}

      {isMe && running ? (
        <RunView job={running} />
      ) : v.action ? (
        confirming && v.confirm ? (
          // `.hub-restart` is the shared warning chassis, but it is `display: flex`
          // in a row and sized for a Setup panel — in this ~200px column the text
          // child collapsed to about six characters wide. Overridden to a block so
          // the colours and border are still the shared ones and the layout is the
          // column's. The variant is `.err`, not `.tone-err`: hub.css says so
          // directly, because these are surface variants, not the tone token map.
          <div className="hub-restart err"
               style={{ display: 'block', marginTop: 6, marginBottom: 0,
                        padding: '8px 9px', fontSize: 11, lineHeight: 1.35 }}>
            <div style={{ marginBottom: 6 }}>{v.confirm}</div>
            <button type="button" className="hub-btn sm" disabled={busy}
              onClick={() => { setConfirming(false); onAct(m, v.action!.verb); }}>
              {v.action.label}
            </button>
            <button type="button" className="hub-btn ghost sm" style={{ marginLeft: 6 }}
              onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        ) : (
          <button type="button" className="hub-btn ghost sm" style={{ marginTop: 6 }}
            disabled={busy}
            // Disabled only while ANOTHER row is acting, and the title says why —
            // a button that goes dead with no reason reads as broken.
            title={busy ? 'Ava is already freeing something' : undefined}
            onClick={() => (v.confirm ? setConfirming(true) : onAct(m, v.action!.verb))}>
            {busy ? v.action.busyLabel : v.action.label}
          </button>
        )
      ) : v.blocked ? (
        <div style={{ ...muted, marginTop: 2, fontStyle: 'italic' }}>{v.blocked}</div>
      ) : null}
    </div>
  );
}

export function AllocSection() {
  const res = useResource(hub.alloc, []);
  const act = useAction();
  const [job, setJob] = useState<AllocJob | null>(null);
  // The effect below needs to reload the report and post a receipt when a job
  // lands, but neither callback is referentially stable — listing them would
  // restart the poll on every render. A ref keeps the dependency list honest AND
  // short: it depends on the job's status, and on nothing else.
  const onDone = useRef<(j: AllocJob) => void>(() => {});
  onDone.current = (j: AllocJob) => {
    // Name it the way the row above names it, not by its id.
    const named = res.data?.models.find((m) => m.id === j.model);
    const r = receipt(j, named?.label || undefined);
    if (r) act.setMessage(r);
    res.reload();
    // The freed memory shows up in /api/hardware too, and the bubble's own poll is
    // up to 5s behind. Same idiom as `ava:apps-changed`.
    window.dispatchEvent(new Event('ava:models-changed'));
  };

  // Poll ONLY while a job runs. The report costs a driver probe per model — for a
  // docker model that is an `inspect` plus a `stats` — so putting it on the
  // bubble's 2s timer would run a shell command every two seconds, forever.
  const runningNow = job?.status === 'running';
  useEffect(() => {
    if (!runningNow) return;
    let alive = true;
    const id = window.setInterval(async () => {
      try {
        const j = await hub.allocJob();
        if (!alive) return;          // the panel closed mid-flight
        setJob(j);
        if (j.status !== 'running') onDone.current(j);
      } catch { /* the next tick retries; a transient blip is not a failure */ }
    }, 700);
    return () => { alive = false; window.clearInterval(id); };
  }, [runningNow]);

  const onAct = useCallback((m: AllocModel, verb: 'release' | 'restore') => {
    act.run(async () => {
      const r = verb === 'release' ? await hub.allocRelease(m.id) : await hub.allocRestore(m.id);
      if (!r.ok) {
        // A refusal before the job even starts (in use, a turn in flight, another
        // job). Rendered through the same receipt vocabulary so the owner does not
        // meet two different explanations for the same word.
        const fake = { status: 'error', model: m.id, verb, log: [], started_at: null,
                       ended_at: null, result: { ok: false, code: r.code || 'error' } };
        return receipt(fake as AllocJob, m.label)?.text || 'That did not work.';
      }
      setJob(r.job || null);
      return null;
    });
  }, [act]);

  const running = job?.status === 'running' ? job : null;

  return (
    <div className="hwb-sec">
      <div style={{ color: 'var(--muted)', marginBottom: 6 }}>Memory Ava can free</div>
      <ResourceError r={res} label="what is holding memory" />
      {res.data == null ? (
        !res.error && <div style={muted}>Checking…</div>
      ) : !res.data.enabled ? (
        <div style={muted}>Memory management is switched off (alloc.enabled).</div>
      ) : res.data.models.length === 0 ? (
        <div style={muted}>
          Nothing here holds memory Ava can account for. Ava only ever acts on models
          it knows about — it never goes looking for processes to stop.
        </div>
      ) : (
        // No inner scroller. `.hwb-panel` already scrolls and is capped at the
        // space beside the bubble, and a second one here clipped the confirm's
        // Cancel button below its own fold — a destructive step whose way out is
        // off-screen is worse than a longer panel.
        <div>
          {rowOrder(res.data.models).map((m) => (
            <Row key={m.id} m={m} breaker={res.data!.breaker} busy={act.busy || !!running}
                 running={running} onAct={onAct} />
          ))}
        </div>
      )}
      <HubMessage message={act.message} style={{ marginTop: 6, fontSize: 11 }} />
      {res.data?.enabled && (
        <div style={{ ...muted, marginTop: 6 }}>
          Anything not listed here is counted, never touched — it is not Ava's to free.
        </div>
      )}
    </div>
  );
}
