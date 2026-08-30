import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { AppDot, appAccent, appById, appForTool, appsForTools } from '../../../lib/appColor';
import { MarkdownLite } from '../../../lib/markdown';
import { EmptyState, Panel } from '../../ui/layout';
import { hub } from '../hubApi';
import type { Skill, SkillList } from '../hubApi';
import { Badge } from '../ui/Badge';
import { DriftBadge } from '../ui/DriftBadge';

// ─────────────────────────────────────────────────────────────────────────────
// Skills — the agent's SKILL.md capabilities, auto-discovered from the filesystem
// (agent/skills + overlay). Adding a folder surfaces it here with no code change;
// each skill shows what it does, the tools it uses, and whether it's actually
// deployed into the sandbox vs newly added in the repo.
const SKILL_ICONS = new Set([
  'image', 'chart', 'code', 'grid', 'cloud', 'calendar', 'chats', 'megaphone',
  'sparkles', 'graduation', 'bot', 'db', 'gauge', 'search', 'panel',
]);

// (The local skillDeployBadge lived here. It is now <DriftBadge> in hub/ui, so
// skills, the persona, policies and tool servers all speak one vocabulary — and
// so the call to action appears once, in the bar, rather than on every row.)

// How the list is sectioned adapts to what the OWNER has categorized — the
// product ships no taxonomy (categories live in ava.yaml, not shipped skills):
//   • any category present   → group by category ("General" bucket last)
//   • otherwise, mixed source → group by source (Core skills / Your skills)
//   • single source, no cats  → flat list
// so a fresh fork looks clean, and the very first drag-to-categorize switches
// the view over to category grouping. Only category groups are drop targets
// and renamable — the source/flat groupings are derived, not owner data.
type SkillGroupMode = 'category' | 'source' | 'flat';
function groupSkills(skills: Skill[], order: string[]): { mode: SkillGroupMode; groups: [string, Skill[]][] } {
  const cats = new Set(skills.map((s) => s.category).filter(Boolean) as string[]);
  if (cats.size >= 1 || order.length >= 1) {
    const map = new Map<string, Skill[]>();
    // Owner-created categories exist even while empty — seed them so a fresh
    // "New category" shows up as a drop target immediately.
    for (const c of order) map.set(c, []);
    for (const s of skills) {
      const cat = s.category || 'General';
      (map.get(cat) ?? map.set(cat, []).get(cat)!).push(s);
    }
    // Owner order first, then unordered labels alphabetically, General last.
    const pos = new Map(order.map((c, i) => [c, i]));
    const groups = [...map.entries()].sort(([a], [b]) => {
      if (a === 'General') return 1;
      if (b === 'General') return -1;
      const pa = pos.has(a) ? pos.get(a)! : Number.POSITIVE_INFINITY;
      const pb = pos.has(b) ? pos.get(b)! : Number.POSITIVE_INFINITY;
      return pa !== pb ? pa - pb : a.localeCompare(b);
    });
    return { mode: 'category', groups };
  }
  const bySource = new Map<string, Skill[]>();
  for (const s of skills) (bySource.get(s.source) ?? bySource.set(s.source, []).get(s.source)!).push(s);
  if (bySource.size >= 2) {
    const groups: [string, Skill[]][] = [];
    if (bySource.get('core')) groups.push(['Core skills', bySource.get('core')!]);
    if (bySource.get('overlay')) groups.push(['Your skills', bySource.get('overlay')!]);
    return { mode: 'source', groups };
  }
  return { mode: 'flat', groups: [['', skills]] };
}

function SkillRow({ s, open, onToggle, body, dragging, onDragStart, onDragEnd }: {
  s: Skill; open: boolean; onToggle: () => void; body: string | null | undefined;
  dragging: boolean;
  onDragStart: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd: () => void;
}) {
  // Skills that drive a connected app's tools carry the app's identity dot
  // (title + tool chips), so app-backed capabilities read as the app's.
  // SKILL.md `app:` is authoritative (dynamic connectors' tool names carry no
  // prefix); otherwise attribute from the `<connectorId>_` tool convention.
  const declaredApp = appById(s.app);
  const skillApps = declaredApp ? [declaredApp] : appsForTools(s.tools);
  return (
    <div
      className={'skill-row' + (open ? ' open' : '') + (dragging ? ' dragging' : '')}
      data-skill={s.id}
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
    >
      <button type="button" className="skill-head" onClick={onToggle} aria-expanded={open}>
        <span className="skill-icon">
          <Icon name={s.icon && SKILL_ICONS.has(s.icon) ? s.icon : 'sparkles'} />
        </span>
        <span className="skill-head-main">
          <span className="skill-title">
            {s.title}
            {skillApps.map((a) => <AppDot key={a.id} accent={appAccent(a)} title={a.label} />)}
            {s.source === 'overlay' && <span className="skill-tag">private</span>}
          </span>
          <span className="skill-summary">{s.summary || s.description}</span>
        </span>
        <DriftBadge state={s.deployed} />
        <span className={'skill-chevron' + (open ? ' open' : '')}><Icon name="expand" /></span>
      </button>
      {open && (
        <div className="skill-detail">
          {s.tools.length > 0 && (
            <div className="skill-tools">
              <span className="skill-tools-label">tools</span>
              {s.tools.map((t) => {
                // With a declared app the author says these tools are the
                // app's (their names carry no prefix to match on).
                const app = declaredApp ?? appForTool(t);
                return (
                  <code className="skill-tool" key={t}>
                    {app && <AppDot accent={appAccent(app)} title={app.label} />}
                    {t}
                  </code>
                );
              })}
            </div>
          )}
          {body === undefined ? (
            <div className="skill-doc-loading">Loading skill…</div>
          ) : body === null ? (
            <div className="hub-msg err">Couldn’t read this skill’s file.</div>
          ) : (
            <div className="skill-doc"><MarkdownLite text={body} /></div>
          )}
        </div>
      )}
    </div>
  );
}

export function SkillsPanel() {
  const [data, setData] = useState<SkillList | null>(null);
  const [err, setErr] = useState('');
  const [note, setNote] = useState('');
  const [query, setQuery] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [bodies, setBodies] = useState<Record<string, string | null>>({});
  // Groups render collapsed until the owner opens them, so a long skill list
  // scans as a table of contents. A live filter overrides this — matches must
  // be visible, so searching temporarily expands everything.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [dragId, setDragId] = useState<string | null>(null);
  const [dropCat, setDropCat] = useState<string | null>(null);
  // A category header being dragged to reorder, and where it would land.
  const [dragCat, setDragCat] = useState<string | null>(null);
  const [catDrop, setCatDrop] = useState<{ cat: string; before: boolean } | null>(null);
  const [editingCat, setEditingCat] = useState<string | null>(null);
  const [editVal, setEditVal] = useState('');
  // The inline "name a category" form: {skill} after a drop on the new-category
  // zone (file the skill there once named), {} from the New category button.
  const [newCat, setNewCat] = useState<{ skill?: string } | null>(null);
  const [newCatVal, setNewCatVal] = useState('');

  const refresh = useCallback(
    // Normalise the payload so a partial or errored response (missing summary or
    // skills) can never crash the whole Setup view via the error boundary — a
    // malformed body renders as "no skills", not a blank error page.
    () => hub.agentSkills().then((d) => setData({
      skills: d?.skills ?? [],
      errors: d?.errors ?? [],
      summary: d?.summary ?? {
        total: (d?.skills ?? []).length, deployed: 0, stale: 0, unknown: 0,
      },
      category_order: d?.category_order,
    })).catch((e) => setErr((e as Error).message)),
    []);
  useEffect(() => { refresh(); }, [refresh]);

  const toggle = useCallback(async (s: Skill) => {
    if (openId === s.id) { setOpenId(null); return; }
    setOpenId(s.id);
    if (bodies[s.id] === undefined) {
      try {
        const d = await hub.agentSkill(s.id);
        setBodies((b) => ({ ...b, [s.id]: d.body }));
      } catch {
        setBodies((b) => ({ ...b, [s.id]: null }));
      }
    }
  }, [openId, bodies]);

  const moveSkill = useCallback(async (id: string, cat: string) => {
    setNote('');
    // Optimistic: reflect the drop immediately, then confirm with a refetch.
    setData((d) => d && ({
      ...d, skills: d.skills.map((s) => (s.id === id ? { ...s, category: cat } : s)),
    }));
    setExpanded((x) => new Set(x).add(cat));
    try {
      const r = await hub.setSkillCategory(id, cat);
      if (!r.ok) setNote(r.error || 'Couldn’t move that skill.');
    } catch (e) {
      setNote((e as Error).message);
    }
    refresh();
  }, [refresh]);

  const renameCat = useCallback(async (from: string, to: string) => {
    setEditingCat(null);
    const clean = to.trim();
    if (!clean || clean === from) return;
    setNote('');
    try {
      const r = await hub.renameSkillCategory(from, clean);
      if (!r.ok) setNote(r.error || 'Couldn’t rename that category.');
      // Carry the open/closed state over to the new name.
      setExpanded((x) => {
        if (!x.has(from)) return x;
        const n = new Set(x); n.delete(from); n.add(clean);
        return n;
      });
    } catch (e) {
      setNote((e as Error).message);
    }
    refresh();
  }, [refresh]);

  const createCat = useCallback(async (name: string) => {
    setNote('');
    setExpanded((x) => new Set(x).add(name));
    try {
      const r = await hub.createSkillCategory(name);
      if (!r.ok) setNote(r.error || 'Couldn’t create that category.');
    } catch (e) {
      setNote((e as Error).message);
    }
    refresh();
  }, [refresh]);

  const removeCat = useCallback(async (name: string) => {
    setNote('');
    setData((d) => d && ({
      ...d, category_order: (d.category_order ?? []).filter((c) => c !== name),
    }));
    try {
      const r = await hub.deleteSkillCategory(name);
      if (!r.ok) setNote(r.error || 'Couldn’t delete that category.');
    } catch (e) {
      setNote((e as Error).message);
    }
    refresh();
  }, [refresh]);

  const q = query.trim().toLowerCase();
  const filtered = (data?.skills ?? []).filter((s) =>
    !q || s.title.toLowerCase().includes(q) || s.summary.toLowerCase().includes(q) ||
    (s.category ?? '').toLowerCase().includes(q) || s.tools.some((t) => t.toLowerCase().includes(q)));
  const { mode, groups } = groupSkills(filtered, data?.category_order ?? []);
  // While searching, an owner-created-but-empty category is just noise — show
  // only groups with matches.
  const shownGroups = q ? groups.filter(([, list]) => list.length > 0) : groups;

  // Persist a category reorder: rebuild the full visible order (minus the
  // pinned General bucket) with the dragged category in its new slot.
  const applyReorder = (target: string, before: boolean) => {
    if (!dragCat || dragCat === target) { setDragCat(null); setCatDrop(null); return; }
    const current = groups.map(([c]) => c).filter((c) => c !== 'General');
    const list = current.filter((c) => c !== dragCat);
    let idx = target === 'General' ? list.length : list.indexOf(target);
    if (idx < 0) idx = list.length;
    else if (!before) idx += 1;
    list.splice(idx, 0, dragCat);
    setDragCat(null); setCatDrop(null); setNote('');
    setData((d) => d && ({ ...d, category_order: list }));
    hub.setSkillCategoryOrder(list)
      .then((r) => { if (!r.ok) setNote(r.error || 'Couldn’t save the order.'); })
      .catch((e) => setNote((e as Error).message))
      .finally(() => { void refresh(); });
  };

  const renderRows = (list: Skill[]) => (
    <div className="skill-list">
      {list.map((s) => (
        <SkillRow
          key={`${s.source}:${s.id}`}
          s={s}
          open={openId === s.id}
          onToggle={() => toggle(s)}
          body={openId === s.id ? bodies[s.id] : undefined}
          dragging={dragId === s.id}
          onDragStart={(e) => {
            e.dataTransfer.setData('text/plain', s.id);
            e.dataTransfer.effectAllowed = 'move';
            setDragId(s.id);
          }}
          onDragEnd={() => { setDragId(null); setDropCat(null); }}
        />
      ))}
    </div>
  );

  // Drop targets: category groups take skill drops (file it there) AND
  // category drops (reorder — top half inserts before, bottom half after).
  // The "new category" zone (key '+') takes skill drops only.
  const isBefore = (e: React.DragEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientY < r.top + r.height / 2;
  };
  const dropProps = (cat: string) => ({
    onDragOver: (e: React.DragEvent<HTMLDivElement>) => {
      if (dragId) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        setDropCat(cat);
      } else if (dragCat && dragCat !== cat && cat !== '+') {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        setCatDrop({ cat, before: cat !== 'General' && isBefore(e) });
      }
    },
    onDragLeave: (e: React.DragEvent<HTMLDivElement>) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
        setDropCat((c) => (c === cat ? null : c));
        setCatDrop((c) => (c?.cat === cat ? null : c));
      }
    },
    onDrop: (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      if (dragCat) {
        if (cat !== '+') applyReorder(cat, cat !== 'General' && isBefore(e));
        return;
      }
      const id = e.dataTransfer.getData('text/plain') || dragId;
      setDragId(null); setDropCat(null);
      if (!id) return;
      if (cat === '+') { setNewCat({ skill: id }); setNewCatVal(''); } else void moveSkill(id, cat);
    },
  });

  const right = data ? (
    <Badge tone="accent">{data.summary.total} skill{data.summary.total === 1 ? '' : 's'}</Badge>
  ) : null;

  // One naming form, two homes: inline in the top toolbar (New category
  // button) or under the drop zone when a dragged skill is waiting on a name.
  const newCatForm = (
    <form
      className="skill-newcat-form"
      onSubmit={(e) => {
        e.preventDefault();
        const pending = newCat;
        setNewCat(null);
        const name = newCatVal.trim();
        if (!name) return;
        if (pending?.skill) void moveSkill(pending.skill, name);
        else void createCat(name);
      }}
    >
      <input
        className="hub-input"
        placeholder="New category name…"
        value={newCatVal}
        autoFocus
        onChange={(e) => setNewCatVal(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Escape') setNewCat(null); }}
      />
      <button className="hub-btn sm" type="submit" disabled={!newCatVal.trim()}>Create</button>
      <button className="hub-btn ghost sm" type="button" onClick={() => setNewCat(null)}>Cancel</button>
    </form>
  );

  return (
    <Panel
      title="Skills"
      subtitle="Capabilities your agent loads. Drop a folder in agent/skills (or your overlay) and it appears here automatically; expand one to read its full instructions, and re-provision to deploy it into the sandbox. Categories are yours: create your own, drag skills between them, drag headers to reorder, rename with the pencil."
      right={right}
    >
      {err ? (
        <div className="hub-msg err">{err}</div>
      ) : !data ? (
        <EmptyState text="Loading skills…" />
      ) : data.skills.length === 0 ? (
        <EmptyState text="No skills found under agent/skills." />
      ) : (
        <>
          <div className="skill-toolbar">
            {data.skills.length > 6 && (
              <input
                className="hub-input skill-search"
                placeholder="Filter skills…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            )}
            {newCat && !newCat.skill ? (
              newCatForm
            ) : (
              <button type="button"
                className="hub-btn ghost sm skill-newcat-btn"
                onClick={() => { setNewCat({}); setNewCatVal(''); }}
              >
                <Icon name="plus" />New category
              </button>
            )}
          </div>
          {filtered.length === 0 ? (
            <EmptyState text="No skills match your filter." />
          ) : mode === 'flat' ? (
            renderRows(groups[0][1])
          ) : (
            shownGroups.map(([cat, list]) => {
              const isOpen = !!q || expanded.has(cat);
              const canDrop = mode === 'category';
              return (
                <div
                  className={'skill-group'
                    + (canDrop && dropCat === cat ? ' drop-target' : '')
                    + (catDrop?.cat === cat ? (catDrop.before ? ' reorder-before' : ' reorder-after') : '')}
                  key={cat}
                  data-cat={cat}
                  {...(canDrop ? dropProps(cat) : {})}
                >
                  <div
                    className="skill-group-head"
                    draggable={canDrop && cat !== 'General' && !q && editingCat !== cat}
                    onDragStart={(e) => {
                      e.dataTransfer.setData('text/x-ava-category', cat);
                      e.dataTransfer.effectAllowed = 'move';
                      setDragCat(cat);
                    }}
                    onDragEnd={() => { setDragCat(null); setCatDrop(null); }}
                  >
                    {editingCat === cat ? (
                      <input
                        className="skill-group-edit"
                        value={editVal}
                        autoFocus
                        onChange={(e) => setEditVal(e.target.value)}
                        onBlur={() => renameCat(cat, editVal)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') renameCat(cat, editVal);
                          if (e.key === 'Escape') setEditingCat(null);
                        }}
                      />
                    ) : (
                      <>
                        <button type="button"
                          className="skill-group-toggle"
                          onClick={() => setExpanded((x) => {
                            const n = new Set(x);
                            if (n.has(cat)) n.delete(cat); else n.add(cat);
                            return n;
                          })}
                          aria-expanded={isOpen}
                        >
                          <span className={'skill-caret' + (isOpen ? ' open' : '')}>
                            <Icon name="chevronDown" />
                          </span>
                          <span className="skill-group-title">{cat}</span>
                          <span className="skill-group-count">{list.length}</span>
                        </button>
                        {mode === 'category' && (
                          <button type="button"
                            className="skill-group-rename"
                            title={`Rename “${cat}”`}
                            aria-label={`Rename category ${cat}`}
                            onClick={() => { setEditingCat(cat); setEditVal(cat); }}
                          >
                            <Icon name="pencil" />
                          </button>
                        )}
                        {mode === 'category' && cat !== 'General' && list.length === 0 && (
                          <button type="button"
                            className="skill-group-rename skill-group-del"
                            title={`Delete “${cat}”`}
                            aria-label={`Delete category ${cat}`}
                            onClick={() => void removeCat(cat)}
                          >
                            <Icon name="trash" />
                          </button>
                        )}
                      </>
                    )}
                  </div>
                  {isOpen && (list.length > 0 ? renderRows(list) : (
                    <div className="skill-empty-hint">No skills here yet — drag one in.</div>
                  ))}
                </div>
              );
            })
          )}
          {dragId && !newCat && (
            <div
              className={'skill-newcat-zone' + (dropCat === '+' ? ' drop-target' : '')}
              {...dropProps('+')}
            >
              Drop here to file under a new category
            </div>
          )}
          {newCat?.skill && newCatForm}
        </>
      )}
      {note && <div className="hub-msg err" style={{ marginTop: 12 }}>{note}</div>}
      {data && data.errors.length > 0 && (
        <div className="hub-msg err" style={{ marginTop: 12 }}>
          {data.errors.length} skill file{data.errors.length === 1 ? '' : 's'} couldn’t be read:{' '}
          {data.errors.map((e) => e.id).join(', ')}
        </div>
      )}
    </Panel>
  );
}
