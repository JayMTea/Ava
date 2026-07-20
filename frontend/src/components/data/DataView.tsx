import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../lib/icons';
import { EmptyState, Panel, StatCard, ago, fmtClock, fmtInt } from '../dashboard/primitives';
import { useLiveResource } from '../../hooks/useLive';
import { MemoryPanel } from '../hub/MemoryPanel';
import { api } from '../../lib/api';
import { hub } from '../hub/hubApi';
import { dataApi } from './dataApi';
import type { ChatRow, DataStore, LogEvent, LogName, MaintenanceInfo, StoresResponse } from './dataApi';

// Data — the transparency page: everything Ava keeps on disk, one card per
// store, with the memory browser as the flagship tab. The backend returns
// facts; the owner-facing copy lives here (same split as dashboard/metrics.ts).

type TabId = 'overview' | 'memory' | 'chats' | 'logs' | 'maintenance';

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'grid' },
  { id: 'memory', label: 'Memory', icon: 'db' },
  { id: 'chats', label: 'Chats', icon: 'chats' },
  { id: 'logs', label: 'Logs', icon: 'activity' },
  { id: 'maintenance', label: 'Maintenance', icon: 'sliders' },
];

// Which tab a store card's "Browse →" opens.
const BROWSE_TAB: Record<string, TabId> = {
  memory: 'memory', chats: 'chats', audit: 'logs', performance: 'logs', devices: 'logs',
};

const STORE_META: Record<string, { icon: string; desc: string }> = {
  memory: { icon: 'db', desc: 'Distilled facts Ava has learned about you, plus indexed document chunks. Recalled into chats; every recall and edit lands in the audit ledger.' },
  chats: { icon: 'chats', desc: 'Every conversation — messages, attachments, and which model answered. Open and delete chats from the sidebar.' },
  audit: { icon: 'file', desc: 'Ava’s flight recorder: turns, memory recalls and edits, connector grants. Append-only; browsable under Setup → History.' },
  performance: { icon: 'chart', desc: 'Generation throughput, latency, and energy per turn, with hourly and daily rollups behind the Vitals charts.' },
  hw_history: { icon: 'gauge', desc: 'GPU, memory, and CPU samples at minute and hour resolution — the long-range series behind the Vitals gauges.' },
  devices: { icon: 'activity', desc: 'Sensor readings and events from connected devices, one rotated stream per connector.' },
  media_gen: { icon: 'image', desc: 'Images and video Ava has generated. Nothing here is auto-deleted yet.' },
  uploads: { icon: 'attach', desc: 'Files you’ve shared with Ava in chat. Document text is indexed into Memory; the originals stay here.' },
  secrets: { icon: 'lock', desc: 'Login password, session key, internal tokens, and backend API keys. Names are listed for transparency — the values are never displayed, exported, or browsable.' },
};

const FORMAT_TONE: Record<DataStore['format'], string> = {
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

function StoreCard({ s, onBrowse }: { s: DataStore; onBrowse?: () => void }) {
  const meta = STORE_META[s.id] || { icon: 'grid', desc: '' };
  const showEmpty = !s.locked && !s.managed && s.count === 0;
  const hasFoot = Boolean(onBrowse) || s.id === 'memory' || s.locked || s.managed || showEmpty;
  return (
    <article className="data-store">
      <div className="data-store-head">
        <span className="data-store-ic"><Icon name={meta.icon} /></span>
        <span className="data-store-name">{s.label}</span>
        <span className={`hub-badge ${FORMAT_TONE[s.format] || ''}`} style={{ marginLeft: 'auto' }}>
          <i />{s.format.toUpperCase()}
        </span>
      </div>
      {meta.desc && <p className="data-store-desc">{meta.desc}</p>}
      <div className="data-store-path">{s.path}</div>
      {s.items && s.items.length > 0 && (
        <ul className="data-secrets">
          {s.items.map((it) => (
            <li key={it.name}>
              <code>{it.name}</code>
              <span>{it.what}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="data-store-meta">
        {!s.locked && <span><b>{fmtBytes(s.bytes)}</b> on disk</span>}
        {s.count != null && <span><b>{fmtInt(s.count)}</b> {s.locked ? 'items held' : s.id === 'devices' ? 'streams' : 'items'}</span>}
        {s.last_write != null && <span>written <b>{ago(s.last_write)}</b></span>}
      </div>
      {hasFoot && (
        <div className="data-store-foot">
          {onBrowse && <button className="db-linkbtn" onClick={onBrowse}>Browse →</button>}
          {s.id === 'memory' && (
            <a className="hub-btn ghost sm" href="/api/hub/memory/export" download style={{ marginLeft: 'auto' }}>
              <Icon name="file" />Export JSON
            </a>
          )}
          {s.locked && <span className="db-pill pill-err"><i className="db-dot" />never leaves this machine</span>}
          {s.managed && <span className="db-pill pill-ok"><i className="db-dot" />auto-managed</span>}
          {showEmpty && <span className="db-pill pill-muted"><i className="db-dot" />empty</span>}
        </div>
      )}
    </article>
  );
}

function ChatsTab() {
  const [rows, setRows] = useState<ChatRow[] | null>(null);
  const [msg, setMsg] = useState('');
  const load = useCallback(() => {
    setMsg('');
    dataApi.chats().then((r) => setRows(r.chats)).catch((e) => setMsg((e as Error).message));
  }, []);
  useEffect(() => { load(); }, [load]);

  const remove = useCallback(async (c: ChatRow) => {
    if (!window.confirm(`Delete "${c.title}" (${c.messages} message${c.messages === 1 ? '' : 's'})? This is permanent and is recorded in the audit ledger.`)) return;
    setRows((xs) => xs?.filter((x) => x.id !== c.id) ?? null);
    try { await api.deleteChat(c.id); } catch { /* already gone is fine */ }
    load();
  }, [load]);

  const total = rows?.reduce((a, c) => a + c.bytes, 0) ?? 0;
  return (
    <>
      <Panel
        title="Conversations"
        subtitle={rows ? `${rows.length} chat${rows.length === 1 ? '' : 's'} · ${fmtBytes(total)} in data/chats.json` : 'data/chats.json'}
        pad={false}
      >
        {msg && <div className="hub-msg err" style={{ margin: 12 }}>{msg}</div>}
        {rows == null ? <EmptyState text="Loading…" />
          : rows.length === 0 ? <EmptyState text="No chats yet." />
          : (
            <div className="db-table-wrap">
              <table className="db-table">
                <thead><tr><th>Chat</th><th>Messages</th><th>Updated</th><th>Size</th><th></th></tr></thead>
                <tbody>
                  {rows.map((c) => (
                    <tr key={c.id}>
                      <td style={{ maxWidth: 340, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.title}</td>
                      <td>{fmtInt(c.messages)}</td>
                      <td>{ago(c.updated)}</td>
                      <td>{fmtBytes(c.bytes)}</td>
                      <td>
                        <span style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
                          <a className="hub-btn ghost sm" href={`/api/data/chats/${encodeURIComponent(c.id)}/export`} download title="Export as JSON">JSON</a>
                          <a className="hub-btn ghost sm" href={`/api/data/chats/${encodeURIComponent(c.id)}/export?format=md`} download title="Export as Markdown">MD</a>
                          <button className="hub-btn ghost sm" title="Delete chat" onClick={() => remove(c)}><Icon name="trash" /></button>
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
      </Panel>
      <div className="hub-note data-note">
        <Icon name="info" />
        <span>Deleting a chat is permanent and is recorded in the <b>audit ledger</b>. Exports include messages, attachment names, and generated-image links.</span>
      </div>
    </>
  );
}

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

const KIND_TONE: Record<string, string> = {
  turn: 'pill-ok', memory_recall: 'pill-muted', memory_distill: 'pill-muted',
  memory_edit: 'pill-warn', grant: 'pill-warn', revoke: 'pill-err', chat_delete: 'pill-err',
};

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
      pad={false}
      right={
        <div className="db-seg">
          {LOG_SOURCES.map((s) => (
            <button key={s.id} className={'db-seg-btn' + (source === s.id ? ' on' : '')} onClick={() => setSource(s.id)}>{s.label}</button>
          ))}
        </div>
      }
    >
      {source === 'audit' && (
        <div style={{ padding: '10px 12px 0' }}>
          <div className="db-seg">
            {AUDIT_KINDS.map((k) => (
              <button key={k.id} className={'db-seg-btn' + (kind === k.id ? ' on' : '')} onClick={() => setKind(k.id)}>{k.label}</button>
            ))}
          </div>
        </div>
      )}
      {events == null ? <EmptyState text={tail.error ? 'Couldn’t read that log.' : 'Loading…'} />
        : events.length === 0 ? <EmptyState text="Nothing recorded here yet." />
        : (
          <div className="db-table-wrap">
            <table className="db-table">
              <thead><tr><th>Time</th><th>Kind</th><th>Detail</th></tr></thead>
              <tbody>
                {events.map((e, i) => {
                  const tag = String(e.kind || e.category || e.type || '—');
                  return (
                    <tr key={`${e.ts}-${i}`}>
                      <td style={{ whiteSpace: 'nowrap' }}>{fmtClock(e.ts)}</td>
                      <td><span className={`db-pill ${KIND_TONE[tag] || 'pill-muted'}`}><i className="db-dot" />{tag}</span></td>
                      <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)', color: 'var(--muted)', overflowWrap: 'anywhere' }}>{evtDetail(e)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
    </Panel>
  );
}

function retentionLabel(days: number): string {
  if (days === 0) return 'Forever';
  if (days === 365) return '1 y';
  if (days === 730) return '2 y';
  return `${days} d`;
}

function MaintenanceTab({ stores }: { stores: StoresResponse | null }) {
  const [info, setInfo] = useState<MaintenanceInfo | null>(null);
  const [busy, setBusy] = useState<'' | 'integrity' | 'vacuum' | 'retention'>('');
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [restart, setRestart] = useState(false);

  useEffect(() => {
    dataApi.maintenance().then(setInfo).catch(() => {});
  }, []);

  const runIntegrity = useCallback(async () => {
    setBusy('integrity'); setMsg(null);
    try {
      const r = await dataApi.integrity();
      setInfo((i) => (i ? { ...i, db: r.db } : i));
      setMsg({ ok: r.ok, text: r.ok ? 'Integrity check passed.' : `Integrity check failed: ${r.result.detail}` });
    } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
    setBusy('');
  }, []);

  const runVacuum = useCallback(async () => {
    setBusy('vacuum'); setMsg(null);
    try {
      const r = await dataApi.vacuum();
      setInfo((i) => (i ? { ...i, db: r.db } : i));
      const saved = r.before - r.after;
      setMsg({ ok: true, text: saved > 0 ? `Compacted — reclaimed ${fmtBytes(saved)}.` : 'Compacted — already tight.' });
    } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
    setBusy('');
  }, []);

  const setRetention = useCallback(async (days: number) => {
    setBusy('retention'); setMsg(null);
    try {
      await hub.setRetention(days);
      setInfo((i) => (i ? { ...i, retention: { ...i.retention, days } } : i));
      setRestart(true);
    } catch (e) { setMsg({ ok: false, text: (e as Error).message }); }
    setBusy('');
  }, []);

  const db = info?.db;
  const last = db?.last_check;
  return (
    <>
      {restart && (
        <div className="hub-restart">
          <Icon name="refresh" />
          <span>Saved to <b>ava.yaml</b>. Restart Ava to apply the new retention.</span>
        </div>
      )}
      {msg && <div className={`hub-msg ${msg.ok ? 'ok' : 'err'}`}>{msg.text}</div>}
      <div className="data-maint">

        <Panel title="Retention" subtitle="How long metrics history is kept">
          {info == null ? <EmptyState text="Loading…" /> : (
            <>
              <div className="db-seg">
                {info.retention.choices.map((c) => (
                  <button
                    key={c}
                    className={'db-seg-btn' + (info.retention.days === c ? ' on' : '')}
                    disabled={busy === 'retention'}
                    onClick={() => setRetention(c)}
                  >{retentionLabel(c)}</button>
                ))}
              </div>
              <div className="hub-note data-note" style={{ marginTop: 14 }}>
                <Icon name="info" />
                <span>Applies to <b>performance rollups</b> and <b>hardware history</b>. Chats and memories are never auto-deleted — you stay in charge of those.</span>
              </div>
            </>
          )}
        </Panel>

        <Panel
          title="Database health"
          subtitle={db?.path || 'data/memory.db'}
          right={last && (
            <span className={`db-pill ${last.ok ? 'pill-ok' : 'pill-err'}`}>
              <i className="db-dot" />{last.ok ? 'healthy' : 'check failed'}
            </span>
          )}
        >
          {db == null ? <EmptyState text="Loading…" /> : (
            <>
              <div className="data-kv"><span>Size on disk</span><b>{fmtBytes(db.bytes)}</b></div>
              <div className="data-kv"><span>Reclaimable</span><b>{fmtBytes(db.reclaimable)}</b></div>
              <div className="data-kv"><span>Last integrity check</span><b>{last ? `${ago(last.ts)} — ${last.ok ? 'ok' : 'failed'}` : 'never'}</b></div>
              <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
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

        <Panel title="Export everything" subtitle="One archive of all your readable data">
          <p className="data-store-desc" style={{ marginBottom: 12 }}>
            Memories, chats, the audit ledger, and your settings as a single .zip.
            Secrets and keys are never included.
          </p>
          <a className="hub-btn" href="/api/data/export" download>
            <Icon name="file" />Export archive
          </a>
        </Panel>

        <Panel title="Backup" subtitle="Your whole Ava is one folder">
          <div className="data-kv"><span>AVA_HOME</span><b style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--fs-xs)' }}>{stores?.home || '…'}</b></div>
          <div className="data-kv"><span>Total size</span><b>{stores ? fmtBytes(stores.total_bytes) : '…'}</b></div>
          <div className="hub-note data-note" style={{ marginTop: 12 }}>
            <Icon name="info" />
            <span>Copy this folder and you've backed up everything — memories, chats, config, and keys. Restore by pointing <b>AVA_HOME</b> at the copy.</span>
          </div>
        </Panel>

      </div>
    </>
  );
}

export function DataView() {
  const [tab, setTab] = useState<TabId>('overview');
  const fetchStores = useCallback(() => dataApi.stores(), []);
  const inv = useLiveResource(fetchStores, 30000);
  // Tolerate a malformed payload (e.g. a proxy error page): render the empty
  // state instead of unmounting the whole view.
  const d = inv.data && Array.isArray(inv.data.stores) ? inv.data : null;
  const by = (id: string) => d?.stores.find((s) => s.id === id);

  const mediaFiles = (by('media_gen')?.count || 0) + (by('uploads')?.count || 0);
  const retention = d?.retention_days ?? null;

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
            <>
              <div className="db-kpis data-kpis">
                <StatCard label="On disk" value={fmtBytes(d.total_bytes)} tone="accent" hint={`across ${d.stores.length} stores`} />
                <StatCard label="Memories" value={fmtInt(by('memory')?.count)} hint={`${fmtInt(by('memory')?.pinned ?? 0)} pinned`} />
                <StatCard label="Chats" value={fmtInt(by('chats')?.count)} hint={`${fmtInt(by('chats')?.messages ?? 0)} messages`} />
                <StatCard label="Media files" value={fmtInt(mediaFiles)} hint="generated + uploads" />
                <StatCard label="Audit events" value={fmtInt(by('audit')?.count)} hint="all time" />
                <StatCard
                  label="Retention" tone="ok"
                  value={retention === 0 ? '∞' : fmtInt(retention)}
                  unit={retention === 0 ? undefined : 'd'}
                  hint="metrics history"
                />
              </div>

              <h3 className="dash-sec-h">Stores <span className="dash-sec-count">{d.stores.length}</span></h3>
              <div className="data-stores">
                {d.stores.map((s) => (
                  <StoreCard key={s.id} s={s} onBrowse={BROWSE_TAB[s.id] ? () => setTab(BROWSE_TAB[s.id]) : undefined} />
                ))}
              </div>
            </>
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
