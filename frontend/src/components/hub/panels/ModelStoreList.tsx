import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/layout';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import { ResourceError } from '../ui/ResourceState';
import { Badge } from '../ui/Badge';
import type { StoreModel, SwapStatus } from '../hubApi';

// The model store as a LIBRARY: every model actually on disk, what it costs,
// what is holding it, and the two things an owner can do with it — load it into
// Ava's brain, or delete it and get the space back.
//
// Separate from ModelStorePanel, which lists the ROLES ava.yaml declares. That
// is a different question: a model pulled outside Ava, or pulled and since
// undeclared, is real and occupies real space, and used to be invisible here —
// so it could neither be selected nor reclaimed.

const gb = (v: number | null | undefined) =>
  (v == null ? '—' : v >= 1024 ? `${(v / 1024).toFixed(1)} TB` : `${v.toFixed(1)} GB`);

export function ModelStoreList() {
  const storeRes = useResource(() => hub.store());
  const { data, reload } = storeRes;
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');
  const [swap, setSwap] = useState<SwapStatus | null>(null);
  const [confirm, setConfirm] = useState<StoreModel | null>(null);

  // Poll only while a swap is running. A vLLM boot is minutes, and the log is
  // the only thing that makes that wait legible instead of a frozen button.
  useEffect(() => {
    if (swap?.status !== 'running') return;
    const t = setInterval(async () => {
      try {
        const s = await hub.storeBrainStatus();
        setSwap(s);
        if (s.status !== 'running') { reload(); if (s.error) setErr(s.error); }
      } catch { /* keep the last state */ }
    }, 2000);
    return () => clearInterval(t);
  }, [swap?.status, reload]);

  const del = useCallback(async (m: StoreModel, force: boolean) => {
    setBusy(m.id); setErr(''); setMsg('');
    try {
      const r = await hub.storeDelete(m.engine, m.id, force);
      if (!r.ok) {
        setErr(r.held_by?.length
          ? `In use — ${(r.held_by || []).join('; ')}. Delete anyway to remove it regardless.`
          : (r.error || 'could not delete'));
      } else {
        setMsg(`Deleted ${m.id}${r.freed_gb ? ` — ${gb(r.freed_gb)} reclaimed` : ''}.`);
        setConfirm(null);
        reload();
      }
    } catch (e) { setErr((e as Error).message); } finally { setBusy(''); }
  }, [reload]);

  const toBrain = useCallback(async (m: StoreModel) => {
    setBusy(m.id); setErr(''); setMsg('');
    try {
      const r = await hub.storeBrain(m.engine, m.id);
      if (!r.ok) setErr(r.error || 'could not start the swap');
      else setSwap({ status: 'running', model: m.id, error: null, log: [] });
    } catch (e) { setErr((e as Error).message); } finally { setBusy(''); }
  }, []);

  const running = swap?.status === 'running';
  // Never trust the shape. A bridge that predates this route answers the call
  // with something that is not a store listing, and `data.models.length` on it
  // throws inside render — which blanks the whole Brain tab, not just this
  // panel. An unreadable payload has to degrade to "nothing to show".
  const rows: StoreModel[] = Array.isArray(data?.models) ? data!.models : [];
  const totalGb = typeof data?.total_gb === 'number' ? data!.total_gb : null;

  return (
    <>
      {/* Names THIS panel, not the section. ModelStorePanel below is also a
          "model store" and also renders a <ResourceError>; two failures reading
          "Couldn't load the model store" on one tab cannot be told apart, and
          the two ask different routes. The labels track the panel titles. */}
      <ResourceError r={storeRes} label="the models on disk" />
      <Panel
        title="Models on disk"
        subtitle={rows.length
          ? `${rows.length} model${rows.length === 1 ? '' : 's'} in ${data?.store || 'the store'} · ${gb(totalGb)} used`
          : 'Everything downloaded into this box’s model store.'}
      >
        {data == null ? <EmptyState text="Reading the model store…" />
          : rows.length === 0
            ? <EmptyState text="Nothing downloaded yet — pull a model and it appears here." />
            : rows.map((m) => (
              <div className="hub-row" key={`${m.engine}:${m.id}`}>
                <div className="hub-row-main">
                  <div className="hub-row-title">
                    {m.id}{' '}
                    <span style={{ color: 'var(--muted)', fontWeight: 400 }}>· {m.engine}</span>
                  </div>
                  <div className="hub-row-sub">{gb(m.size_gb)} on disk</div>
                  {/* Say WHAT is holding it, not just that something is. The
                      owner cannot act on "in use". */}
                  {m.in_use && (
                    <div className="hub-row-sub">{(m.held_by || []).join(' · ')}</div>
                  )}
                </div>
                <div className="hub-row-actions">
                  {m.in_use && <Badge tone="ok">in use</Badge>}
                  {m.engine === 'vllm' && (
                    <button type="button" className="hub-btn ghost sm"
                      disabled={!!busy || running}
                      onClick={() => toBrain(m)}>
                      <Icon name="sparkles" />Use as brain
                    </button>
                  )}
                  <button type="button" className="hub-btn ghost sm"
                    disabled={!!busy || running}
                    onClick={() => { setConfirm(m); setErr(''); }}>
                    <Icon name="trash" />Delete
                  </button>
                </div>
              </div>
            ))}

        {/* Deleting weights is irreversible and measured in GB, so it is never
            one click — and the confirm names the size being reclaimed. */}
        {confirm && (
          <div className="hub-preview" style={{ marginTop: 12 }}>
            <div className="hub-preview-head">
              <Icon name="alert" />Delete {confirm.id}?
            </div>
            <div className="hub-row-sub" style={{ padding: '0 0 8px' }}>
              Removes it from disk and frees {gb(confirm.size_gb)}. This cannot be undone.
              {confirm.in_use && ` It is currently in use — ${(confirm.held_by || []).join('; ')}.`}
            </div>
            <div className="hub-row-actions">
              <button type="button" className="hub-btn sm" disabled={!!busy}
                onClick={() => del(confirm, confirm.in_use)}>
                {busy ? 'Deleting…' : confirm.in_use ? 'Delete anyway' : 'Delete'}
              </button>
              <button type="button" className="hub-btn ghost sm" disabled={!!busy}
                onClick={() => setConfirm(null)}>Cancel</button>
            </div>
          </div>
        )}

        {swap && swap.status !== 'idle' && (
          <div className="hub-preview" style={{ marginTop: 12 }}>
            <div className="hub-preview-head">
              <Icon name={running ? 'refresh' : swap.status === 'done' ? 'check' : 'alert'} />
              {running ? `Loading ${swap.model} into Ava's brain…`
                : swap.status === 'done' ? `Ava's brain is now ${swap.model}`
                  : `Could not load ${swap.model}`}
            </div>
            <pre>{swap.log.length ? swap.log.join('\n') : 'starting…'}</pre>
          </div>
        )}

        {msg && <div className="hub-msg ok">{msg}</div>}
        {err && <div className="hub-msg err">{err}</div>}
      </Panel>
    </>
  );
}
