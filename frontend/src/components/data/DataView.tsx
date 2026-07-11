import { useCallback, useState } from 'react';
import { Icon } from '../../lib/icons';
import { EmptyState, StatCard, ago, fmtInt } from '../dashboard/primitives';
import { useLiveResource } from '../../hooks/useLive';
import { MemoryPanel } from '../hub/MemoryPanel';
import { dataApi } from './dataApi';
import type { DataStore } from './dataApi';

// Data — the transparency page: everything Ava keeps on disk, one card per
// store, with the memory browser as the flagship tab. The backend returns
// facts; the owner-facing copy lives here (same split as dashboard/metrics.ts).

type TabId = 'overview' | 'memory';

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'grid' },
  { id: 'memory', label: 'Memory', icon: 'db' },
];

const STORE_META: Record<string, { icon: string; desc: string }> = {
  memory: { icon: 'db', desc: 'Distilled facts Ava has learned about you, plus indexed document chunks. Recalled into chats; every recall and edit lands in the audit ledger.' },
  chats: { icon: 'chats', desc: 'Every conversation — messages, attachments, and which model answered. Open and delete chats from the sidebar.' },
  audit: { icon: 'file', desc: 'Ava’s flight recorder: turns, memory recalls and edits, connector grants. Append-only; browsable under Setup → History.' },
  performance: { icon: 'chart', desc: 'Generation throughput, latency, and energy per turn, with hourly and daily rollups behind the Vitals charts.' },
  hw_history: { icon: 'gauge', desc: 'GPU, memory, and CPU samples at minute and hour resolution — the long-range series behind the Vitals gauges.' },
  devices: { icon: 'activity', desc: 'Sensor readings and events from connected devices, one rotated stream per connector.' },
  media_gen: { icon: 'image', desc: 'Images and video Ava has generated. Nothing here is auto-deleted yet.' },
  uploads: { icon: 'attach', desc: 'Files you’ve shared with Ava in chat. Document text is indexed into Memory; the originals stay here.' },
  secrets: { icon: 'lock', desc: 'Login password, session key, internal tokens, and backend API keys. Listed for transparency — never displayed, exported, or browsable.' },
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
                  <StoreCard key={s.id} s={s} onBrowse={s.id === 'memory' ? () => setTab('memory') : undefined} />
                ))}
              </div>
            </>
          )
        )}

        {tab === 'memory' && <MemoryPanel />}
      </div>
    </div>
  );
}
