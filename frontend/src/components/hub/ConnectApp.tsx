import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../lib/icons';
import { Panel } from '../dashboard/layout';
import { hub } from './hubApi';
import { attachToProvisionJob } from '../../hooks/useProvisionState';
import type { DeviceEvent, NewConnectorBody, ProbeResult } from './hubApi';
import { Tile } from './ui/Tile';

// The connect-an-app flow — the fields, the probe, the JIT-consent copy and the
// create call — lives here ONCE and is mounted in two places:
//
//   · Setup → Connectors (<NewConnectorForm>), where it sits above the list of
//     everything already connected, and
//   · the sidebar's "Connect your app" dialog (<ConnectAppDialog>), so an owner
//     with nothing connected yet can add their app from where they noticed it
//     was missing, without first learning where Setup keeps its connectors.
//
// Setup → Connectors remains the *home* of connectors: the dialog connects one
// app and then points at it (managing, permissions, appearance, removal all
// live there, and the dialog says so rather than growing its own copies).
//
// <ConnectAppFields> owns every field and reports the outcome through
// `onConnected`; the chrome around it — a Setup panel or a modal — decides what
// to show afterwards. That split is why the two mount points cannot drift.

// `input` rides along untouched: it is the JSON-schema the backend read from
// the app's OpenAPI spec, and this form is just a courier for it — dropping it
// here meant every detected tool reached the manifest argument-less.
interface ActionDraft { id: string; method: string; path: string; description: string; confirm?: boolean; access?: string; input?: Record<string, unknown> }

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
          <button type="button" className="hub-btn ghost sm" style={{ flex: '0 0 auto' }} aria-label="Remove action"
            onClick={() => setActions((x) => x.filter((_, j) => j !== i))}><Icon name="trash" /></button>
        </div>
      ))}
      <button type="button" className="hub-btn ghost sm" onClick={() => setActions((a) => [...a, { id: '', method: 'POST', path: '', description: '' }])}>
        <Icon name="plus" />Add another
      </button>
    </>
  );
}

// Derive a safe connector id from a human app name, so the user never has to
// think about slugs: "My Notes App" -> "my-notes-app".
export function slugId(name: string): string {
  return name.trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-+|-+$)/g, '')
    .replace(/-{2,}/g, '-')
    .slice(0, 32);
}
const VALID_ID = /^[a-z][a-z0-9_-]{1,31}$/;

/** What a successful connect produced — facts, not copy: each mount point says
 *  it in its own words, because "Preview / Deploy below" is true in Setup and
 *  meaningless in a dialog with no list under it. A device has no tools to
 *  preview either — it has a push token and a first reading to wait for — so
 *  the chrome has to be able to tell the two outcomes apart.
 *
 *  `jit` marks the just-in-time-consent case: Ava read the app's own API, so
 *  reads already work and nothing needed reviewing at connect time. */
export type ConnectResult =
  | { kind: 'app'; cid: string; name: string; jit: boolean; warnings: string[];
      // The connector declares tools or actions, so Ava's sandbox needs a copy
      // of them before she can use it. Creating the manifest does not put them
      // there — only an Apply does.
      needsApply: boolean }
  | { kind: 'device'; cid: string; name: string; warnings: string[] };

export function ConnectAppFields({ onCreated, onConnected }: {
  /** The connector registry changed — reload whatever lists it. */
  onCreated: () => void;
  /** Connected successfully. The fields have already cleared themselves. */
  onConnected: (r: ConnectResult) => void;
}) {
  const [name, setName] = useState('');
  const [reach, setReach] = useState('');       // a URL or a start command
  const [tokenVal, setTokenVal] = useState(''); // the app's token/API key — saved once, server-side
  const [tokenEnv, setTokenEnv] = useState(''); // optional: name an existing env var instead
  const [health, setHealth] = useState('');
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probeErr, setProbeErr] = useState('');
  // What the probe actually tried, and how each attempt failed. The backend
  // swallowed all six attempts, so "no tools found" was the answer to a TLS
  // failure, a wrong port and an expired token alike.
  const [probeTried, setProbeTried] = useState<string[]>([]);
  const [actions, setActions] = useState<ActionDraft[]>([]);
  const [isolate, setIsolate] = useState(true);
  const [dockerAvail, setDockerAvail] = useState(true);
  const [confirmAll, setConfirmAll] = useState(false);
  const [isDevice, setIsDevice] = useState(false);
  const [addToRail, setAddToRail] = useState(false);
  const [uiUrl, setUiUrl] = useState('');   // split apps: UI at a different address
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    hub.system().then((s) => { setDockerAvail(s.docker); setIsolate(s.docker); }).catch(() => {});
  }, []);

  const id = slugId(name);
  const validId = VALID_ID.test(id);
  const isUrl = reach.trim().toLowerCase().startsWith('http');

  const reset = () => {
    setName(''); setReach(''); setTokenVal(''); setTokenEnv(''); setHealth('');
    setProbe(null); setProbeErr(''); setProbeTried([]); setActions([]); setIsDevice(false); setAddToRail(false); setUiUrl('');
  };
  const setAction = (i: number, patch: Partial<ActionDraft>) =>
    setActions((a) => a.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  const runProbe = useCallback(async () => {
    if (!reach.trim()) return;
    setProbing(true); setProbe(null); setProbeErr(''); setProbeTried([]); setActions([]);
    try {
      // Detecting a start command RUNS it, so the isolation choice has to
      // travel with the request that runs it — not be confirmed afterwards.
      const body = isUrl
        ? { url: reach.trim() }
        : {
            command: reach.trim(),
            sandbox: isolate && dockerAvail ? 'docker' : 'none',
            allow_unsandboxed: !isolate || !dockerAvail,
          };
      const r = await hub.probeConnector({
        ...body,
        token_env: tokenEnv.trim() || undefined,
        token_value: tokenVal.trim() || undefined,
      });
      if (!r.ok) { setProbeErr(r.error || 'could not reach it'); setProbeTried(r.tried || []); }
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
                // The probe's per-operation JSON-schema. Mapping only the six
                // visible fields here is what used to drop it — the editor has
                // no schema UI, but the manifest downstream needs it.
                input: a.input,
              }))
            : [{ id: '', method: 'POST', path: '', description: '' }]);
        }
      }
    } catch (e) { setProbeErr((e as Error).message); }
    setProbing(false);
  }, [reach, isUrl, tokenEnv, tokenVal, isolate, dockerAvail]);

  const create = useCallback(async () => {
    setBusy(true); setMsg('');
    const nm = name.trim();
    const body: NewConnectorBody = {
      id, label: nm || undefined, probe: health.trim() || undefined,
    };
    // Credential: the NAME (optional — the backend derives one from the id when
    // a value is given without a name) and the VALUE (saved once, server-side,
    // never in the manifest). Sent top-level for every connect mode.
    body.token_env = tokenEnv.trim() || undefined;
    body.token_value = tokenVal.trim() || undefined;
    if (probe?.kind === 'mcp') {
      body.mcp = isUrl
        ? { url: reach.trim() }
        // Say which, always. `undefined` used to mean "uncontained" to the
        // manifest writer while it meant "contained" to the probe — so a command
        // DETECTED inside a container could be SAVED to run outside one. The
        // backend now defaults to containment, which makes an explicit 'none'
        // the only way to record that the owner chose otherwise.
        : { command: reach.trim(),
            sandbox: (isolate && dockerAvail) ? 'docker' : 'none' };
      if (confirmAll) body.confirm = true;
    } else if (probe?.kind === 'discover') {
      // Facade paths from /.well-known/ava.json when declared (they may not
      // live at the /tools + /call defaults).
      body.discover = { base: reach.trim(), list: probe.discover?.list, call: probe.discover?.call };
      if (confirmAll) body.confirm = true;
    } else if (!isDevice) {
      body.base_url = reach.trim() || undefined;
      body.actions = actions.filter((a) => a.id.trim() && a.path.trim());
      if (confirmAll) body.confirm = true;
    }
    if (isDevice) { body.role = 'device'; body.ingest = true; }
    if (addToRail && probe?.has_ui && !isDevice) body.ui = true;
    if (!probe?.has_ui && !isDevice && uiUrl.trim().toLowerCase().startsWith('http')) {
      body.ui_url = uiUrl.trim();   // split app: UI served from a different address
    }
    const jit = probe?.kind === 'rest' && (probe.actions?.length || 0) > 0 && !confirmAll;
    // Anything that renders an agent tool + an egress rule. A UI-only tile and a
    // push-only device render neither, and have nothing to apply.
    const needsApply = !isDevice && (
      probe?.kind === 'mcp' || probe?.kind === 'discover'
      || (body.actions?.length || 0) > 0);
    try {
      const r = await hub.newConnector(body);
      // `warnings` means the connector exists but something about it did not
      // check out. It is deliberately NOT an error — refusing the connect would
      // throw away work over a typo the owner can fix in the manifest editor —
      // but it must reach them, because "Connected" otherwise claims more than
      // anything verified.
      const warnings = r.warnings ?? [];
      if (!r.ok) { setMsg(r.error || 'could not create connector'); }
      else if (isDevice) { reset(); onCreated(); onConnected({ kind: 'device', cid: id, name: nm, warnings }); }
      else { reset(); onCreated(); onConnected({ kind: 'app', cid: id, name: nm, jit, warnings, needsApply }); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [id, name, health, reach, isUrl, tokenEnv, tokenVal, probe, actions, isolate, dockerAvail, confirmAll, isDevice, addToRail, uiUrl, onCreated, onConnected]);

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

  return (
    <>
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
          <button type="button" className="hub-btn" style={{ flex: '0 0 auto' }} onClick={runProbe} disabled={probing || !reach.trim()}>
            <Icon name={probing ? 'refresh' : 'sparkles'} />{probing ? 'Checking…' : 'Detect'}
          </button>
        </div>
        {!isUrl && reach.trim() && (
          <label className="hub-check" style={{ marginTop: 10, borderBottom: 0, paddingBottom: 0 }}>
            <input type="checkbox" checked={isolate && dockerAvail} disabled={!dockerAvail}
              onChange={(e) => setIsolate(e.target.checked)} />
            <span className="hub-check-main">
              <span className="hub-check-title">Run it in an isolated container <span style={{ color: 'var(--ok)' }}>(recommended)</span></span>
              <span className="hub-check-sub">
                {dockerAvail
                  ? 'That looks like a start command, so Detect will RUN it. A container keeps it off your files and away from Ava’s environment (read-only, resource-capped).'
                  : 'Docker isn’t installed, so this command would run directly on this host with Ava’s environment. Install Docker to contain it, or tick nothing and Detect will refuse.'}
              </span>
            </span>
          </label>
        )}
        <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 5 }}>
          {isDevice
            ? 'Leave blank for a push-only device. Add the address of its pull server (or the host adapter) if Ava should read or command it.'
            : "Paste its web address, or a command that starts it. Ava checks what it is — you don't have to know."}
        </div>
      </div>

      <div className="hub-fieldrow">
        <div className="hub-field"><label>Access token / API key <span style={{ opacity: 0.7 }}>(optional, if it needs auth)</span></label>
          <input className="hub-input" type="password" autoComplete="off" value={tokenVal}
            onChange={(e) => setTokenVal(e.target.value)} placeholder="paste your app's token" />
          <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 5 }}>
            Saved once to Ava's private secret store — <b>never</b> written to the manifest or shown to the AI.
            You won't be asked for it again on deploy. Leave blank if the app needs no login.
          </div>
        </div>
        <div className="hub-field"><label>Health check URL <span style={{ opacity: 0.7 }}>(optional — shows if it's online)</span></label>
          <input className="hub-input" value={health} onChange={(e) => setHealth(e.target.value)} placeholder="http://127.0.0.1:9000/health" /></div>
      </div>
      <div className="hub-field" style={{ maxWidth: 420 }}>
        <label>Environment variable name <span style={{ opacity: 0.7 }}>(optional — advanced)</span></label>
        <input className="hub-input" value={tokenEnv} onChange={(e) => setTokenEnv(e.target.value)}
          placeholder={validId ? `${id.toUpperCase().replace(/[^A-Z0-9]/g, '_')}_TOKEN` : 'MYAPP_TOKEN'} />
        <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 5 }}>
          Only if you already keep this token in an environment variable (e.g. <code>HASS_TOKEN</code>) and would
          rather Ava read it from there. Otherwise leave blank — Ava names and stores it for you.
        </div>
      </div>

      {probeErr && (
        <div className="hub-msg err">
          Couldn't reach it: {probeErr}. Check it's running, or add its actions manually below.
          {probeTried.length > 0 && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {probeTried.map((t) => <li key={t}>{t}</li>)}
            </ul>
          )}
        </div>
      )}

      {found && (
        <div className="hub-note" style={{ borderColor: 'color-mix(in srgb, var(--ok) 40%, transparent)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span style={{ color: 'var(--ok)', display: 'inline-flex' }}><Icon name="check" /></span>
            <b>Found {probe!.tools?.length || 0} tool{(probe!.tools?.length || 0) === 1 ? '' : 's'}</b>
            {/* The transport is interpolated only when there IS one. It used to
                be unconditional, and a probe response without the field rendered
                the literal string "MCP (undefined)" to the owner — which is what
                the published connect walkthrough shows at 0:38. The bridge always
                sets it, so this never fired against a real Ava; the demo mock did
                not, and the mock is what the camera sees. Guarding the render is
                the half that protects a user, whatever answered them. */}
            <span style={{ color: 'var(--muted)' }}>via {probe!.kind === 'mcp' ? (probe!.transport ? `MCP (${probe!.transport})` : 'MCP') : 'its tool list'} — Ava will discover and call these for you.</span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {(probe!.tools || []).slice(0, 24).map((t) => (
              <span key={t.name} className="hub-badge" title={t.description}>{t.name}</span>
            ))}
          </div>
          {probe!.kind === 'mcp' && !isUrl && (
            <div className="hub-check-sub" style={{ marginTop: 12 }}>
              {isolate && dockerAvail
                ? 'Detected inside an isolated container, and it will run that way.'
                : 'Detected by running it directly on this host.'}
            </div>
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
            {probe!.needs_auth
              ? 'It answered, but it wants a token — add one above and detect again, or tell Ava what it can do:'
              : probe!.kind === 'unknown'
                ? 'Ava couldn’t auto-detect its tools — tell it what this app can do:'
                : 'This looks like a regular web app — tell Ava what it can do:'}
          </label>
          <ActionEditor actions={actions} setAction={setAction} setActions={setActions} />
          {/* The backend has always said WHY it found nothing — an MCP command
              that exposed no tools, an app that wants a token, a spec that would
              not parse. Nothing rendered it, so every one of those arrived as the
              same blank action editor. */}
          {probe!.detail && (
            <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 6 }}>{probe!.detail}</div>
          )}
          {(probe!.tried?.length || 0) > 0 && (
            <details style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)', marginTop: 6 }}>
              <summary>What Ava tried</summary>
              <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                {probe!.tried!.map((t) => <li key={t}>{t}</li>)}
              </ul>
            </details>
          )}
        </div>
      )}

      {probe?.has_ui && !isDevice && (
        <label className="hub-check" style={{ borderBottom: 0 }}>
          <input type="checkbox" checked={addToRail} onChange={(e) => setAddToRail(e.target.checked)} />
          <span className="hub-check-main">
            <span className="hub-check-title">Add it to Ava’s sidebar</span>
            <span className="hub-check-sub">
              This app has its own web UI — Ava embeds it as a tile in the left rail.
              Unless you have set <code>apps.origin</code>, its screen runs with your Ava
              session and can reach Ava’s settings, so tick this for an app you trust.
              Setup → Connectors explains it and how to isolate them. Uncheck to connect
              only its tools.
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
        <button type="button" className="hub-btn" onClick={create} disabled={busy || !canCreate}>
          <Icon name="check" />{busy ? 'Connecting…' : 'Connect app'}
        </button>
        {!probe && !isDevice && reach.trim() && <span className="hub-msg" style={{ marginTop: 0, alignSelf: 'center', color: 'var(--muted)' }}>Click Detect first.</span>}
      </div>
      {msg && <div className="hub-msg err">{msg}</div>}
    </>
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
      <button type="button" className="hub-btn ghost sm" style={{ marginTop: 8 }} onClick={onClose}>Done</button>
    </div>
  );
}

/** Things that did not check out about a connector that WAS created. Separate
 *  from the error path on purpose: refusing the connect over a typo'd probe URL
 *  would throw away everything the owner just filled in, and the manifest editor
 *  is two clicks away. What is not acceptable is saying "Connected" and nothing
 *  else, which is what the route did — it saw these and discarded them. */
function ConnectWarnings({ notes }: { notes: string[] }) {
  if (!notes.length) return null;
  return (
    <div className="hub-msg err" style={{ marginTop: 12 }}>
      <b>Connected, but check {notes.length === 1 ? 'this' : 'these'}:</b>
      <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
        {notes.map((n, i) => <li key={i}>{n}</li>)}
      </ul>
      <div style={{ marginTop: 6, opacity: 0.85 }}>
        Ava will not be able to use the app properly until this is fixed — edit it
        from the connector’s ⋯ menu below.
      </div>
    </div>
  );
}

/** The step between "connected" and "Ava can use it".
 *
 *  Creating a connector writes ONE file: its manifest. The agent tools and the
 *  egress rule that let Ava actually call the app are rendered from that
 *  manifest and pushed into her sandbox by an Apply — which is why the dialog
 *  saying "Connected — reads work now" was a claim about a thing that had not
 *  happened. Deny-by-default means the opposite was true: until this runs, every
 *  call Ava makes to the app is refused by the sandbox.
 *
 *  It runs the same single-slot job as Setup → Agent's Apply, so a run started
 *  here shows up there and vice versa. */
function ApplyToAgent({ cid, name }: { cid: string; name: string }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');

  const apply = useCallback(async () => {
    setBusy(true); setErr(''); setMsg('');
    try {
      const r = await hub.deployConnector(cid);
      if (!r.ok) setErr(r.error || r.detail || 'could not apply it to the agent');
      else if (r.running) {
        setMsg('Applying…');
        await attachToProvisionJob();
        setMsg(`Ava has ${name}'s tools now.`);
      } else setMsg(r.detail || 'Done.');
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  }, [cid, name]);

  if (msg && !busy) return <div className="hub-msg ok" style={{ marginTop: 10 }}>{msg}</div>;
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--muted)' }}>
        Ava can reach it from here, but her sandbox doesn’t have its tools yet — that’s one Apply.
      </div>
      <div className="hub-btn-row" style={{ marginTop: 6 }}>
        <button type="button" className="hub-btn" onClick={apply} disabled={busy}>
          <Icon name="check" />{busy ? 'Applying…' : 'Apply to the agent'}
        </button>
      </div>
      {err && <div className="hub-msg err" style={{ marginTop: 6 }}>{err}</div>}
    </div>
  );
}

/** Setup → Connectors' mount: a collapsed button that opens the fields in a
 *  panel above the connector list. Connecting leaves the form open and cleared
 *  (Cancel reveals the confirmation beside the button) — the list below is the
 *  proof, and an owner adding three apps in a row keeps their place. */
export function NewConnectorForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState('');
  const [notes, setNotes] = useState<string[]>([]);
  const [verify, setVerify] = useState<{ cid: string; name: string } | null>(null);

  const connected = (r: ConnectResult) => {
    setNotes(r.warnings);
    if (r.kind === 'device') setVerify({ cid: r.cid, name: r.name });
    // "reads work now" said two things, and got both wrong: reads did not work
    // yet (nothing was in the sandbox), and the JIT tier is about CONSENT, not
    // reachability. Say what is true — the app is connected — and point at the
    // step that makes it usable.
    else setDone(r.needsApply
      ? `Connected “${r.name}” — Deploy below to give Ava its tools.`
      : `Connected “${r.name}”.`);
  };

  if (!open) {
    return (
      <>
        <div className="hub-btn-row" style={{ marginTop: 0 }}>
          <button type="button" className="hub-btn" onClick={() => setOpen(true)}><Icon name="plus" />Connect an app or device</button>
          {done && <span className="hub-msg ok" style={{ marginTop: 0, alignSelf: 'center' }}>{done}</span>}
        </div>
        {verify && <DeviceVerify cid={verify.cid} name={verify.name} onClose={() => setVerify(null)} />}
      </>
    );
  }
  return (
    <Panel title="Connect an app" subtitle="Tell Ava where your app is — it figures out how to talk to it and writes the setup. You'll preview the tools and the security policy before anything goes live." right={
      <button type="button" className="hub-btn ghost sm" onClick={() => setOpen(false)}>Cancel</button>
    }>
      <ConnectAppFields onCreated={onCreated} onConnected={connected} />
      <ConnectWarnings notes={notes} />
      {verify && <DeviceVerify cid={verify.cid} name={verify.name} onClose={() => setVerify(null)} />}
    </Panel>
  );
}

/** The sidebar's mount: the same fields in a modal, so connecting an app never
 *  starts with "where in Setup is that?". Everything AFTER connecting — the
 *  permissions sheet, the appearance picker, removal — stays in Setup →
 *  Connectors, and the foot links there rather than growing a second copy. */
export function ConnectAppDialog({ onClose, onConnected }: {
  onClose: () => void;
  /** The app registry changed — the rail should redraw. */
  onConnected?: () => void;
}) {
  const [result, setResult] = useState<ConnectResult | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // Escape closes, and Tab stays inside: `aria-modal` tells a screen reader the
  // page behind is inert, so tabbing into the sidebar underneath it would be a
  // straight contradiction. The fields autofocus their first input; on the
  // confirmation step nothing does, and focus lands on <body> — hence the
  // "outside the dialog" case rather than a plain edge-wrap.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose(); return; }
      if (e.key !== 'Tab' || !box.current) return;
      const stops = [...box.current.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])',
      )].filter((el) => el.offsetParent !== null);
      if (!stops.length) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const here = document.activeElement;
      if (!box.current.contains(here)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
      else if (e.shiftKey && here === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && here === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      opener?.focus?.();   // back to the row that opened it, not the document
    };
  }, [onClose]);

  // The registry changed: /api/apps is what draws the rail, and the connector
  // list in Setup listens to the same event.
  const created = useCallback(() => {
    window.dispatchEvent(new Event('ava:apps-changed'));
    onConnected?.();
  }, [onConnected]);

  return createPortal(
    // The backdrop is a convenience, not the close affordance — Escape and the
    // labelled × button are, and they are what a keyboard user reaches. It
    // closes on mousedown on ITSELF only, so a drag that starts inside the form
    // (selecting a token, say) and ends out here does not dismiss the dialog
    // with the typed values in it.
    <div className="connect-modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="connect-modal" role="dialog" aria-modal="true" aria-labelledby="connect-modal-title" ref={box}>
        <header className="connect-modal-head">
          <Tile icon="plug" tone="accent" size={34} />
          <div className="connect-modal-titles">
            <h2 id="connect-modal-title">Connect your app</h2>
            {/* The subtitle is an instruction, so it goes once the instruction
                has been carried out — the confirmation below says the rest. */}
            {!result && <p>Tell Ava where it is — Ava works out how to talk to it and writes the setup.</p>}
          </div>
          <button type="button" className="ibtn" aria-label="Close" onClick={onClose}><Icon name="close" /></button>
        </header>

        <div className="connect-modal-body">
          {result == null ? (
            <ConnectAppFields onCreated={created} onConnected={setResult} />
          ) : result.kind === 'device' ? (
            <DeviceVerify cid={result.cid} name={result.name} onClose={onClose} />
          ) : (
            <div className="hub-note" style={{ borderColor: 'color-mix(in srgb, var(--ok) 40%, transparent)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ color: 'var(--ok)', display: 'inline-flex' }}><Icon name="check" /></span>
                <b>Connected “{result.name}”.</b>
              </div>
              <div style={{ marginTop: 6 }}>
                {result.jit && 'Reading won’t interrupt you; Ava asks the first time it needs to change anything. '}
                If it has its own web page it is in your sidebar already. Its tools, permissions
                and appearance live in Setup → Connectors.
              </div>
              {result.needsApply && <ApplyToAgent cid={result.cid} name={result.name} />}
              <div className="hub-btn-row" style={{ marginTop: 10 }}>
                <button type="button" className="hub-btn" onClick={onClose}>Done</button>
                <button type="button" className="hub-btn ghost" onClick={() => setResult(null)}>
                  <Icon name="plus" />Connect another
                </button>
              </div>
            </div>
          )}
        </div>

        <footer className="connect-modal-foot">
          <a href="#hub/connectors" onClick={onClose}>Manage connected apps in Setup → Connectors<Icon name="arrowRight" /></a>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
