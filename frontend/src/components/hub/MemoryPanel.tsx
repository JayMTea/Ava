import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../lib/icons';
import { EmptyState, Panel } from '../dashboard/primitives';
import { hub } from './hubApi';
import type { MemoryCounts, MemoryItem } from './hubApi';

// Memory — everything Ava remembers long-term, readable and correctable.
// Facts come from the learning cycle's distiller or manual entry; doc chunks
// from uploads. Recalls that influenced a turn are in History (memory_recall).
// Shared by Setup → Memory and the Data page, so both stay one implementation.
function memorySourceLabel(m: MemoryItem): string {
  if (m.source === 'distilled') return 'learned from chats';
  if (m.source === 'manual') return 'added by you';
  if (m.source.startsWith('upload:')) return m.source.slice(7);
  return m.source || m.kind;
}

export function MemoryPanel() {
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [counts, setCounts] = useState<MemoryCounts | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [q, setQ] = useState('');
  const [query, setQuery] = useState(''); // committed search
  const [kind, setKind] = useState('');
  const [newFact, setNewFact] = useState('');
  const [editId, setEditId] = useState<number | null>(null);
  const [editText, setEditText] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const load = useCallback(() => {
    setMsg('');
    hub.memory(query, kind).then((r) => {
      setItems(r.items); setCounts(r.counts); setEnabled(r.enabled);
    }).catch((e) => setMsg((e as Error).message));
  }, [query, kind]);
  useEffect(() => { load(); }, [load]);

  const add = useCallback(async () => {
    const text = newFact.trim();
    if (!text) return;
    setBusy(true); setMsg('');
    try {
      const r = await hub.addMemory(text);
      if (r.error) setMsg(r.error);
      else { setNewFact(''); load(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [newFact, load]);

  const saveEdit = useCallback(async () => {
    if (editId == null) return;
    setBusy(true); setMsg('');
    try {
      const r = await hub.updateMemory(editId, { text: editText });
      if (r.error) setMsg(r.error);
      else { setEditId(null); load(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [editId, editText, load]);

  const togglePin = useCallback(async (m: MemoryItem) => {
    setItems((xs) => xs?.map((x) => (x.id === m.id ? { ...x, pinned: !m.pinned } : x)) ?? null);
    try { await hub.updateMemory(m.id, { pinned: !m.pinned }); } catch { load(); }
  }, [load]);

  const remove = useCallback(async (m: MemoryItem) => {
    setItems((xs) => xs?.filter((x) => x.id !== m.id) ?? null);
    try { await hub.deleteMemory(m.id); } catch { /* already gone is fine */ }
    load();
  }, [load]);

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

      <Panel
        title="What Ava remembers"
        subtitle="Her long-term memory: facts distilled from chats plus uploaded-document content. Everything is yours to correct, pin, delete, or export — and every recall she uses in a reply is logged under History."
        right={
          <span style={{ display: 'flex', gap: 8 }}>
            <a className="hub-btn ghost sm" href="/api/hub/memory/export" download>
              <Icon name="file" />Export
            </a>
            <button className="hub-btn ghost sm" onClick={load}><Icon name="refresh" />Refresh</button>
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
        {msg && <div className="hub-msg err">{msg}</div>}
        {items == null ? <EmptyState text="Loading…" />
          : items.length === 0 ? (
            <EmptyState text={query
              ? 'No memories match that search.'
              : 'Nothing here yet. Facts appear as the learning cycle distills your chats; documents as you upload them; or teach her one above.'} />
          ) : (
            <div>
              {items.map((m) => (
                <div key={m.id} className="hub-row">
                  <div className="hub-row-main" style={{ minWidth: 0 }}>
                    {editId === m.id ? (
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <input
                          className="hub-input" style={{ flex: '1 1 260px' }}
                          value={editText} autoFocus
                          onChange={(e) => setEditText(e.target.value)}
                          onKeyDown={(e) => { if (e.key === 'Enter') saveEdit(); if (e.key === 'Escape') setEditId(null); }}
                        />
                        <button className="hub-btn sm" onClick={saveEdit} disabled={busy}><Icon name="check" />Save</button>
                        <button className="hub-btn ghost sm" onClick={() => setEditId(null)}><Icon name="close" />Cancel</button>
                      </div>
                    ) : (
                      <>
                        <div className="hub-row-title" style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                          {m.pinned && <span style={{ color: 'var(--accent)', display: 'inline-flex', flexShrink: 0, alignSelf: 'center' }}><Icon name="pin" /></span>}
                          <span style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{m.text}</span>
                        </div>
                        <div className="hub-row-sub">
                          {memorySourceLabel(m)} · {new Date((m.updated || m.created) * 1000).toLocaleDateString()}
                        </div>
                      </>
                    )}
                  </div>
                  {editId !== m.id && (
                    <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                      <button className="hub-btn ghost sm" title={m.pinned ? 'Unpin' : 'Pin (always ranks first)'} onClick={() => togglePin(m)}>
                        <Icon name="pin" />
                      </button>
                      {m.kind === 'fact' && (
                        <button className="hub-btn ghost sm" title="Edit" onClick={() => { setEditId(m.id); setEditText(m.text); }}>
                          <Icon name="pencil" />
                        </button>
                      )}
                      <button className="hub-btn ghost sm" title="Forget" onClick={() => remove(m)}>
                        <Icon name="trash" />
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
      </Panel>
    </>
  );
}
