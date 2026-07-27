import { useState } from 'react';
import { Icon } from '../../lib/icons';
import { RowMenu, type MenuAction } from '../../lib/RowMenu';
import { EmptyState, Panel } from '../dashboard/primitives';
import { useAction, useResource } from './hooks';
import { HubMessage } from './ui/HubMessage';
import { Legend } from './ui/Legend';
import { Tile } from './ui/Tile';
import { hub } from './hubApi';
import type { MemoryItem } from './hubApi';

// Memory — everything Ava remembers long-term, readable and correctable.
// Facts come from the learning cycle's distiller or manual entry; doc chunks
// from uploads. Recalls that influenced a turn are in History (memory_recall).
// Shared by Setup → Memory and the Data page, so both stay one implementation.
//
// Each row is typed by where it came from — the same "identity glyph + text +
// quiet meta line + one visible action + ⋯ menu" grammar as a connector row, so
// the two pages read as one system. Colour is spent only on the pinned state.
function memoryType(m: MemoryItem): { icon: string; label: string } {
  if (m.kind === 'doc' || m.source.startsWith('upload:'))
    return { icon: 'file', label: m.source.startsWith('upload:') ? m.source.slice(7) : 'document' };
  if (m.source === 'distilled') return { icon: 'sparkles', label: 'learned from chats' };
  if (m.source === 'manual') return { icon: 'user', label: 'added by you' };
  return { icon: 'db', label: m.source || m.kind };
}

export function MemoryPanel() {
  const [q, setQ] = useState('');
  const [query, setQuery] = useState(''); // committed search
  const [kind, setKind] = useState('');
  const [newFact, setNewFact] = useState('');
  const [editId, setEditId] = useState<number | null>(null);
  const [editText, setEditText] = useState('');
  const { data: mem, reload, setData: setMem, error } = useResource(() => hub.memory(query, kind), [query, kind]);
  const { busy, message, run } = useAction();

  const items = mem?.items ?? null;
  const counts = mem?.counts ?? null;
  const enabled = mem?.enabled ?? true;
  const patchItems = (fn: (xs: MemoryItem[]) => MemoryItem[]) =>
    setMem((m) => (m ? { ...m, items: fn(m.items) } : m));

  const add = () => {
    const text = newFact.trim();
    if (!text) return;
    run(async () => {
      const r = await hub.addMemory(text);
      if (r.error) return r.error;
      setNewFact(''); reload();
    });
  };

  const saveEdit = () => {
    if (editId == null) return;
    run(async () => {
      const r = await hub.updateMemory(editId, { text: editText });
      if (r.error) return r.error;
      setEditId(null); reload();
    });
  };

  const togglePin = async (m: MemoryItem) => {
    patchItems((xs) => xs.map((x) => (x.id === m.id ? { ...x, pinned: !m.pinned } : x)));
    try { await hub.updateMemory(m.id, { pinned: !m.pinned }); } catch { reload(); }
  };

  const remove = async (m: MemoryItem) => {
    patchItems((xs) => xs.filter((x) => x.id !== m.id));
    try { await hub.deleteMemory(m.id); } catch { /* already gone is fine */ }
    reload();
  };

  const FILTERS: { id: string; label: string }[] = [
    { id: '', label: 'All' }, { id: 'fact', label: 'Facts' }, { id: 'doc', label: 'Documents' },
  ];

  return (
    <>
      {!enabled && (
        <div className="hub-restart">
          <Icon name="info" />
          <span>Memory is off (<b>features.memory: false</b> in ava.yaml) — nothing new is saved or recalled. The store below is still browsable.</span>
        </div>
      )}
      <Panel
        title="Teach Ava"
        subtitle="Add a fact she should remember — it's recalled whenever a chat message looks related."
      >
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <input
            className="hub-input" style={{ flex: '1 1 260px' }}
            placeholder="e.g. My workshop machine is the Jetson in the garage"
            value={newFact}
            onChange={(e) => setNewFact(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') add(); }}
          />
          <button className="hub-btn" onClick={add} disabled={busy || !newFact.trim()}>
            <Icon name="plus" />Remember
          </button>
        </div>
      </Panel>

      <div className="hub-section" />

      <Panel
        title="What Ava remembers"
        subtitle="Her long-term memory: facts distilled from chats plus uploaded-document content. Everything is yours to correct, pin, delete, or export — and every recall she uses in a reply is logged under History."
        right={
          <span style={{ display: 'flex', gap: 8 }}>
            <a className="hub-btn ghost sm" href="/api/hub/memory/export" download>
              <Icon name="file" />Export
            </a>
            <button className="hub-btn ghost sm" onClick={reload}><Icon name="refresh" />Refresh</button>
          </span>
        }
      >
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
          <div className="hub-tabs" style={{ borderBottom: 0, marginBottom: 0 }}>
            {FILTERS.map((f) => (
              <button key={f.id} className={'hub-tab' + (kind === f.id ? ' active' : '')} onClick={() => setKind(f.id)}>{f.label}</button>
            ))}
          </div>
          <input
            className="hub-input" style={{ flex: '1 1 180px', maxWidth: 320 }}
            placeholder="Search memory…"
            value={q}
            onChange={(e) => { setQ(e.target.value); if (!e.target.value.trim()) setQuery(''); }}
            onKeyDown={(e) => { if (e.key === 'Enter') setQuery(q.trim()); }}
          />
          {counts && (
            <span style={{ color: 'var(--muted)', fontSize: 12.5, marginLeft: 'auto' }}>
              {counts.facts} fact{counts.facts === 1 ? '' : 's'} · {counts.doc_chunks} doc chunk{counts.doc_chunks === 1 ? '' : 's'}
            </span>
          )}
        </div>
        {error && <div className="hub-msg err">{error}</div>}
        <HubMessage message={message} />
        {items == null ? <EmptyState text="Loading…" />
          : items.length === 0 ? (
            <EmptyState text={query
              ? 'No memories match that search.'
              : 'Nothing here yet. Facts appear as the learning cycle distills your chats; documents as you upload them; or teach her one above.'} />
          ) : (
            <div>
              {items.map((m) => {
                const t = memoryType(m);
                const editing = editId === m.id;
                const menu: MenuAction[] = [
                  ...(m.kind === 'fact'
                    ? [{ label: 'Edit', icon: 'pencil', onClick: () => { setEditId(m.id); setEditText(m.text); } }]
                    : []),
                  { label: 'Copy text', icon: 'copy', onClick: () => navigator.clipboard?.writeText(m.text) },
                  // Confirmed like every other destructive action here (chats,
                  // connectors, models). A memory is distilled from past
                  // conversations, so there is nothing to re-derive it from.
                  { label: 'Forget', icon: 'trash', danger: true,
                    onClick: () => {
                      if (window.confirm('Forget this memory? Ava cannot recover it.')) remove(m);
                    } },
                ];
                return (
                  <div key={m.id} className="mem-row">
                    <Tile icon={m.pinned ? 'pin' : t.icon} tone={m.pinned ? 'accent' : 'muted'} size={30} className="mem-tile" />
                    <div className="mem-body">
                      {editing ? (
                        <div className="mem-edit">
                          <input
                            className="hub-input" style={{ flex: '1 1 240px' }}
                            value={editText} autoFocus
                            onChange={(e) => setEditText(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') saveEdit(); if (e.key === 'Escape') setEditId(null); }}
                          />
                          <button className="hub-btn sm" onClick={saveEdit} disabled={busy}><Icon name="check" />Save</button>
                          <button className="hub-btn ghost sm" onClick={() => setEditId(null)}><Icon name="close" />Cancel</button>
                        </div>
                      ) : (
                        <>
                          <div className="mem-text">{m.text}</div>
                          <div className="mem-meta">
                            <span>{t.label}</span>
                            <span className="meta-sep">·</span>
                            <span>{new Date((m.updated || m.created) * 1000).toLocaleDateString()}</span>
                            {m.pinned && (
                              <><span className="meta-sep">·</span>
                              <span className="mem-pinned"><Icon name="pin" />pinned</span></>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                    {!editing && (
                      <div className="row-actions">
                        <button
                          className={'hub-btn ghost sm mem-pin' + (m.pinned ? ' on' : '')}
                          aria-pressed={m.pinned}
                          title={m.pinned ? 'Unpin' : 'Pin — always ranks first in recall'}
                          onClick={() => togglePin(m)}
                        >
                          <Icon name="pin" />{m.pinned ? 'Pinned' : 'Pin'}
                        </button>
                        <RowMenu actions={menu} />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        <Legend
          title="How memory works"
          items={[
            { icon: 'sparkles', term: 'Learned', desc: 'Facts the learning cycle distils from your chats automatically.' },
            { icon: 'user', term: 'Added by you', desc: 'Facts you typed in above — kept verbatim.' },
            { icon: 'file', term: 'Documents', desc: 'Chunks of files you uploaded, searched the same way as facts.' },
            { icon: 'pin', term: 'Pin', desc: 'Keeps a memory ranked first whenever it\'s relevant to a message.' },
          ]}
          foot={<div>Every recall Ava uses in a reply is logged under <b>History</b>. <b>Export</b> downloads everything as JSON.</div>}
        />
      </Panel>
    </>
  );
}
