import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { RowMenu, type MenuAction } from '../../../lib/RowMenu';
import { ACCENT_SLOTS, APP_ICONS, appAccent, appIcon } from '../../../lib/appColor';
import { EmptyState, Panel } from '../../dashboard/primitives';
import { useResource } from '../hooks';
import { isExternalApp } from '../shared';
import { hub } from '../hubApi';
import type {
  DeviceEvent, GenerateResult, GrantAction, HubConnector, IngestToken, NewConnectorBody, ProbeResult,
} from '../hubApi';
import { Badge } from '../ui/Badge';
import { Legend } from '../ui/Legend';
import { Tile } from '../ui/Tile';

// Connectors — the external-app registry: per-connector row (deploy state,
// permissions, preview, ⋯ menu), the JIT permission sheet, and the "connect a
// new app / device" wizard.
function PermissionsSheet({ cid }: { cid: string }) {
  const [acts, setActs] = useState<GrantAction[] | null>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    hub.connectorGrants(cid).then((r) => setActs(r.actions)).catch((e) => setErr((e as Error).message));
  }, [cid]);

  const toggle = async (a: GrantAction) => {
    setActs((xs) => xs?.map((x) => (x.id === a.id ? { ...x, granted: !a.granted } : x)) ?? null);
    try {
      if (a.granted) await hub.revokeGrant(cid, a.id);
      else await hub.grantAction(cid, a.id);
    } catch {
      setActs((xs) => xs?.map((x) => (x.id === a.id ? { ...x, granted: a.granted } : x)) ?? null);
    }
  };

  if (err) return <div className="hub-msg err">{err}</div>;
  if (acts == null) return <EmptyState text="Loading permissions…" />;
  if (!acts.length) return <EmptyState text="No declared actions — nothing to permit." />;

  const groups = [...new Set(acts.map((a) => a.capability))];
  const state = (a: GrantAction) =>
    a.access === 'read'
      ? <span style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)' }}>always allowed</span>
      : a.access === 'destructive' || !a.grantable
        ? <span style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <Icon name="lock" />asks every time</span>
        : <label className="hub-check" style={{ borderBottom: 0, padding: 0, margin: 0 }}
            title={a.granted ? 'Always allowed — uncheck and Ava asks again' : 'Ava asks the first time; check to always allow'}>
            <input type="checkbox" checked={a.granted} onChange={() => toggle(a)} />
            <span style={{ fontSize: 'var(--fs-xs)' }}>always allow</span>
          </label>;

  return (
    <div style={{ marginTop: 10 }}>
      {groups.map((g) => (
        <div key={g} style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 4 }}>
            {g}
          </div>
          {acts.filter((a) => a.capability === g).map((a) => (
            <div key={a.id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '5px 0', borderBottom: '1px solid var(--line)' }}>
              <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                <code>{a.id}</code>
                <Badge tone={a.access === 'read' ? 'ok' : a.access === 'destructive' || a.access === 'physical' ? 'err' : 'accent'}>{a.access}</Badge>
                {a.description && <span style={{ color: 'var(--muted)', fontSize: 'var(--fs-xs)', marginLeft: 6 }}>{a.description}</span>}
              </span>
              <span style={{ flexShrink: 0 }}>{state(a)}</span>
            </div>
          ))}
        </div>
      ))}
      <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
        Reads run silently · writes ask once unless always-allowed · destructive and physical actions always ask.
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
function ConnectorRow({ c, onChanged }: { c: HubConnector; onChanged: () => void }) {
  const [gen, setGen] = useState<GenerateResult | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');
  const [token, setToken] = useState<IngestToken | null>(null);
  const [editText, setEditText] = useState<string | null>(null);
  const [showPerms, setShowPerms] = useState(false);
  const [showLook, setShowLook] = useState(false);

  const hasAgentSurface = c.actions > 0 || ((c.mcp || c.discover) && c.renders_policy);

  // Rail identity. Writes ui.icon / ui.color to the manifest (the source of
  // truth /api/apps reads); null clears one back to the stable auto-pick.
  // `ava:apps-changed` makes the sidebar redraw without a reload.
  const setLook = useCallback(async (patch: { icon?: string | null; color?: string | null }) => {
    setBusy(true); setErr(''); setMsg('');
    try {
      const r = await hub.setAppearance(c.id, patch);
      if (r.ok) {
        window.dispatchEvent(new Event('ava:apps-changed'));
        onChanged();
      } else setErr(r.error || 'could not update appearance');
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [c.id, onChanged]);

  const toggleEnabled = useCallback(async () => {
    setBusy(true); setErr('');
    try {
      const r = await hub.setConnectorEnabled(c.id, !c.enabled);
      if (r.ok) onChanged(); else setErr(r.error || 'could not update');
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [c.id, c.enabled, onChanged]);

  const openEdit = useCallback(async () => {
    setErr(''); setMsg('');
    try {
      const r = await hub.getManifest(c.id);
      if (r.ok) setEditText(r.yaml || ''); else setErr(r.error || 'could not read manifest');
    } catch (e) { setErr((e as Error).message); }
  }, [c.id]);

  const saveEdit = useCallback(async () => {
    if (editText == null) return;
    setBusy(true); setErr('');
    try {
      const r = await hub.saveManifest(c.id, editText);
      if (r.ok) { setEditText(null); setMsg('Manifest saved.'); onChanged(); }
      else setErr(r.error || 'could not save');
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [c.id, editText, onChanged]);

  const preview = useCallback(async () => {
    setBusy(true); setMsg(''); setErr('');
    try { setGen(await hub.generate(c.id, false)); setOpen(true); }
    catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [c.id]);

  const deploy = useCallback(async () => {
    setBusy(true); setMsg(''); setErr('');
    try {
      const r = await hub.deployConnector(c.id);
      if (r.ok) setMsg(r.detail || (r.deployed ? 'Deployed into the agent sandbox.' : 'Done.'));
      else setErr(r.detail || r.steps?.find((s) => !s.ok)?.detail || 'deploy failed');
      onChanged();
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [c.id, onChanged]);

  const showToken = useCallback(async () => {
    setErr('');
    try {
      const t = await hub.ingestToken(c.id);
      if (t.ok) setToken(t); else setErr(t.error || 'no token');
    } catch (e) { setErr((e as Error).message); }
  }, [c.id]);

  const remove = useCallback(async () => {
    if (!window.confirm(`Remove connector “${c.label}”? This deletes its manifest.`)) return;
    setBusy(true); setErr('');
    try {
      const r = await hub.deleteConnector(c.id);
      if (r.ok) onChanged(); else setErr(r.error || 'could not remove');
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [c.id, c.label, onChanged]);

  // Identity: this row *represents a connected app*, so it carries the app's
  // own icon + accent (CLAUDE.md → connected-app identity accents), never Ava's.
  const ident = { id: c.id, icon: c.icon, color: c.color };
  const accent = appAccent(ident);

  // Deploy state. A connector is "deployed" only when everything it renders is
  // current: its tools (if it declares actions) and its egress policy (if it
  // renders one). Anything out of date — including never-deployed — is drift,
  // and drift is the *only* time the primary Deploy button appears. Deployed &
  // current rows offer a quiet Redeploy in the ⋯ menu instead.
  const toolsCurrent = c.actions === 0 || c.has_tools;
  const policyCurrent = !c.renders_policy || c.has_policy;
  const deployed = hasAgentSurface && toolsCurrent && policyCurrent;
  const needsDeploy = hasAgentSurface && !deployed;
  const drift = [
    c.actions > 0 && !c.has_tools ? 'tools' : null,
    c.renders_policy && !c.has_policy ? 'policy' : null,
  ].filter(Boolean).join(' + ');

  // Secondary + destructive actions collapse into the "⋯" so the visible row is
  // at most: one primary + Permissions + Preview + the kebab. Enable is promoted
  // to the primary slot when the connector is off (its most likely next action).
  const menuActions: MenuAction[] = [
    { label: 'Push token', icon: 'lock', onClick: showToken },
    ...(deployed ? [{ label: busy ? 'Redeploying…' : 'Redeploy', icon: 'refresh', onClick: deploy, disabled: busy }] : []),
    ...(c.app && !c.builtin ? [{ label: 'Appearance', icon: appIcon(ident), onClick: () => setShowLook((v) => !v) }] : []),
    ...(!c.builtin ? [{ label: 'Edit manifest', icon: 'pencil', onClick: openEdit }] : []),
    ...(c.enabled && !c.builtin ? [{ label: 'Disable', icon: 'eyeOff', onClick: toggleEnabled, disabled: busy }] : []),
    ...(!c.builtin ? [{ label: 'Remove', icon: 'trash', onClick: remove, danger: true, disabled: busy }] : []),
  ];

  return (
    <div className={'conn-row' + (c.enabled ? '' : ' off')}>
      <div className="conn-head">
        <Tile icon={appIcon(ident)} color={accent} size={34} />
        <div className="conn-id">
          <div className="conn-title-row">
            <span className="conn-title">{c.label}</span>
            {c.app && <Badge tone="accent">APP</Badge>}
            {(c.mcp || c.discover || c.actions > 0) && <Badge tone="accent">MCP</Badge>}
            {c.builtin && <Badge>built-in</Badge>}
          </div>
          <div className="conn-meta">
            {c.enabled
              ? <span className="conn-stat tone-ok" title="Ava can use this connector"><i />enabled</span>
              : <span className="conn-stat tone-muted" title="Turned off — Ava won't use it"><i />disabled</span>}
            {c.actions > 0 && <><span className="meta-sep">·</span><span>{c.actions} action{c.actions === 1 ? '' : 's'}</span></>}
            {hasAgentSurface && c.enabled && (
              <><span className="meta-sep">·</span>
              {deployed
                ? <span className="conn-stat tone-ok" title="Tools and egress policy are up to date in the agent"><Icon name="check" />deployed</span>
                : <span className="conn-stat tone-warn" title={`${drift || 'tools'} out of date — Deploy regenerates ${drift ? 'them' : 'the tools'} into the agent`}><Icon name="alert" />needs deploy</span>}
              </>
            )}
          </div>
        </div>
        <div className="row-actions">
          {!c.enabled && !c.builtin ? (
            <button className="hub-btn sm" onClick={toggleEnabled} disabled={busy}>
              <Icon name="check" />{busy ? 'Enabling…' : 'Enable'}
            </button>
          ) : needsDeploy ? (
            <button className="hub-btn sm" onClick={deploy} disabled={busy}
              title={`${drift || 'This connector'} out of date — regenerate into the agent`}>
              <Icon name="check" />{busy ? 'Deploying…' : 'Deploy'}
            </button>
          ) : null}
          {hasAgentSurface && c.enabled && (
            <button className="hub-btn ghost sm" onClick={() => setShowPerms((v) => !v)} aria-expanded={showPerms}
              title="What Ava may do in this app — reads run silently, writes ask once, destructive always asks">
              <Icon name="lock" />Permissions
            </button>
          )}
          {hasAgentSurface && c.enabled && (
            <button className="hub-btn ghost sm" onClick={preview} disabled={busy}>
              <Icon name="code" />Preview
            </button>
          )}
          <RowMenu actions={menuActions} disabled={busy} />
        </div>
      </div>
      {showLook && (
        <div className="app-look" style={{ marginTop: 10 }}>
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 6 }}>
            How <b>{c.label}</b> looks in the sidebar. Unset uses a stable pick from the
            app’s id, so every app differs without configuring anything.
          </div>
          <div className="app-look-row">
            {APP_ICONS.map((n) => (
              <button
                key={n}
                className={'app-look-ic' + (c.icon === n ? ' on' : '')}
                style={{ color: appAccent({ id: c.id, color: c.color }) }}
                disabled={busy}
                aria-label={`Icon: ${n}`}
                aria-pressed={c.icon === n}
                title={n}
                onClick={() => setLook({ icon: n })}
              >
                <Icon name={n} />
              </button>
            ))}
          </div>
          <div className="app-look-row" style={{ marginTop: 8 }}>
            {ACCENT_SLOTS.map((i) => {
              const v = `var(--app-accent-${i})`;
              return (
                <button
                  key={i}
                  className={'app-look-sw' + (c.color === v ? ' on' : '')}
                  style={{ background: v }}
                  disabled={busy}
                  aria-label={`Accent color ${i + 1}`}
                  aria-pressed={c.color === v}
                  onClick={() => setLook({ color: v })}
                />
              );
            })}
          </div>
          <div className="hub-btn-row" style={{ marginTop: 8 }}>
            <button className="hub-btn ghost sm" disabled={busy || (!c.icon && !c.color)}
              onClick={() => setLook({ icon: null, color: null })}
              title="Clear both overrides — back to the automatic icon and color">
              <Icon name="refresh" />Reset to auto
            </button>
            <button className="hub-btn ghost sm" onClick={() => setShowLook(false)} disabled={busy}>Done</button>
          </div>
        </div>
      )}
      {editText != null && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 6 }}>
            Editing <code>connectors/{c.id}/connector.yaml</code>. Full schema: <b>docs/CONNECTOR_SDK.md</b>.
          </div>
          <textarea className="hub-input" value={editText} spellCheck={false}
            onChange={(e) => setEditText(e.target.value)}
            style={{ width: '100%', minHeight: 200, fontFamily: 'monospace', fontSize: 'var(--fs-xs)', resize: 'vertical' }} />
          <div className="hub-btn-row" style={{ marginTop: 8 }}>
            <button className="hub-btn sm" onClick={saveEdit} disabled={busy}>
              <Icon name="check" />{busy ? 'Saving…' : 'Save manifest'}
            </button>
            <button className="hub-btn ghost sm" onClick={() => setEditText(null)} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}
      {showPerms && <PermissionsSheet cid={c.id} />}
      {msg && <div className="hub-msg ok" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="hub-msg err" style={{ marginTop: 8 }}>{err}</div>}
      {token && (
        <div className="hub-note" style={{ marginTop: 8 }}>
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 6 }}>
            Your device sends this as <code>Authorization: Bearer …</code> when it POSTs to <code>{token.url}</code>. Keep it secret — treat it like a password.
          </div>
          <div className="hub-fieldrow">
            <input className="hub-input" readOnly value={token.token || ''} style={{ flex: 1, fontFamily: 'monospace' }}
              onFocus={(e) => e.currentTarget.select()} />
            <button className="hub-btn ghost sm" style={{ flex: '0 0 auto' }}
              onClick={() => navigator.clipboard?.writeText(token.token || '')}><Icon name="copy" />Copy</button>
            <button className="hub-btn ghost sm" style={{ flex: '0 0 auto' }} onClick={() => setToken(null)}>Hide</button>
          </div>
        </div>
      )}
      {gen && open && (
        <div style={{ marginTop: 10 }}>
          {gen.policy && (
            <div className="hub-preview">
              <div className="hub-preview-head"><Icon name="lock" /> egress policy · agent/policies/generated/{c.id}.yaml</div>
              <pre>{gen.policy}</pre>
            </div>
          )}
          {gen.tools?.map((t) => (
            <div className="hub-preview" key={t.name}>
              <div className="hub-preview-head"><Icon name="code" /> {t.name}</div>
              <pre>{t.source}</pre>
            </div>
          ))}
          <button className="hub-btn ghost sm" style={{ marginTop: 8 }} onClick={() => setOpen(false)}>Hide preview</button>
        </div>
      )}
    </div>
  );
}

interface ActionDraft { id: string; method: string; path: string; description: string; confirm?: boolean; access?: string }

function ActionEditor({ actions, setAction, setActions }: {
  actions: ActionDraft[];
  setAction: (i: number, patch: Partial<ActionDraft>) => void;
  setActions: React.Dispatch<React.SetStateAction<ActionDraft[]>>;
}) {
  return (
    <>
      {actions.map((a, i) => (
        <div className="hub-fieldrow" key={i} style={{ marginBottom: 8 }}>
          <input className="hub-input" style={{ flex: 1 }} value={a.id} placeholder="what it does (e.g. create_note)"
            onChange={(e) => setAction(i, { id: e.target.value })} />
          <select className="hub-select" style={{ flex: '0 0 90px' }} value={a.method}
            onChange={(e) => setAction(i, { method: e.target.value })}>
            <option>POST</option><option>GET</option>
          </select>
          <input className="hub-input" style={{ flex: 2 }} value={a.path} placeholder="/api/notes"
            onChange={(e) => setAction(i, { path: e.target.value })} />
          <input className="hub-input" style={{ flex: 2 }} value={a.description} placeholder="short description for the agent"
            onChange={(e) => setAction(i, { description: e.target.value })} />
          <label className="hub-check" style={{ flex: '0 0 auto', borderBottom: 0, padding: '0 4px', margin: 0 }}
            title="Require my approval before Ava runs this action">
            <input type="checkbox" checked={!!a.confirm} onChange={(e) => setAction(i, { confirm: e.target.checked })} />
            <Icon name="lock" />
          </label>
          <button className="hub-btn ghost sm" style={{ flex: '0 0 auto' }} aria-label="Remove action"
            onClick={() => setActions((x) => x.filter((_, j) => j !== i))}><Icon name="trash" /></button>
        </div>
      ))}
      <button className="hub-btn ghost sm" onClick={() => setActions((a) => [...a, { id: '', method: 'POST', path: '', description: '' }])}>
        <Icon name="plus" />Add another
      </button>
    </>
  );
}

// Derive a safe connector id from a human app name, so the user never has to
// think about slugs: "My Notes App" -> "my-notes-app".
function slugId(name: string): string {
  return name.trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-+|-+$)/g, '')
    .replace(/-{2,}/g, '-')
    .slice(0, 32);
}
const VALID_ID = /^[a-z][a-z0-9_-]{1,31}$/;

function NewConnectorForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [reach, setReach] = useState('');       // a URL or a start command
  const [tokenEnv, setTokenEnv] = useState('');
  const [health, setHealth] = useState('');
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probeErr, setProbeErr] = useState('');
  const [actions, setActions] = useState<ActionDraft[]>([]);
  const [isolate, setIsolate] = useState(true);
  const [dockerAvail, setDockerAvail] = useState(true);
  const [confirmAll, setConfirmAll] = useState(false);
  const [isDevice, setIsDevice] = useState(false);
  const [addToRail, setAddToRail] = useState(false);
  const [uiUrl, setUiUrl] = useState('');   // split apps: UI at a different address
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [done, setDone] = useState('');
  const [verify, setVerify] = useState<{ cid: string; name: string } | null>(null);

  useEffect(() => {
    hub.system().then((s) => { setDockerAvail(s.docker); setIsolate(s.docker); }).catch(() => {});
  }, []);

  const id = slugId(name);
  const validId = VALID_ID.test(id);
  const isUrl = reach.trim().toLowerCase().startsWith('http');

  const reset = () => {
    setName(''); setReach(''); setTokenEnv(''); setHealth('');
    setProbe(null); setProbeErr(''); setActions([]); setIsDevice(false); setAddToRail(false); setUiUrl('');
  };
  const setAction = (i: number, patch: Partial<ActionDraft>) =>
    setActions((a) => a.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  const runProbe = useCallback(async () => {
    if (!reach.trim()) return;
    setProbing(true); setProbe(null); setProbeErr(''); setActions([]);
    try {
      const body = isUrl ? { url: reach.trim() } : { command: reach.trim() };
      const r = await hub.probeConnector({ ...body, token_env: tokenEnv.trim() || undefined });
      if (!r.ok) setProbeErr(r.error || 'could not reach it');
      else {
        setProbe(r);
        setAddToRail(!!r.has_ui);   // the app has a web UI — offer the sidebar tile, default on
        // Self-described apps (/.well-known/ava.json) prefill the form — only
        // fields the user hasn't typed into.
        if (r.label) setName((v) => v.trim() ? v : r.label!);
        if (r.health) setHealth((v) => v.trim() ? v : r.health!);
        if (r.kind === 'rest' || r.kind === 'unknown') {
          // Auto-fill from the app's OpenAPI spec when we found one; otherwise
          // start with one blank row for the user to fill in.
          setActions(r.actions?.length
            ? r.actions.map((a) => ({
                id: a.id, method: a.method, path: a.path,
                description: a.description || '', confirm: a.confirm, access: a.access,
              }))
            : [{ id: '', method: 'POST', path: '', description: '' }]);
        }
      }
    } catch (e) { setProbeErr((e as Error).message); }
    setProbing(false);
  }, [reach, isUrl, tokenEnv]);

  const create = useCallback(async () => {
    setBusy(true); setMsg(''); setDone('');
    const nm = name.trim();
    const body: NewConnectorBody = {
      id, label: nm || undefined, probe: health.trim() || undefined,
    };
    const tenv = tokenEnv.trim() || undefined;
    if (probe?.kind === 'mcp') {
      body.mcp = isUrl
        ? { url: reach.trim(), token_env: tenv }
        : { command: reach.trim(), token_env: tenv, sandbox: (isolate && dockerAvail) ? 'docker' : undefined };
      if (confirmAll) body.confirm = true;
    } else if (probe?.kind === 'discover') {
      // Facade paths from /.well-known/ava.json when declared (they may not
      // live at the /tools + /call defaults).
      body.discover = { base: reach.trim(), token_env: tenv,
                        list: probe.discover?.list, call: probe.discover?.call };
      if (confirmAll) body.confirm = true;
    } else if (!isDevice) {
      body.base_url = reach.trim() || undefined;
      body.token_env = tenv;
      body.actions = actions.filter((a) => a.id.trim() && a.path.trim());
      if (confirmAll) body.confirm = true;
    }
    if (isDevice) { body.role = 'device'; body.ingest = true; }
    if (addToRail && probe?.has_ui && !isDevice) body.ui = true;
    if (!probe?.has_ui && !isDevice && uiUrl.trim().toLowerCase().startsWith('http')) {
      body.ui_url = uiUrl.trim();   // split app: UI served from a different address
    }
    const jit = probe?.kind === 'rest' && (probe.actions?.length || 0) > 0 && !confirmAll;
    try {
      const r = await hub.newConnector(body);
      if (!r.ok) { setMsg(r.error || 'could not create connector'); }
      else if (isDevice) { setVerify({ cid: id, name: nm }); reset(); onCreated(); }
      else {
        setDone(jit
          ? `Connected “${nm}” — reads work now; Ava asks the first time it needs anything else.`
          : `Connected “${nm}”. Preview / Deploy below.`);
        reset(); onCreated();
      }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [id, name, health, reach, isUrl, tokenEnv, probe, actions, isolate, dockerAvail, confirmAll, isDevice, addToRail, uiUrl, onCreated]);

  const found = probe && (probe.kind === 'mcp' || probe.kind === 'discover');
  const manual = probe && (probe.kind === 'rest' || probe.kind === 'unknown');
  // A UI-only connect is legitimate: the app serves a web page but no
  // discoverable tools (e.g. an SPA container whose API lives elsewhere) —
  // the sidebar tile IS the product; tools can be added later via Edit.
  const uiOnly = !!probe?.has_ui && addToRail;
  const canCreate = validId && (isDevice
    ? true
    : !!probe && (found || (manual && (uiOnly
        || actions.some((a) => a.id.trim() && a.path.trim())))));

  // JIT consent (docs/dev/CONNECTOR_DISCOVERY_UX_PLAN.md): when the API was
  // auto-read there is nothing to review at connect time — reads run silently,
  // writes ask on first use, destructive always asks. Summarize by tier.
  const autoFound = (probe?.actions?.length || 0) > 0;
  const tierOf = (a: ActionDraft) => a.access || (a.method === 'GET' ? 'read' : 'write');
  const nTier = (t: string) => actions.filter((a) => tierOf(a) === t).length;

  if (!open) {
    return (
      <>
        <div className="hub-btn-row" style={{ marginTop: 0 }}>
          <button className="hub-btn" onClick={() => setOpen(true)}><Icon name="plus" />Connect an app or device</button>
          {done && <span className="hub-msg ok" style={{ marginTop: 0, alignSelf: 'center' }}>{done}</span>}
        </div>
        {verify && <DeviceVerify cid={verify.cid} name={verify.name} onClose={() => setVerify(null)} />}
      </>
    );
  }
  return (
    <Panel title="Connect an app" subtitle="Tell Ava where your app is — it figures out how to talk to it and writes the setup. You'll preview the tools and the security policy before anything goes live." right={
      <button className="hub-btn ghost sm" onClick={() => { setOpen(false); reset(); }}>Cancel</button>
    }>
      <div className="hub-field" style={{ maxWidth: 420 }}>
        <label>App name</label>
        <input className="hub-input" value={name} autoFocus
          onChange={(e) => setName(e.target.value)} placeholder="My Notes App" />
        {name.trim() && (validId
          ? <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 5 }}>
              Ava will refer to it as <code style={{ color: 'var(--txt)' }}>{id}</code>
            </div>
          : <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--warn)', marginTop: 5 }}>
              Please start the name with a letter (at least 2 characters).
            </div>)}
      </div>

      <label className="hub-check" style={{ marginTop: 4 }}>
        <input type="checkbox" checked={isDevice} onChange={(e) => setIsDevice(e.target.checked)} />
        <span className="hub-check-main">
          <span className="hub-check-title">This is a device (sensor / hardware)</span>
          <span className="hub-check-sub">
            Ava will let it push readings and events, and you'll get a push token after connecting.
            Wire your Arduino/ESP32/hub with the <code>AvaClient</code> SDK (<code>sdk/</code>). A web
            address below is optional — add one only if Ava should also read or command it on demand.
          </span>
        </span>
      </label>

      <div className="hub-field">
        <label>{isDevice ? 'Where is your device app? (optional)' : 'Where is your app?'}</label>
        <div className="hub-fieldrow">
          <input className="hub-input" style={{ flex: 3 }} value={reach}
            onChange={(e) => { setReach(e.target.value); setProbe(null); setProbeErr(''); }}
            onKeyDown={(e) => { if (e.key === 'Enter') runProbe(); }}
            placeholder="http://127.0.0.1:9000  —  or a start command like: npx -y @modelcontextprotocol/server-github" />
          <button className="hub-btn" style={{ flex: '0 0 auto' }} onClick={runProbe} disabled={probing || !reach.trim()}>
            <Icon name={probing ? 'refresh' : 'sparkles'} />{probing ? 'Checking…' : 'Detect'}
          </button>
        </div>
        <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 5 }}>
          {isDevice
            ? 'Leave blank for a push-only device. Add the address of its pull server (or the host adapter) if Ava should read or command it.'
            : "Paste its web address, or a command that starts it. Ava checks what it is — you don't have to know."}
        </div>
      </div>

      <div className="hub-fieldrow">
        <div className="hub-field"><label>Access token env var <span style={{ opacity: 0.7 }}>(optional, if it needs auth)</span></label>
          <input className="hub-input" value={tokenEnv} onChange={(e) => setTokenEnv(e.target.value)} placeholder="MYAPP_TOKEN" /></div>
        <div className="hub-field"><label>Health check URL <span style={{ opacity: 0.7 }}>(optional — shows if it's online)</span></label>
          <input className="hub-input" value={health} onChange={(e) => setHealth(e.target.value)} placeholder="http://127.0.0.1:9000/health" /></div>
      </div>

      {probeErr && <div className="hub-msg err">Couldn't reach it: {probeErr}. Check it's running, or add its actions manually below.</div>}

      {found && (
        <div className="hub-note" style={{ borderColor: 'color-mix(in srgb, var(--ok) 40%, transparent)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span style={{ color: 'var(--ok)', display: 'inline-flex' }}><Icon name="check" /></span>
            <b>Found {probe!.tools?.length || 0} tool{(probe!.tools?.length || 0) === 1 ? '' : 's'}</b>
            <span style={{ color: 'var(--muted)' }}>via {probe!.kind === 'mcp' ? `MCP (${probe!.transport})` : 'its tool list'} — Ava will discover and call these for you.</span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {(probe!.tools || []).slice(0, 24).map((t) => (
              <span key={t.name} className="hub-badge" title={t.description}>{t.name}</span>
            ))}
          </div>
          {probe!.kind === 'mcp' && !isUrl && (
            <label className="hub-check" style={{ marginTop: 12, borderBottom: 0, paddingBottom: 0 }}>
              <input type="checkbox" checked={isolate && dockerAvail} disabled={!dockerAvail}
                onChange={(e) => setIsolate(e.target.checked)} />
              <span className="hub-check-main">
                <span className="hub-check-title">Run it in an isolated container <span style={{ color: 'var(--ok)' }}>(recommended)</span></span>
                <span className="hub-check-sub">
                  {dockerAvail
                    ? 'This server runs on your machine — a container keeps it off your files (read-only, resource-capped).'
                    : 'Docker isn’t installed, so the server would run directly on the host. Install Docker to contain it.'}
                </span>
              </span>
            </label>
          )}
          <label className="hub-check" style={{ marginTop: probe!.kind === 'mcp' && !isUrl ? 0 : 12, borderBottom: 0, paddingBottom: 0 }}>
            <input type="checkbox" checked={confirmAll} onChange={(e) => setConfirmAll(e.target.checked)} />
            <span className="hub-check-main">
              <span className="hub-check-title">Ask me before Ava uses these</span>
              <span className="hub-check-sub">Every call waits for your one-tap approval — good for anything that spends money, sends messages, or deletes data.</span>
            </span>
          </label>
        </div>
      )}

      {manual && autoFound && (
        <div className="hub-note" style={{ borderColor: 'color-mix(in srgb, var(--ok) 40%, transparent)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span style={{ color: 'var(--ok)', display: 'inline-flex' }}><Icon name="check" /></span>
            <b>Ava read this app’s API — {actions.length} action{actions.length === 1 ? '' : 's'} found.</b>
          </div>
          <div style={{ color: 'var(--muted)' }}>
            Nothing to review now: {nTier('read') > 0 && <><b>{nTier('read')} read</b> work right away</>}
            {nTier('write') > 0 && <>{nTier('read') > 0 ? ' · ' : ''}<b>{nTier('write')} write</b> ask the first time each is used</>}
            {nTier('destructive') > 0 && <> · <b>{nTier('destructive')} destructive</b> ask every time</>}.
            {' '}You can change any of this later in the connector’s settings.
          </div>
          <label className="hub-check" style={{ marginTop: 12, borderBottom: 0, paddingBottom: 0 }}>
            <input type="checkbox" checked={confirmAll} onChange={(e) => setConfirmAll(e.target.checked)} />
            <span className="hub-check-main">
              <span className="hub-check-title">Ask me before Ava uses these — every time</span>
              <span className="hub-check-sub">Stricter than the default: every call waits for your one-tap approval, with no “always allow”.</span>
            </span>
          </label>
          <details style={{ marginTop: 10 }}>
            <summary style={{ cursor: 'pointer', color: 'var(--muted)', fontSize: 'var(--fs-xs)' }}>
              Advanced — view or edit the {actions.length} actions
            </summary>
            <div style={{ marginTop: 10 }}><ActionEditor actions={actions} setAction={setAction} setActions={setActions} /></div>
          </details>
        </div>
      )}

      {manual && !autoFound && (
        <div className="hub-field">
          <label>
            {probe!.kind === 'unknown'
              ? 'Ava couldn’t auto-detect its tools — tell it what this app can do:'
              : 'This looks like a regular web app — tell Ava what it can do:'}
          </label>
          <ActionEditor actions={actions} setAction={setAction} setActions={setActions} />
        </div>
      )}

      {probe?.has_ui && !isDevice && (
        <label className="hub-check" style={{ borderBottom: 0 }}>
          <input type="checkbox" checked={addToRail} onChange={(e) => setAddToRail(e.target.checked)} />
          <span className="hub-check-main">
            <span className="hub-check-title">Add it to Ava’s sidebar</span>
            <span className="hub-check-sub">
              This app has its own web UI — Ava embeds it as a tile in the left rail, served
              same-origin so it just works. Uncheck to connect only its tools.
            </span>
          </span>
        </label>
      )}

      {probe && !probe.has_ui && !isDevice && (
        <div className="hub-field" style={{ maxWidth: 420 }}>
          <label>Web UI address <span style={{ opacity: 0.7 }}>(optional — if this app's UI runs at a different address)</span></label>
          <input className="hub-input" value={uiUrl} onChange={(e) => setUiUrl(e.target.value)}
            placeholder="http://127.0.0.1:8081" />
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 5 }}>
            Split apps (an SPA container in front of an API container) get a sidebar tile too —
            Ava embeds the UI from here and routes its /api calls to the address above.
          </div>
        </div>
      )}

      <div className="hub-btn-row">
        <button className="hub-btn" onClick={create} disabled={busy || !canCreate}>
          <Icon name="check" />{busy ? 'Connecting…' : 'Connect app'}
        </button>
        {!probe && !isDevice && reach.trim() && <span className="hub-msg" style={{ marginTop: 0, alignSelf: 'center', color: 'var(--muted)' }}>Click Detect first.</span>}
      </div>
      {msg && <div className="hub-msg err">{msg}</div>}
      {verify && <DeviceVerify cid={verify.cid} name={verify.name} onClose={() => setVerify(null)} />}
    </Panel>
  );
}

// After a device is connected, watch for its first pushed reading so the user
// sees it come alive — the terminal-free proof that wiring worked.
function DeviceVerify({ cid, name, onClose }: { cid: string; name: string; onClose: () => void }) {
  const [ev, setEv] = useState<DeviceEvent | null>(null);
  useEffect(() => {
    let live = true;
    const tick = () => hub.lastEvent(cid).then((r) => { if (live && r.event) setEv(r.event); }).catch(() => {});
    tick();
    const h = setInterval(() => { if (live && !ev) tick(); }, 2500);
    return () => { live = false; clearInterval(h); };
  }, [cid, ev]);
  const okBorder = 'color-mix(in srgb, var(--ok) 40%, transparent)';
  return (
    <div className="hub-note" style={{ marginTop: 12, borderColor: ev ? okBorder : undefined }}>
      {ev ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span style={{ color: 'var(--ok)', display: 'inline-flex' }}><Icon name="check" /></span>
          <b>Receiving data from {name}.</b>
          <span style={{ color: 'var(--muted)' }}>
            Last: {ev.name}{ev.value !== undefined ? ` = ${ev.value}${ev.unit ? ` ${ev.unit}` : ''}` : ''}.
          </span>
        </div>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ display: 'inline-flex', color: 'var(--muted)' }}><Icon name="refresh" /></span>
          <span>Waiting for the first reading from <b>{name}</b>… copy its <b>push token</b> from the list below into your board, then it appears here.</span>
        </div>
      )}
      <button className="hub-btn ghost sm" style={{ marginTop: 8 }} onClick={onClose}>Done</button>
    </div>
  );
}

export function ConnectorsPanel() {
  // The fetcher fires `ava:apps-changed` on mount and every reload (connect /
  // edit / remove may have changed the app registry) so the rail redraws
  // without a page refresh — same as the old hand-rolled load.
  const { data: raw, error: loadErr, reload: load } = useResource(() => {
    window.dispatchEvent(new Event('ava:apps-changed'));
    return hub.connectors();
  });
  const conns = raw ? raw.connectors.filter(isExternalApp) : null;
  const badManifests = raw?.errors ?? [];
  return (
    <>
      <NewConnectorForm onCreated={load} />
      <div className="hub-section" />
      <Panel
        title="Connectors"
        subtitle="Each connector is one manifest that wires an app into Ava — its health, metrics, agent tools, and egress security policy."
      >
        {badManifests.length > 0 && (
          <div className="hub-msg err" style={{ marginBottom: 12 }}>
            <b>{badManifests.length} manifest{badManifests.length === 1 ? '' : 's'} couldn’t be loaded</b> and won’t appear below:
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {badManifests.map((e) => (
                <li key={e.path}><code>{e.id}</code>: {e.error}</li>
              ))}
            </ul>
          </div>
        )}
        {loadErr
          ? <div className="hub-msg err">Couldn’t load connectors: {loadErr}. <button className="hub-btn ghost sm" style={{ marginLeft: 8 }} onClick={load}>Retry</button></div>
          : conns == null ? <EmptyState text="Loading connectors…" />
            : conns.length === 0 ? <EmptyState text="No connectors yet — create one above." />
              : conns.map((c) => <ConnectorRow key={c.id} c={c} onChanged={load} />)}
        <Legend
          title="What the actions do"
          items={[
            { icon: 'check', term: 'Deploy', desc: <>Appears only when a connector's tools or egress policy are out of date — regenerates them into the agent so Ava can use it. Up-to-date connectors read <b>deployed</b>; redeploy anytime from the ⋯ menu.</> },
            { icon: 'lock', term: 'Permissions', desc: 'What Ava may do here — reads run silently, writes ask once, destructive actions always ask.' },
            { icon: 'code', term: 'Preview', desc: 'The tools and egress policy generated from the manifest, without touching the agent.' },
            { icon: 'more', term: 'More', desc: <>Push token, appearance, manifest editor, and disable&nbsp;/&nbsp;remove.</> },
          ]}
          foot={<>
            <div>Agent host unreachable from here? Run <code>cd agent &amp;&amp; ./install.sh</code> there to deploy.</div>
            <div>Docs — connectors: <b>docs/CONNECTOR_SDK.md</b> · hardware: <b>docs/DEVICE_CONNECTORS.md</b></div>
          </>}
        />
      </Panel>
    </>
  );
}
