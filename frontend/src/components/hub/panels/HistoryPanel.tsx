import { useState } from 'react';
import { Icon } from '../../../lib/icons';
import { ago, EmptyState, Panel } from '../../dashboard/primitives';
import { eventMeta, humanize } from '../events';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import type { AuditEvent } from '../hubApi';
import { Legend } from '../ui/Legend';
import { Tile } from '../ui/Tile';

// History — the flight recorder (durable audit ledger). Event typing (glyph +
// label + tone per kind) is shared with the Data → Logs view via hub/events.ts.
// The kind-specific facts (everything except the label + timestamp), joined for
// the quiet meta line under the title.
function eventDetail(e: AuditEvent): string {
  const s = (v: unknown) => (v == null || v === '' ? '' : String(v));
  const bits: string[] = [];
  switch (e.kind) {
    case 'turn':
      bits.push(s(e.status), s(e.model), e.duration_s ? `${e.duration_s}s` : '',
        e.tools?.length ? e.tools.join(', ') : 'no tools');
      break;
    case 'code_change':
      bits.push(humanize(s(e.outcome)), s(e.commit),
        e.paths?.length ? `${e.paths.length} file${e.paths.length === 1 ? '' : 's'}` : '',
        e.approved_by ? `approved by ${e.approved_by}` : '');
      break;
    case 'egress':
      bits.push(`${s(e.connector)}/${s(e.tool)}`, s(e.status));
      break;
    case 'memory_recall':
      bits.push(`${s(e.count) || '?'} item(s) folded into a turn`);
      break;
    case 'memory_distill':
      bits.push(`${s(e.added) || '?'} new fact(s) from ${s(e.messages) || '?'} messages`);
      break;
    case 'memory_edit':
      bits.push(`${s(e.action) || 'edit'} · item #${s(e.id)}`);
      break;
    case 'grant': case 'revoke':
      bits.push(s(e.connector), s(e.tool) || s(e.action));
      break;
    case 'route':
      bits.push(s(e.label), s(e.tier));
      break;
    default:
      bits.push(humanize(s(e.outcome)), s(e.status));
  }
  return bits.filter(Boolean).join(' · ');
}

// Client-side categories — a single audit kind is too fine-grained to filter
// well server-side (Memory spans recall/distill/edit; Permissions span
// grant/revoke/approval), so we fetch the whole tail once and group here.
const HISTORY_CATS: { id: string; label: string; kinds: string[] }[] = [
  { id: '', label: 'All', kinds: [] },
  { id: 'turn', label: 'Chats', kinds: ['turn'] },
  { id: 'code', label: 'Self-edits', kinds: ['code_change'] },
  { id: 'memory', label: 'Memory', kinds: ['memory_recall', 'memory_distill', 'memory_edit'] },
  { id: 'perms', label: 'Permissions', kinds: ['grant', 'revoke', 'approval'] },
  { id: 'system', label: 'System', kinds: ['route', 'job', 'egress', 'data_export', 'data_maintenance'] },
];

export function HistoryPanel() {
  const [cat, setCat] = useState('');
  const { data: audit, error: err, reload: load } = useResource(() => hub.audit(300));
  const events = audit?.events ?? null;

  const kinds = HISTORY_CATS.find((c) => c.id === cat)?.kinds ?? [];
  const shown = events && (kinds.length ? events.filter((e) => kinds.includes(e.kind)) : events);
  const count = (c: { id: string; kinds: string[] }) =>
    !events ? 0 : c.kinds.length ? events.filter((e) => c.kinds.includes(e.kind)).length : events.length;

  return (
    <Panel
      title="Flight recorder"
      subtitle="A durable, append-only record of everything the agent did. Survives restarts and the agent can't rewrite it (logs/audit.jsonl)."
      right={<button className="hub-btn ghost sm" onClick={load}><Icon name="refresh" />Refresh</button>}
    >
      <div className="hub-tabs" style={{ marginBottom: 14, borderBottom: 0 }}>
        {HISTORY_CATS.map((f) => (
          <button key={f.id} className={'hub-tab' + (cat === f.id ? ' active' : '')} onClick={() => setCat(f.id)}>
            {f.label}{events && <span className="hist-tab-n">{count(f)}</span>}
          </button>
        ))}
      </div>
      {err && <div className="hub-msg err">{err}</div>}
      {events == null ? <EmptyState text="Loading…" />
        : shown && shown.length === 0 ? (
          <EmptyState text={cat ? 'No events in this category yet.' : 'No events recorded yet. Actions will appear here as the agent works.'} />
        ) : (
          <div>
            {shown!.map((e, i) => {
              const m = eventMeta(e.kind);
              const detail = eventDetail(e);
              return (
                <div key={i} className="hist-row">
                  <Tile icon={m.icon} tone={m.tone} size={28} className="hist-tile" />
                  <div className="hist-body">
                    <div className="hist-head">
                      <span className="hist-title">{m.label}</span>
                      <span className="hist-time" title={new Date(e.ts * 1000).toLocaleString()}>{ago(e.ts)}</span>
                    </div>
                    {e.request && <div className="hist-req">“{e.request}”</div>}
                    {detail && <div className="hist-detail">{detail}</div>}
                    {e.error && <div className="hist-detail err">error: {e.error}</div>}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      <Legend
        title="What's in the ledger"
        items={[
          { icon: 'chats', term: 'Chats', desc: 'Every message Ava answered — the model, tools used, and how long it took.' },
          { icon: 'code', term: 'Self-edits', desc: 'Changes the agent made to its own code, the commit, and who approved it.' },
          { icon: 'db', term: 'Memory', desc: 'Recalls folded into a reply, plus facts distilled from chats or edited by you.' },
          { icon: 'lock', term: 'Permissions', desc: 'Connector grants, revokes, and one-off approvals.' },
          { icon: 'activity', term: 'System', desc: 'Intent routing, media jobs, and data export / maintenance.' },
        ]}
        foot={<div>Append-only — nothing here can be altered after the fact. The full audit with export lives on the <b>Data</b> page.</div>}
      />
    </Panel>
  );
}
