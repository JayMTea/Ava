import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { RowMenu, type MenuAction } from '../../../lib/RowMenu';
import { ACCENT_SLOTS, APP_ICONS, appAccent, appIcon } from '../../../lib/appColor';
import { EmptyState, Panel } from '../../dashboard/layout';
import { NewConnectorForm } from '../ConnectApp';
import { useResource } from '../hooks';
import { connectorGroup, isExternalApp, type ConnectorGroup } from '../shared';
import { hub } from '../hubApi';
import type {
  GenerateResult, GrantAction, HubConnector, IngestToken,
} from '../hubApi';
import { Badge } from '../ui/Badge';
import { Legend } from '../ui/Legend';
import { Tile } from '../ui/Tile';

// Connectors — the external-app registry: per-connector row (deploy state,
// permissions, preview, ⋯ menu) and the JIT permission sheet.
//
// The "connect a new app / device" wizard used to live here too. It moved to
// hub/ConnectApp.tsx unchanged, because the sidebar now offers the same flow in
// a dialog — one form, two mount points, no second implementation to keep in
// step. This panel stays the home of everything you do to an app *after*
// connecting it.

// How a connector's tools reach Ava, rendered verbatim from the backend's
// `transport` field (ava_bridge/connectors.py transport()). The UI must never
// re-derive this: the previous badge showed "MCP" for anything with tools at all,
// so a plain-REST app and a real MCP server looked identical. Identity decides the
// section (shared.connectorGroup); this is a separate axis shown in the meta line.
const TRANSPORT_LABEL: Record<string, string> = {
  mcp: 'MCP',
  discover: 'tool facade',
  rest: 'REST',
  none: '',
};
// Sections are identity, not protocol: a device that speaks MCP is still a
// device. Order runs most-concrete to least.
const GROUP_ORDER: ConnectorGroup[] = ['devices', 'apps', 'tools'];
const GROUP_TITLES: Record<ConnectorGroup, string> = {
  devices: 'Devices',
  apps: 'Apps',
  tools: 'Tools',
};

const TRANSPORT_HINT: Record<string, string> = {
  mcp: 'A real Model Context Protocol server — Ava speaks MCP to it.',
  discover: "Ava's own ava-tools/1 HTTP facade — MCP-shaped, but not MCP.",
  rest: "Statically declared actions proxied to the app's REST API.",
  none: 'No agent surface — UI-only, or a push-only device.',
};
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
  const [showCred, setShowCred] = useState(false);
  const [credVal, setCredVal] = useState('');

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

  // Save (or clear, value: '') the app's credential to Ava's server-side secret
  // store, keyed by the manifest's token_env NAME. Persists across restarts and
  // every redeploy — no more re-entering a password; the agent never sees it.
  const saveCred = useCallback(async (value: string) => {
    setBusy(true); setMsg(''); setErr('');
    try {
      const r = await hub.setConnectorSecret(c.id, value);
      if (r.ok) {
        setMsg(value.trim() ? "Credential saved — you won't be asked for it again on deploy." : 'Credential cleared.');
        setCredVal(''); setShowCred(false); onChanged();
      } else setErr(r.error || 'could not save credential');
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [c.id, onChanged]);

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
    ...(c.auth_env ? [{ label: c.auth_set ? 'Update credential' : 'Add credential', icon: 'lock', onClick: () => setShowCred((v) => !v) }] : []),
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
            {c.builtin && <Badge>built-in</Badge>}
          </div>
          <div className="conn-meta">
            {c.enabled
              ? <span className="conn-stat tone-ok" title="Ava can use this connector"><i />enabled</span>
              : <span className="conn-stat tone-muted" title="Turned off — Ava won't use it"><i />disabled</span>}
            {c.transport && c.transport !== 'none' && (
              <><span className="meta-sep">·</span>
              <span title={TRANSPORT_HINT[c.transport]}>{TRANSPORT_LABEL[c.transport]}</span></>
            )}
            {c.actions > 0 && <><span className="meta-sep">·</span><span>{c.actions} action{c.actions === 1 ? '' : 's'}</span></>}
            {hasAgentSurface && c.enabled && (
              <><span className="meta-sep">·</span>
              {deployed
                ? <span className="conn-stat tone-ok" title="Tools and egress policy are up to date in the agent"><Icon name="check" />deployed</span>
                : <span className="conn-stat tone-warn" title={`${drift || 'tools'} out of date — Deploy regenerates ${drift ? 'them' : 'the tools'} into the agent`}><Icon name="alert" />needs deploy</span>}
              </>
            )}
            {c.auth_env && (
              <><span className="meta-sep">·</span>
              {c.auth_set
                ? <span className="conn-stat tone-ok" title={`Credential saved — Ava signs in with ${c.auth_env}; you won't be re-prompted on deploy`}><Icon name="lock" />credential saved</span>
                : <span className="conn-stat tone-warn" title={`Needs a token (${c.auth_env}) — add it so Ava can sign in`}><Icon name="alert" />needs a token</span>}
              </>
            )}
          </div>
        </div>
        <div className="row-actions">
          {!c.enabled && !c.builtin ? (
            <button type="button" className="hub-btn sm" onClick={toggleEnabled} disabled={busy}>
              <Icon name="check" />{busy ? 'Enabling…' : 'Enable'}
            </button>
          ) : needsDeploy ? (
            <button type="button" className="hub-btn sm" onClick={deploy} disabled={busy}
              title={`${drift || 'This connector'} out of date — regenerate into the agent`}>
              <Icon name="check" />{busy ? 'Deploying…' : 'Deploy'}
            </button>
          ) : null}
          {hasAgentSurface && c.enabled && (
            <button type="button" className="hub-btn ghost sm" onClick={() => setShowPerms((v) => !v)} aria-expanded={showPerms}
              title="What Ava may do in this app — reads run silently, writes ask once, destructive always asks">
              <Icon name="lock" />Permissions
            </button>
          )}
          {hasAgentSurface && c.enabled && (
            <button type="button" className="hub-btn ghost sm" onClick={preview} disabled={busy}>
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
              <button type="button"
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
                <button type="button"
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
            <button type="button" className="hub-btn ghost sm" disabled={busy || (!c.icon && !c.color)}
              onClick={() => setLook({ icon: null, color: null })}
              title="Clear both overrides — back to the automatic icon and color">
              <Icon name="refresh" />Reset to auto
            </button>
            <button type="button" className="hub-btn ghost sm" onClick={() => setShowLook(false)} disabled={busy}>Done</button>
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
            <button type="button" className="hub-btn sm" onClick={saveEdit} disabled={busy}>
              <Icon name="check" />{busy ? 'Saving…' : 'Save manifest'}
            </button>
            <button type="button" className="hub-btn ghost sm" onClick={() => setEditText(null)} disabled={busy}>Cancel</button>
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
            <button type="button" className="hub-btn ghost sm" style={{ flex: '0 0 auto' }}
              onClick={() => navigator.clipboard?.writeText(token.token || '')}><Icon name="copy" />Copy</button>
            <button type="button" className="hub-btn ghost sm" style={{ flex: '0 0 auto' }} onClick={() => setToken(null)}>Hide</button>
          </div>
        </div>
      )}
      {showCred && c.auth_env && (
        <div className="hub-note" style={{ marginTop: 8 }}>
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginBottom: 6 }}>
            Paste {c.label}'s token / API key. Saved once to Ava's private secret store as{' '}
            <code>{c.auth_env}</code> — never in the manifest, never shown to the AI, and reused on every deploy.
          </div>
          <div className="hub-fieldrow">
            <input className="hub-input" type="password" autoComplete="off" value={credVal} style={{ flex: 1 }}
              placeholder={c.auth_set ? 'enter a new value to replace it' : 'paste token'}
              onChange={(e) => setCredVal(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && credVal.trim()) saveCred(credVal.trim()); }} />
            <button type="button" className="hub-btn sm" style={{ flex: '0 0 auto' }} disabled={busy || !credVal.trim()}
              onClick={() => saveCred(credVal.trim())}><Icon name="check" />{busy ? 'Saving…' : 'Save'}</button>
            {c.auth_stored && (
              <button type="button" className="hub-btn ghost sm" style={{ flex: '0 0 auto' }} disabled={busy}
                onClick={() => saveCred('')} title="Remove the saved credential"><Icon name="trash" />Clear</button>
            )}
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
          <button type="button" className="hub-btn ghost sm" style={{ marginTop: 8 }} onClick={() => setOpen(false)}>Hide preview</button>
        </div>
      )}
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
          ? <div className="hub-msg err">Couldn’t load connectors: {loadErr}. <button type="button" className="hub-btn ghost sm" style={{ marginLeft: 8 }} onClick={load}>Retry</button></div>
          : conns == null ? <EmptyState text="Loading connectors…" />
            : conns.length === 0 ? <EmptyState text="No connectors yet — create one above." />
              : GROUP_ORDER.map((g) => {
                const rows = conns.filter((c) => connectorGroup(c) === g);
                if (rows.length === 0) return null;
                return (
                  <div className="hub-group" key={g}>
                    <div className="hub-group-title">{GROUP_TITLES[g]}</div>
                    {rows.map((c) => <ConnectorRow key={c.id} c={c} onChanged={load} />)}
                  </div>
                );
              })}
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
