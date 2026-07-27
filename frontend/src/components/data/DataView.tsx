import { useCallback, useState } from 'react';
import { Icon } from '../../lib/icons';
import { ago, EmptyState, Panel, fmtClock, fmtInt } from '../dashboard/primitives';
import { useLiveResource } from '../../hooks/useLive';
import { MemoryPanel } from '../hub/MemoryPanel';
import { eventMeta } from '../hub/events';
import { useResource } from '../hub/hooks';
import type { ActionMsg } from '../hub/hooks';
import { Badge } from '../hub/ui/Badge';
import { HubMessage } from '../hub/ui/HubMessage';
import { Legend } from '../hub/ui/Legend';
import { Tile } from '../hub/ui/Tile';
import { api } from '../../lib/api';
import { hub } from '../hub/hubApi';
import { dataApi } from './dataApi';
import { ResourceError } from '../hub/ui/ResourceState';
import type { ChatRow, DataStore, LogEvent, LogName, StoresResponse } from './dataApi';

// Data — the transparency page: everything Ava keeps on disk, one row per store,
// with the memory browser as the flagship tab. Built from the SAME pieces as the
// Setup (Hub) page — Tile / Badge / Legend / the row grammar / one tone system —
// so the two read as one product (see hub/ui + hub/events). The backend returns
// facts; the owner-facing copy lives here.

type TabId = 'overview' | 'memory' | 'chats' | 'logs' | 'maintenance';

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'grid' },
  { id: 'memory', label: 'Memory', icon: 'db' },
  { id: 'chats', label: 'Chats', icon: 'chats' },
  { id: 'logs', label: 'Logs', icon: 'activity' },
  { id: 'maintenance', label: 'Maintenance', icon: 'sliders' },
];

// Which tab a store row's "Browse" opens.
const BROWSE_TAB: Record<string, TabId> = {
  memory: 'memory', chats: 'chats', audit: 'logs', performance: 'logs', devices: 'logs',
  alloc: 'logs',
};

const STORE_META: Record<string, { icon: string; desc: string }> = {
  memory: { icon: 'db', desc: 'Distilled facts Ava has learned about you, plus indexed document chunks. Recalled into chats; every recall and edit lands in the audit ledger.' },
  chats: { icon: 'chats', desc: 'Every conversation — messages, attachments, and which model answered. Open and delete chats from the sidebar.' },
  audit: { icon: 'file', desc: 'Ava’s flight recorder: turns, memory recalls and edits, connector grants. Append-only; browsable under Setup → History.' },
  performance: { icon: 'chart', desc: 'Generation throughput, latency, and energy per turn, with hourly and daily rollups behind the Vitals charts.' },
  alloc: { icon: 'sliders', desc: 'Every allocation decision: what was asked for, what the pool held, and what (if anything) was released to make room. This is the log you read before letting the allocator act.' },
  hw_history: { icon: 'gauge', desc: 'GPU, memory, and CPU samples at minute and hour resolution — the long-range series behind the Vitals gauges.' },
  devices: { icon: 'activity', desc: 'Sensor readings and events from connected devices, one rotated stream per connector.' },
  media_gen: { icon: 'image', desc: 'Images and video Ava has generated. Use Reclaim space on the Maintenance tab to prune old files.' },
  uploads: { icon: 'attach', desc: 'Files you’ve shared with Ava in chat. Document text is indexed into Memory; the originals stay here.' },
  secrets: { icon: 'lock', desc: 'Login password, session key, internal tokens, and backend API keys. Names are listed for transparency — the values are never displayed, exported, or browsable.' },
};

const FORMAT_TONE: Record<DataStore['format'], 'accent' | 'ok' | 'warn' | 'err'> = {
  sqlite: 'accent', json: 'accent', jsonl: 'ok', files: 'warn', locked: 'err',
};

function fmtBytes(n: number): string {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

// ── Overview ────────────────────────────────────────────────────────────────
function StoreRow({ s, onBrowse }: { s: DataStore; onBrowse?: () => void }) {
  const meta = STORE_META[s.id] || { icon: 'grid', desc: '' };
  const tone = FORMAT_TONE[s.format] || 'muted';
  return (
    <div className="data-row">
      <Tile icon={meta.icon} tone={tone} size={32} className="data-row-tile" />
      <div className="data-row-body">
        <div className="data-row-head">
          <span className="data-row-title">{s.label}</span>
          <Badge tone={tone}>{s.format.toUpperCase()}</Badge>
          {s.locked && <Badge tone="err">never leaves this machine</Badge>}
          {s.managed && <Badge tone="ok">auto-managed</Badge>}
        </div>
        {meta.desc && <div className="data-row-desc">{meta.desc}</div>}
        <code className="data-row-path">{s.path}</code>
        {s.items && s.items.length > 0 && (
          <ul className="data-secrets">
            {s.items.map((it) => (
              <li key={it.name}><code>{it.name}</code><span>{it.what}</span></li>
            ))}
          </ul>
        )}
        <div className="data-row-meta">
          {!s.locked && <span><b>{fmtBytes(s.bytes)}</b> on disk</span>}
          {s.count != null && <><span className="meta-sep">·</span><span><b>{fmtInt(s.count)}</b> {s.locked ? 'items held' : s.id === 'devices' ? 'streams' : 'items'}</span></>}
          {s.last_write != null && <><span className="meta-sep">·</span><span>written <b>{ago(s.last_write)}</b></span></>}
          {!s.locked && !s.managed && s.count === 0 && <><span className="meta-sep">·</span><span>empty</span></>}
        </div>
      </div>
      <div className="row-actions">
        {onBrowse && <button className="hub-btn ghost sm" onClick={onBrowse}>Browse<Icon name="arrowRight" /></button>}
        {s.id === 'memory' && (
          <a className="hub-btn ghost sm" href="/api/hub/memory/export" download><Icon name="file" />Export</a>
        )}
      </div>
    </div>
  );
}

// ── Chats ─────────────────────────────────────────────────────────────────────
function ChatsTab() {
  const { data, reload, setData, error } = useResource(() => dataApi.chats());
  const rows = data?.chats ?? null;

  const remove = useCallback(async (c: ChatRow) => {
    if (!window.confirm(`Delete "${c.title}" (${c.messages} message${c.messages === 1 ? '' : 's'})? This is permanent and is recorded in the audit ledger.`)) return;
    setData((d) => (d ? { ...d, chats: d.chats.filter((x) => x.id !== c.id) } : d));
    try { await api.deleteChat(c.id); } catch { /* already gone is fine */ }
    reload();
  }, [reload, setData]);

  const total = rows?.reduce((a, c) => a + c.bytes, 0) ?? 0;
  return (
    <Panel
      title="Conversations"
      subtitle={rows ? `${rows.length} chat${rows.length === 1 ? '' : 's'} · ${fmtBytes(total)} in data/chats.json` : 'data/chats.json'}
    >
      {error && <div className="hub-msg err">{error}</div>}
      {rows == null ? <EmptyState text="Loading…" />
        : rows.length === 0 ? <EmptyState text="No chats yet." />
        : rows.map((c) => (
          <div key={c.id} className="data-row">
            <Tile icon="chats" tone="muted" size={30} className="data-row-tile" />
            <div className="data-row-body">
              <div className="data-row-title">{c.title}</div>
              <div className="data-row-meta">
                <span><b>{fmtInt(c.messages)}</b> message{c.messages === 1 ? '' : 's'}</span>
                <span className="meta-sep">·</span><span>{fmtBytes(c.bytes)}</span>
                <span className="meta-sep">·</span><span>updated <b>{ago(c.updated)}</b></span>
              </div>
            </div>
            <div className="row-actions">
              <a className="hub-btn ghost sm" href={`/api/data/chats/${encodeURIComponent(c.id)}/export`} download title="Export as JSON">JSON</a>
              <a className="hub-btn ghost sm" href={`/api/data/chats/${encodeURIComponent(c.id)}/export?format=md`} download title="Export as Markdown">MD</a>
              <button className="hub-btn ghost sm" title="Delete chat" onClick={() => remove(c)}><Icon name="trash" /></button>
            </div>
          </div>
        ))}
      <Legend
        title="About your chats"
        items={[
          { icon: 'file', term: 'Export', desc: 'JSON or Markdown per chat — messages, attachment names, and generated-image links.' },
          { icon: 'trash', term: 'Delete', desc: <>Permanent, and recorded in the <b>audit ledger</b> as a <code>chat_delete</code> event.</> },
        ]}
      />
    </Panel>
  );
}

// ── Logs ──────────────────────────────────────────────────────────────────────
const LOG_SOURCES: { id: LogName; label: string }[] = [
  { id: 'audit', label: 'Audit' },
  { id: 'performance', label: 'Performance' },
  { id: 'devices', label: 'Devices' },
];

const AUDIT_KINDS: { id: string; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'turn', label: 'Turns' },
  { id: 'memory_recall', label: 'Recalls' },
  { id: 'memory_edit', label: 'Memory edits' },
  { id: 'grant', label: 'Grants' },
  { id: 'chat_delete', label: 'Chat deletes' },
];

// One line of detail per event: every field except the ones already shown as
// their own columns, so nothing in the record is hidden from the owner.
const DETAIL_SKIP = new Set(['ts', 'iso', 'kind', 'category', 'host', 'seq']);
function evtDetail(e: LogEvent): string {
  return Object.entries(e)
    .filter(([k, v]) => !DETAIL_SKIP.has(k) && v != null && v !== '')
    .slice(0, 8)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(' · ');
}

function LogsTab() {
  const [source, setSource] = useState<LogName>('audit');
  const [kind, setKind] = useState('');
  const fetchTail = useCallback(
    () => dataApi.logTail(source, 100, source === 'audit' ? kind : ''),
    [source, kind],
  );
  const tail = useLiveResource(fetchTail, 15000);
  const events = tail.data?.events;

  return (
    <Panel
      title="Log tails"
      subtitle="Newest first, read straight from the append-only files under logs/"
      right={
        <div className="hub-tabs" style={{ borderBottom: 0, marginBottom: 0 }}>
          {LOG_SOURCES.map((s) => (
            <button key={s.id} className={'hub-tab' + (source === s.id ? ' active' : '')} onClick={() => setSource(s.id)}>{s.label}</button>
          ))}
        </div>
      }
    >
      {source === 'audit' && (
        <div className="hub-tabs" style={{ borderBottom: 0, marginBottom: 10 }}>
          {AUDIT_KINDS.map((k) => (
            <button key={k.id} className={'hub-tab' + (kind === k.id ? ' active' : '')} onClick={() => setKind(k.id)}>{k.label}</button>
          ))}
        </div>
      )}
      {events == null ? <EmptyState text={tail.error ? 'Couldn’t read that log.' : 'Loading…'} />
        : events.length === 0 ? <EmptyState text="Nothing recorded here yet." />
        : events.map((e, i) => {
          const tag = String(e.kind || e.category || e.type || '—');
          const m = eventMeta(tag);
          const detail = evtDetail(e);
          return (
            <div key={`${e.ts}-${i}`} className="hist-row">
              <Tile icon={m.icon} tone={m.tone} size={28} className="hist-tile" />
              <div className="hist-body">
                <div className="hist-head">
                  <span className="hist-title">{m.label}</span>
                  <span className="hist-time" title={new Date(e.ts * 1000).toLocaleString()}>{fmtClock(e.ts)}</span>
                </div>
                {detail && <div className="hist-detail" style={{ fontFamily: 'var(--font-mono)', overflowWrap: 'anywhere' }}>{detail}</div>}
              </div>
            </div>
          );
        })}
    </Panel>
  );
}

// ── Maintenance ───────────────────────────────────────────────────────────────
const RETENTION_LABELS: Record<number, string> = {
  0: 'Forever', 30: '1 month', 90: '3 months', 183: '6 months', 365: '1 year', 730: '2 years',
};
function retentionLabel(days: number): string {
  return RETENTION_LABELS[days] || (days > 0 ? `${days} days` : 'Forever');
}

function MaintenanceTab({ stores }: { stores: StoresResponse | null }) {
  const infoRes = useResource(() => dataApi.maintenance());
  const { data: info, setData: setInfo } = infoRes;
  const [busy, setBusy] = useState<'' | 'integrity' | 'vacuum' | 'retention'>('');
  const [message, setMessage] = useState<ActionMsg>(null);
  const [restart, setRestart] = useState(false);

  const runIntegrity = useCallback(async () => {
    setBusy('integrity'); setMessage(null);
    try {
      const r = await dataApi.integrity();
      setInfo((i) => (i ? { ...i, db: r.db } : i));
      setMessage({ ok: r.ok, text: r.ok ? 'Integrity check passed.' : `Integrity check failed: ${r.result.detail}` });
    } catch (e) { setMessage({ ok: false, text: (e as Error).message }); }
    setBusy('');
  }, [setInfo]);

  const runVacuum = useCallback(async () => {
    setBusy('vacuum'); setMessage(null);
    try {
      const r = await dataApi.vacuum();
      setInfo((i) => (i ? { ...i, db: r.db } : i));
      const saved = r.before - r.after;
      setMessage({ ok: true, text: saved > 0 ? `Compacted — reclaimed ${fmtBytes(saved)}.` : 'Compacted — already tight.' });
    } catch (e) { setMessage({ ok: false, text: (e as Error).message }); }
    setBusy('');
  }, [setInfo]);

  const setRetention = useCallback(async (days: number) => {
    setBusy('retention'); setMessage(null);
    try {
      await hub.setRetention(days);
      setInfo((i) => (i ? { ...i, retention: { ...i.retention, days } } : i));
      setRestart(true);
    } catch (e) { setMessage({ ok: false, text: (e as Error).message }); }
    setBusy('');
  }, [setInfo]);

  const db = info?.db;
  const last = db?.last_check;
  return (
    <>
      <ResourceError r={infoRes} label="maintenance status" />
      {restart && (
        <div className="hub-restart">
          <Icon name="refresh" />
          <span>Saved to <b>ava.yaml</b>. Restart Ava to apply the new retention.</span>
        </div>
      )}
      <HubMessage message={message} />

      <Panel title="Data retention" subtitle="How long Ava keeps performance metrics and hardware history.">
        {info == null ? <EmptyState text="Loading…" /> : (
          <>
            <div className="hub-field" style={{ maxWidth: 340 }}>
              <label>Keep data for</label>
              <select className="hub-select" value={info.retention.days} disabled={busy === 'retention'}
                onChange={(e) => setRetention(Number(e.target.value))}>
                {info.retention.choices.map((c) => (
                  <option key={c} value={c}>{retentionLabel(c)}{c === 183 ? ' (default)' : ''}</option>
                ))}
              </select>
            </div>
            <div className="hub-note" style={{ marginTop: 12 }}>
              Applies to <b>performance rollups</b> and <b>hardware history</b>. Chats and memories are never
              auto-deleted — you stay in charge of those. Changing this takes effect after a restart.
            </div>
          </>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel
        title="Database health"
        subtitle={db?.path || 'data/memory.db'}
        right={last ? (last.ok ? <Badge tone="ok">healthy</Badge> : <Badge tone="err">check failed</Badge>) : null}
      >
        {db == null ? <EmptyState text="Loading…" /> : (
          <>
            <dl className="hub-kv">
              <dt>Size on disk</dt><dd>{fmtBytes(db.bytes)}</dd>
              <dt>Reclaimable</dt><dd>{fmtBytes(db.reclaimable)}</dd>
              <dt>Last integrity check</dt><dd>{last ? `${ago(last.ts)} — ${last.ok ? 'ok' : 'failed'}` : 'never'}</dd>
            </dl>
            <div className="hub-btn-row">
              <button className="hub-btn ghost sm" disabled={busy !== ''} onClick={runIntegrity}>
                <Icon name="check" />{busy === 'integrity' ? 'Checking…' : 'Check integrity'}
              </button>
              <button className="hub-btn ghost sm" disabled={busy !== ''} onClick={runVacuum}>
                <Icon name="refresh" />{busy === 'vacuum' ? 'Compacting…' : 'Compact (VACUUM)'}
              </button>
            </div>
          </>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Export everything" subtitle="One archive of all your readable data">
        <p className="hub-note" style={{ border: 0, padding: 0, background: 'none', marginBottom: 12 }}>
          Memories, chats, the audit ledger, and your settings as a single .zip. Secrets and keys are never included.
        </p>
        <a className="hub-btn" href="/api/data/export" download><Icon name="file" />Export archive</a>
      </Panel>

      <div className="hub-section" />
      <Panel title="Backup" subtitle="Your whole Ava is one folder">
        <dl className="hub-kv">
          <dt>AVA_HOME</dt><dd><code>{stores?.home || '…'}</code></dd>
          <dt>Total size</dt><dd>{stores ? fmtBytes(stores.total_bytes) : '…'}</dd>
        </dl>
        <div className="hub-note" style={{ marginTop: 12 }}>
          Copy this folder and you've backed up everything — memories, chats, config, and keys. Restore by
          pointing <b>AVA_HOME</b> at the copy.
        </div>
      </Panel>
    </>
  );
}

// ── Shell ─────────────────────────────────────────────────────────────────────
export function DataView() {
  const [tab, setTab] = useState<TabId>('overview');
  const fetchStores = useCallback(() => dataApi.stores(), []);
  const inv = useLiveResource(fetchStores, 30000);
  // Tolerate a malformed payload (e.g. a proxy error page): render the empty
  // state instead of unmounting the whole view.
  const d = inv.data && Array.isArray(inv.data.stores) ? inv.data : null;

  return (
    <div className="hub view-scroll">
      <div className="hub-inner">
        <div className="hub-head" style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <h2>Data</h2>
            <p>Everything Ava stores on this machine — browse it, export it, delete it.</p>
          </div>
          {d && (
            <span className="data-home-chip" title="All of Ava's data lives under this one folder">
              <Icon name="db" />AVA_HOME · {d.home}
            </span>
          )}
        </div>

        <div className="hub-tabs">
          {TABS.map((t) => (
            <button key={t.id} className={'hub-tab' + (tab === t.id ? ' active' : '')} onClick={() => setTab(t.id)}>
              <Icon name={t.icon} />{t.label}
            </button>
          ))}
        </div>

        {tab === 'overview' && (
          !d ? (
            <EmptyState text={inv.loading ? 'Measuring stores…' : 'Couldn’t load the store inventory.'} />
          ) : (
            <Panel
              title="Stores"
              subtitle={`${d.stores.length} store${d.stores.length === 1 ? '' : 's'} · ${fmtBytes(d.total_bytes)} on disk · metrics kept ${d.retention_days === 0 ? 'forever' : `${d.retention_days} days`}`}
            >
              {d.stores.map((s) => (
                <StoreRow key={s.id} s={s} onBrowse={BROWSE_TAB[s.id] ? () => setTab(BROWSE_TAB[s.id]) : undefined} />
              ))}
            </Panel>
          )
        )}

        {tab === 'memory' && <MemoryPanel />}
        {tab === 'chats' && <ChatsTab />}
        {tab === 'logs' && <LogsTab />}
        {tab === 'maintenance' && <MaintenanceTab stores={d} />}
      </div>
    </div>
  );
}
