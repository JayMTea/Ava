import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../lib/icons';
import { RowMenu, type MenuAction } from '../../lib/RowMenu';
import {
  ACCENT_SLOTS, APP_ICONS, AppDot, appAccent, appById, appForTool, appIcon, appsForTools,
} from '../../lib/appColor';
import { MarkdownLite } from '../../lib/markdown';
import { ago, EmptyState, Panel } from '../dashboard/primitives';
import { api } from '../../lib/api';
import { MemoryPanel } from './MemoryPanel';
import { hub } from './hubApi';
import type {
  AgentStatus, AuditEvent, Backend, BackendList, BackendProbe, BackendTestResult, BenchResult,
  BenchStatus, ConnectorLoadError, CostSettings, DeviceEvent, EnrollResult, GenerateResult,
  GrantAction, HardwareInfo, HubConnector, IngestToken, ModelStore,
  NewConnectorBody, PendingApproval, ProbeResult, PullStatus, Skill, SkillList, SystemInfo, VoiceStatus,
} from './hubApi';

// Engine presets for the "add a model" form: label + default OpenAI-compatible
// base URL + whether it's a local engine (local engines need no API key). Ava
// talks to any of these the same way — an OpenAI-compatible /v1 endpoint.
const ENGINE_PRESETS: { value: string; label: string; base: string; cloud?: boolean }[] = [
  { value: 'ollama', label: 'Ollama', base: 'http://127.0.0.1:11434/v1' },
  { value: 'mlx', label: 'MLX (Apple Silicon)', base: 'http://127.0.0.1:8080/v1' },
  { value: 'lmstudio', label: 'LM Studio', base: 'http://127.0.0.1:1234/v1' },
  { value: 'llamacpp', label: 'llama.cpp', base: 'http://127.0.0.1:8080/v1' },
  { value: 'vllm', label: 'vLLM (NVIDIA)', base: 'http://127.0.0.1:8002/v1' },
  { value: 'openai', label: 'Cloud (OpenAI-compatible)', base: 'https://api.openai.com/v1', cloud: true },
];

// ─────────────────────────────────────────────────────────────────────────────
// Approvals banner — the agent parked a sensitive action; the operator decides.
// Polls so it appears on any Hub tab while a call is blocked waiting.
// ─────────────────────────────────────────────────────────────────────────────
function ApprovalsBanner() {
  const [pending, setPending] = useState<PendingApproval[]>([]);
  useEffect(() => {
    let alive = true;
    const tick = () => hub.approvals().then((r) => { if (alive) setPending(r.pending); }).catch(() => {});
    tick();
    const t = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const decide = async (id: string, decision: 'approve' | 'always' | 'deny') => {
    setPending((p) => p.filter((x) => x.id !== id));
    try { await hub.decideApproval(id, decision); } catch { /* it may have timed out */ }
  };
  if (!pending.length) return null;
  return (
    <div style={{ marginBottom: 16 }}>
      {pending.map((p) => (
        <div key={p.id} className="hub-restart" style={{
          background: 'rgba(0,122,204,0.10)', color: 'var(--txt)',
          borderColor: 'color-mix(in srgb, var(--accent) 45%, transparent)',
          justifyContent: 'space-between', flexWrap: 'wrap', gap: 10,
        }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            <span style={{ color: 'var(--accent)', display: 'inline-flex' }}><Icon name="lock" /></span>
            <span>
              <b>Approve action?</b> Ava wants to run <code>{p.action}</code> on <b>{p.connector}</b>
              {Object.keys(p.args).length > 0 && (
                <span style={{ color: 'var(--muted)' }}> · {Object.entries(p.args).map(([k, v]) => `${k}=${v}`).join(', ')}</span>
              )}
              {p.access === 'destructive' && (
                <span style={{ color: 'var(--muted)' }}> · destructive — asks every time</span>
              )}
              {p.access === 'physical' && (
                <span style={{ color: 'var(--muted)' }}> · physical action — moves something in the real world; asks every time</span>
              )}
            </span>
          </span>
          <span style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
            {p.grantable && (
              <button className="hub-btn sm" onClick={() => decide(p.id, 'always')}
                title="Run it now and never ask again for this action — revoke anytime in the connector's settings">
                <Icon name="check" />Always allow</button>
            )}
            <button className={'hub-btn sm' + (p.grantable ? ' ghost' : '')} onClick={() => decide(p.id, 'approve')}>
              <Icon name="check" />{p.grantable ? 'Just once' : 'Approve'}</button>
            <button className="hub-btn ghost sm" onClick={() => decide(p.id, 'deny')}><Icon name="close" />Deny</button>
          </span>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared bits
// ─────────────────────────────────────────────────────────────────────────────
type TabId = 'overview' | 'hardware' | 'agent' | 'connectors' | 'voice' | 'memory' | 'budgets' | 'history' | 'system';

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'gauge' },
  { id: 'hardware', label: 'Hardware', icon: 'chart' },
  { id: 'agent', label: 'Agent', icon: 'bot' },
  { id: 'connectors', label: 'Connectors', icon: 'panel' },
  { id: 'voice', label: 'Voice', icon: 'mic' },
  { id: 'memory', label: 'Memory', icon: 'db' },
  { id: 'budgets', label: 'Budgets', icon: 'chart' },
  { id: 'history', label: 'History', icon: 'activity' },
  { id: 'system', label: 'System', icon: 'sliders' },
];

// The Hub sub-tab is kept in the URL hash as a second segment (#hub/<tab>) so a
// refresh or a bookmark lands back where you were. App.tsx's top-level router
// reads only the FIRST segment (`viewFromHash` does split('/')[0]), so this
// segment is invisible to it — no coupling, no fight over the hash.
const TAB_IDS = TABS.map((t) => t.id);

function tabFromHash(): TabId {
  if (typeof window === 'undefined') return 'overview';
  const parts = window.location.hash.replace(/^#\/?/, '').split('/');
  if (parts[0] !== 'hub') return 'overview';
  return (TAB_IDS as string[]).includes(parts[1]) ? (parts[1] as TabId) : 'overview';
}

function writeTabHash(t: TabId): void {
  // Overview is the default, so keep its URL clean as plain #hub.
  const next = t === 'overview' ? 'hub' : `hub/${t}`;
  if (window.location.hash.replace(/^#\/?/, '') !== next) window.location.hash = next;
}

function Badge({ tone, children }: { tone?: 'ok' | 'warn' | 'err' | 'accent' | 'muted'; children: React.ReactNode }) {
  return <span className={'hub-badge' + (tone ? ' ' + tone : '')}><i />{children}</span>;
}

function RestartBanner({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <div className="hub-restart">
      <Icon name="refresh" />
      <span>Saved to <b>ava.yaml</b>. Restart Ava to apply the change.</span>
    </div>
  );
}

// Kinds that are internal plumbing (bridge, inference router) or models
// (vLLM Omni, the GPU service) — they run behind the scenes / live in the Models tab,
// not on the Connectors page, which is only for external apps the user wires in.
const INTERNAL_KINDS = new Set(['core', 'inference', 'media']);
const isExternalApp = (c: HubConnector): boolean => !INTERNAL_KINDS.has(c.kind);

// ─────────────────────────────────────────────────────────────────────────────
// Overview
// ─────────────────────────────────────────────────────────────────────────────
function Overview({ onGo }: { onGo: (t: TabId) => void }) {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [agent, setAgent] = useState<AgentStatus | null>(null);
  const [conns, setConns] = useState<HubConnector[]>([]);
  const [backends, setBackends] = useState<BackendProbe | null>(null);
  const [hw, setHw] = useState<HardwareInfo | null>(null);

  useEffect(() => {
    hub.system().then(setSys).catch(() => {});
    hub.agentStatus().then(setAgent).catch(() => {});
    hub.connectors().then((r) => setConns(r.connectors)).catch(() => {});
    hub.backends().then(setBackends).catch(() => {});
    hub.hardware().then(setHw).catch(() => {});
  }, []);

  const engineUp = backends && (backends.vllm || backends.ollama);
  const enabledConns = conns.filter((c) => c.enabled && isExternalApp(c)).length;

  const card = (t: TabId, icon: string, title: string, value: React.ReactNode, sub: string) => (
    <button className="ov-card" onClick={() => onGo(t)}>
      <span className="ov-card-ic" aria-hidden="true"><Icon name={icon} /></span>
      <span className="ov-card-body">
        <span className="ov-card-title">{title}</span>
        <span className="ov-card-val">{value}</span>
        <span className="ov-card-sub">{sub}</span>
      </span>
      <span className="ov-card-go" aria-hidden="true"><Icon name="arrowRight" /></span>
    </button>
  );

  return (
    <>
      <Panel title="Welcome" subtitle="Set up and control everything from here — no terminal required.">
        <p className="hub-note" style={{ border: 0, padding: 0, background: 'none' }}>
          Each tab configures one piece: your <b>hardware</b>, the <b>agent</b> — its model (brain),
          tools, and memory — the <b>connectors</b> that wire in your apps, and <b>system</b> settings
          like self-editing governance. Changes are written to <b>ava.yaml</b> — never to source.
        </p>
      </Panel>

      <div className="hub-section" />
      <div className="ov-cards">
        {card('hardware', 'chart', 'Hardware',
          hw ? <Badge tone="accent">{hw.tier} tier</Badge> : <Badge>detecting…</Badge>,
          hw?.gpu ? hw.gpu : 'GPU · memory · model tier')}
        {card('agent', 'bot', 'Agent',
          agent?.available ? <Badge tone="ok">{agent.name} ready</Badge>
            : agent?.enabled === false ? <Badge tone="muted">disabled</Badge>
              : <Badge tone="warn">not provisioned</Badge>,
          engineUp ? 'model (engine up) · tools · memory' : 'model · tools · memory · sandbox')}
        {card('connectors', 'panel', 'Connectors',
          <Badge tone="accent">{enabledConns} enabled</Badge>,
          'apps Ava monitors & drives')}
        {card('system', 'sliders', 'Governance',
          sys ? <Badge tone={sys.code_approval === 'all' ? 'ok' : sys.code_approval === 'none' ? 'warn' : 'accent'}>approval: {sys.code_approval}</Badge> : <Badge>…</Badge>,
          'self-editing · learning · voice')}
      </div>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Models
// ─────────────────────────────────────────────────────────────────────────────
function HardwarePanel() {
  const [hw, setHw] = useState<HardwareInfo | null>(null);

  useEffect(() => { hub.hardware().then(setHw).catch(() => {}); }, []);

  return (
    <Panel title="Your hardware"
      subtitle="Detected automatically — it sets the recommended model tier. Pick and download the model itself under the Agent tab.">
      {hw ? (
        <>
          <div className="hw-hero">
            <span className="hw-hero-ic" aria-hidden="true"><Icon name="chart" /></span>
            <div className="hw-hero-body">
              <div className="hw-hero-tier">{hw.tier} tier</div>
              <div className="hw-hero-hint">{hw.hint}</div>
            </div>
          </div>
          <dl className="hub-kv" style={{ marginTop: 16 }}>
            <dt>Compute</dt><dd>{hw.gpu || 'No local GPU detected'}</dd>
            <dt>Usable memory</dt><dd>{hw.fit_gb != null ? `${hw.fit_gb} GB · ${hw.source || 'detected'}` : '—'}</dd>
          </dl>
          <div className="hub-note" style={{ marginTop: 14 }}>
            The tier sets which models Ava recommends. Pick and download one under the <b>Agent</b> tab.
          </div>
        </>
      ) : <EmptyState text="Detecting hardware…" />}
    </Panel>
  );
}

// The multi-model "brain" manager: link one or more OpenAI-compatible models
// (local engines or any cloud provider), test each before committing, and pick
// which one is Ava's brain. Cloud keys go to the secrets store, never ava.yaml.
function BrainManager({ onRestart }: { onRestart: () => void }) {
  const [list, setList] = useState<BackendList | null>(null);
  const [be, setBe] = useState<BackendProbe | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);

  // form state
  const [id, setId] = useState('');
  const [engine, setEngine] = useState('ollama');
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:11434/v1');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [makeBrain, setMakeBrain] = useState(false);
  const [test, setTest] = useState<BackendTestResult | null>(null);
  const [testing, setTesting] = useState(false);
  // The agent sandbox's own model: while the agent runtime is active, THAT is
  // what chat turns think with — the backends below only serve the tool-less
  // fallback and router roles. Without pinning it here, this panel reads
  // "no model linked" on a machine where Ava is plainly answering.
  const [agent, setAgent] = useState<AgentStatus | null>(null);
  // The router's live route: when nothing is configured it serves a built-in
  // default (`implicit`) — with the agent off, THAT is the operative brain.
  const [route, setRoute] = useState<{ backends?: { id: string; label: string; model: string; implicit?: boolean }[] } | null>(null);

  const preset = ENGINE_PRESETS.find((p) => p.value === engine) ?? ENGINE_PRESETS[0];
  const isCloud = !!preset.cloud;

  const load = useCallback(() => { hub.backendList().then(setList).catch(() => {}); }, []);
  useEffect(() => {
    load();
    hub.backends().then(setBe).catch(() => {});
    hub.agentStatus().then(setAgent).catch(() => {});
    api.getModel().then(setRoute).catch(() => {});
  }, [load]);

  const resetForm = useCallback(() => {
    setEditing(null); setId(''); setEngine('ollama');
    setBaseUrl('http://127.0.0.1:11434/v1'); setModel(''); setApiKey('');
    setMakeBrain(false); setTest(null); setMsg('');
  }, []);

  const openAdd = useCallback(() => {
    resetForm();
    setMakeBrain(!list?.backends.length); // first model is the brain by default
    setShowForm(true);
  }, [resetForm, list]);

  const openEdit = useCallback((b: Backend) => {
    setEditing(b.id); setId(b.id); setEngine(b.engine);
    setBaseUrl(b.base_url); setModel(b.model); setApiKey('');
    setMakeBrain(b.is_brain); setTest(null); setMsg(''); setShowForm(true);
  }, []);

  // Changing the engine swaps in that engine's default endpoint (unless editing
  // an existing backend, where we keep the user's URL).
  const onEngine = useCallback((v: string) => {
    setEngine(v); setTest(null);
    if (!editing) {
      const p = ENGINE_PRESETS.find((x) => x.value === v);
      if (p) setBaseUrl(p.base);
    }
  }, [editing]);

  const runTest = useCallback(async () => {
    setTesting(true); setTest(null); setMsg('');
    try {
      const r = await hub.backendTest({
        id: editing || id, base_url: baseUrl.trim(), model: model.trim(),
        api_key: apiKey.trim() || undefined,
      });
      setTest(r);
    } catch (e) { setTest({ ok: false, error: (e as Error).message }); }
    setTesting(false);
  }, [editing, id, baseUrl, model, apiKey]);

  const save = useCallback(async () => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.backendSave({
        id: (editing || id).trim(), engine, base_url: baseUrl.trim(),
        model: model.trim(), api_key: apiKey.trim() || undefined, make_brain: makeBrain,
      });
      if (!r.ok) { setMsg(r.error || 'could not save'); }
      else { setShowForm(false); resetForm(); load(); onRestart(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [editing, id, engine, baseUrl, model, apiKey, makeBrain, resetForm, load, onRestart]);

  const setBrain = useCallback(async (bid: string) => {
    try { const r = await hub.backendBrain(bid); if (r.ok) { load(); onRestart(); } }
    catch { /* surfaced on next load */ }
  }, [load, onRestart]);

  const remove = useCallback(async (bid: string) => {
    if (!window.confirm(`Remove the "${bid}" model?`)) return;
    try { const r = await hub.backendDelete(bid); if (r.ok) { load(); onRestart(); } }
    catch { /* surfaced on next load */ }
  }, [load, onRestart]);

  const backends = list?.backends ?? [];
  // While the agent runtime is active, ITS sandbox model is what chat turns
  // actually think with (set by `nemoclaw onboard`) — pin it as the effective
  // brain so this panel never claims "no model" on a working install.
  const agentBrain = agent?.available && agent.sandbox_model ? agent : null;
  // Agent off + nothing configured: the router still serves its built-in
  // default, so chat works — show that instead of "no model linked".
  const routerDefault = !agentBrain && backends.length === 0
    ? (route?.backends || []).find((b) => b.implicit) || null : null;

  return (
    <Panel
      title="Ava's brain"
      subtitle="Link any model — a local engine (Ollama, MLX, LM Studio, llama.cpp, vLLM) or any OpenAI-compatible cloud provider — and pick which one Ava thinks with."
      right={<button className="hub-btn sm" onClick={openAdd}><Icon name="sparkles" />Add a model</button>}
    >
      {agentBrain && (
        <div className="hub-model-list" style={{ marginBottom: backends.length || showForm ? 10 : 0 }}>
          <div className="hub-opt sel" style={{ display: 'flex', alignItems: 'center', gap: 12, cursor: 'default' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <b style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                {agentBrain.sandbox_model!.split('/').pop()}
                <Badge tone="accent">brain</Badge>
                <Badge tone="muted">agent sandbox</Badge>
              </b>
              <small style={{ color: 'var(--muted)', wordBreak: 'break-all' }}>
                {agentBrain.sandbox_model} · {agentBrain.sandbox_provider || 'sandbox provider'} · sandbox “{agentBrain.sandbox}”
                — the agent thinks with this; change it via <b>nemoclaw onboard</b>.
                Models linked below serve the tool-less fallback and other roles.
              </small>
            </div>
          </div>
        </div>
      )}
      {routerDefault && (
        <div className="hub-model-list" style={{ marginBottom: showForm ? 10 : 0 }}>
          <div className="hub-opt sel" style={{ display: 'flex', alignItems: 'center', gap: 12, cursor: 'default' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <b style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                {routerDefault.label}
                <Badge tone="accent">brain</Badge>
                <Badge tone="muted">built-in default</Badge>
              </b>
              <small style={{ color: 'var(--muted)', wordBreak: 'break-all' }}>
                {routerDefault.model} — nothing is configured, so the router serves this
                default. Add a model below to choose your own.
              </small>
            </div>
          </div>
        </div>
      )}
      {list == null ? <EmptyState text="Loading models…" />
        : backends.length === 0 && !showForm
          ? (routerDefault ? null : <EmptyState text={agentBrain
              ? 'No fallback model linked — optional: add one so chat still works if the agent runtime is stopped.'
              : 'No model linked yet — click “Add a model” to connect Ava\'s brain.'} />)
          : (
            <div className="hub-model-list">
              {backends.map((b) => (
                <div key={b.id} className={'hub-opt' + (b.is_brain ? ' sel' : '')}
                     style={{ display: 'flex', alignItems: 'center', gap: 12, cursor: 'default' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <b style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {b.label}
                      {b.is_brain && (agentBrain
                        ? <Badge tone="muted">fallback brain</Badge>
                        : <Badge tone="accent">brain</Badge>)}
                    </b>
                    <small style={{ color: 'var(--muted)', wordBreak: 'break-all' }}>
                      {b.engine} · {b.model || 'no model set'} · {b.base_url}
                      {!b.local && (b.has_key ? ' · key ✓' : ' · no key')}
                    </small>
                  </div>
                  {!b.is_brain && (
                    <button className="hub-btn sm ghost" onClick={() => setBrain(b.id)}>Use as brain</button>
                  )}
                  <button className="hub-btn sm ghost" onClick={() => openEdit(b)}>Edit</button>
                  <button className="hub-btn sm ghost" onClick={() => remove(b.id)} aria-label={`Remove ${b.id}`}>
                    <Icon name="trash" />
                  </button>
                </div>
              ))}
            </div>
          )}

      {showForm && (
        <div className="hub-model-form" style={{ marginTop: 14, borderTop: '1px solid var(--line)', paddingTop: 14 }}>
          <div className="hub-fieldrow">
            <div className="hub-field"><label>Name</label>
              <input className="hub-input" value={id} disabled={!!editing}
                     onChange={(e) => setId(e.target.value)} placeholder="e.g. my-openai" /></div>
            <div className="hub-field"><label>Engine</label>
              <select className="hub-select" value={engine} onChange={(e) => onEngine(e.target.value)}>
                {ENGINE_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select></div>
          </div>
          <div className="hub-fieldrow">
            <div className="hub-field"><label>Base URL</label>
              <input className="hub-input" value={baseUrl} onChange={(e) => { setBaseUrl(e.target.value); setTest(null); }} /></div>
            <div className="hub-field"><label>Model</label>
              <input className="hub-input" value={model} onChange={(e) => { setModel(e.target.value); setTest(null); }}
                     placeholder={isCloud ? 'e.g. gpt-4o-mini' : 'e.g. llama3.1:70b'} /></div>
          </div>
          {isCloud && (
            <div className="hub-field"><label>API key <span style={{ opacity: 0.7 }}>(stored in secrets/, never in ava.yaml)</span></label>
              <input className="hub-input" type="password" value={apiKey}
                     onChange={(e) => { setApiKey(e.target.value); setTest(null); }}
                     placeholder={editing ? 'leave blank to keep the saved key' : ''} /></div>
          )}
          <label className="hub-check" style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '10px 0' }}>
            <input type="checkbox" checked={makeBrain} onChange={(e) => setMakeBrain(e.target.checked)} />
            Use this as Ava's brain
          </label>

          {test && (
            <div className={'hub-msg ' + (test.ok ? 'ok' : 'err')}>
              {test.ok
                ? `✓ Connected${test.ms != null ? ` (${(test.ms / 1000).toFixed(2)} s)` : ''}${test.reply ? ` — replied “${test.reply}”` : ''}`
                : `✗ ${test.error || 'failed'}`}
            </div>
          )}

          <div className="hub-btn-row">
            <button className="hub-btn ghost" onClick={runTest} disabled={testing || !baseUrl.trim() || !model.trim()}>
              {testing ? 'Testing…' : 'Test connection'}
            </button>
            <button className="hub-btn" onClick={save} disabled={busy || !(editing || id).trim() || !baseUrl.trim() || !model.trim()}>
              <Icon name="check" />{busy ? 'Saving…' : 'Save model'}
            </button>
            <button className="hub-btn ghost" onClick={() => { setShowForm(false); resetForm(); }} disabled={busy}>Cancel</button>
          </div>
          {msg && <div className="hub-msg err">{msg}</div>}
          {be && !be.ollama && !be.vllm && !isCloud && (
            <div className="hub-msg" style={{ color: 'var(--muted)' }}>
              No local engine detected. Start one first (e.g. install Ollama and run <code>ollama serve</code>), or link a cloud provider.
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function ModelStorePanel() {
  const [store, setStore] = useState<ModelStore | null>(null);
  const [pull, setPull] = useState<PullStatus | null>(null);
  const [msg, setMsg] = useState('');
  const logRef = useRef<HTMLPreElement>(null);

  const load = useCallback(() => { hub.models().then(setStore).catch(() => {}); }, []);
  useEffect(() => { load(); hub.pullStatus().then(setPull).catch(() => {}); }, [load]);

  // Poll while a pull runs; refresh the list when it finishes.
  useEffect(() => {
    if (pull?.status !== 'running') return;
    const t = setInterval(async () => {
      try {
        const s = await hub.pullStatus();
        setPull(s);
        if (s.status !== 'running') load();
      } catch { /* keep last state */ }
    }, 1500);
    return () => clearInterval(t);
  }, [pull?.status, load]);

  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [pull?.log?.length]);

  const start = useCallback(async (role: string) => {
    setMsg('');
    try {
      const r = await hub.pull(role);
      if (!r.ok) { setMsg(r.error || 'could not start'); return; }
      setPull({ status: 'running', role: role || 'auto', rc: null, log: [] });
    } catch (e) { setMsg((e as Error).message); }
  }, []);

  const running = pull?.status === 'running';

  return (
    <Panel
      title="Model store"
      subtitle={store ? `Downloads land in ${store.store} · detected tier: ${store.detected_tier}${store.available_gb ? ` · ${store.available_gb} GB` : ''}` : 'Download models sized to your hardware.'}
      right={
        <button className="hub-btn sm" onClick={() => start('auto')} disabled={running}>
          <Icon name="sparkles" />{running ? 'Pulling…' : 'Pull recommended'}
        </button>
      }
    >
      {store == null ? <EmptyState text="Loading model store…" />
        : store.roles.length === 0 ? <EmptyState text="No models declared in ava.yaml (models: …) — 'Pull recommended' picks one for your tier." />
          : store.roles.map((m) => (
            <div className="hub-row" key={m.role}>
              <div className="hub-row-main">
                <div className="hub-row-title">{m.role} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>· {m.id}</span></div>
                <div className="hub-row-sub">{m.engine}{m.tier ? ` · tier ${m.tier}` : ''}</div>
              </div>
              <div className="hub-row-actions">
                {m.present ? <Badge tone="ok">downloaded</Badge> : (
                  <button className="hub-btn ghost sm" onClick={() => start(m.role)} disabled={running}>
                    <Icon name="cloud" />Pull
                  </button>
                )}
              </div>
            </div>
          ))}

      {pull && pull.status !== 'idle' && (
        <div className="hub-preview" style={{ marginTop: 12 }}>
          <div className="hub-preview-head">
            <Icon name={running ? 'refresh' : pull.status === 'done' ? 'check' : 'close'} />
            pull {pull.role} · {pull.status}{pull.rc != null && pull.status === 'error' ? ` (exit ${pull.rc})` : ''}
          </div>
          <pre ref={logRef}>{pull.log.length ? pull.log.join('\n') : 'starting…'}</pre>
        </div>
      )}
      {msg && <div className="hub-msg err">{msg}</div>}
      <div className="hub-section" />
      <BenchPanel />
    </Panel>
  );
}

// Compare-panel scale/reference constants. Bars are absolute-but-adaptive: the
// track scales to the fastest in the set (never below a floor, so a lone model
// still fills a meaningful amount), and a faint marker shows the "good enough"
// threshold so a single-model baseline still reads as fast/slow.
const TOKS_MIN_SCALE = 30;    // tok/s — bar track never scales below this
const TTFT_MAX_SCALE = 1000;  // ms — TTFT track floor
const TOKS_GOOD = 15;         // interactive throughput marker
const TTFT_GOOD = 500;        // snappy time-to-first-token marker
const clampPct = (n: number) => Math.max(0, Math.min(100, n));

function BenchBar({ pct, mark, kind, markTitle }: {
  pct: number; mark: number; kind: 'tok' | 'ttft'; markTitle: string;
}) {
  return (
    <div className="bench-bar">
      <span className={'bench-bar-fill ' + kind} style={{ width: pct + '%' }} />
      {mark > 1 && mark < 99 && <i className="bench-mark" style={{ left: mark + '%' }} title={markTitle} />}
    </div>
  );
}

function BenchTable({ results, winner }: { results: BenchResult[]; winner?: string | null }) {
  const okv = results.filter((r) => r.ok);
  const maxTok = Math.max(TOKS_MIN_SCALE, ...okv.map((r) => r.tok_s || 0));
  const maxTtft = Math.max(TTFT_MAX_SCALE, ...okv.map((r) => r.ttft_ms || 0));
  // Best throughput first; failed backends sink to the bottom.
  const sorted = [...results].sort((a, b) =>
    a.ok !== b.ok ? (a.ok ? -1 : 1) : (b.tok_s || 0) - (a.tok_s || 0));
  const tokMark = clampPct((TOKS_GOOD / maxTok) * 100);
  const ttftMark = clampPct((1 - TTFT_GOOD / maxTtft) * 100);

  return (
    <div className="bench-cmp">
      {sorted.map((r) => (
        <div className={'bench-row' + (r.id === winner ? ' win' : '') + (r.ok ? '' : ' err')} key={r.id}>
          <div className="bench-name">
            {r.id === winner && <Badge tone="ok">fastest</Badge>}
            <span className="bench-id" title={r.model || r.id}>{r.model || r.id}</span>
            {r.engine && <span className="bench-eng">{r.engine}</span>}
          </div>
          {r.ok ? (
            <>
              <div className="bench-metric">
                <div className="bench-metric-head">
                  <b>{r.tok_s}</b> tok/s
                  {r.estimated_tokens && <span className="bench-est" title="tokens/sec estimated — the endpoint didn't report token usage">est.</span>}
                </div>
                <BenchBar pct={clampPct(((r.tok_s || 0) / maxTok) * 100)} mark={tokMark}
                  kind="tok" markTitle="interactive ≥ 15 tok/s" />
              </div>
              <div className="bench-metric">
                <div className="bench-metric-head"><b>{r.ttft_ms != null ? (r.ttft_ms / 1000).toFixed(2) : '—'}</b> s TTFT</div>
                <BenchBar pct={clampPct((1 - (r.ttft_ms || 0) / maxTtft) * 100)} mark={ttftMark}
                  kind="ttft" markTitle="snappy ≤ 0.5 s" />
              </div>
            </>
          ) : (
            <div className="bench-fail"><Icon name="alert" /> {r.error || 'no response'}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function BenchPanel() {
  const [bench, setBench] = useState<BenchStatus | null>(null);
  const [prompt, setPrompt] = useState('');
  const [msg, setMsg] = useState('');
  const load = useCallback(() => { hub.benchStatus().then(setBench).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (bench?.status !== 'running') return;
    const t = setInterval(() => hub.benchStatus().then(setBench).catch(() => {}), 1200);
    return () => clearInterval(t);
  }, [bench?.status]);

  const run = useCallback(async () => {
    setMsg('');
    try {
      const r = await hub.bench(prompt);
      if (!r.ok) { setMsg(r.error || 'could not start'); return; }
      setBench({ status: 'running', result: null });
    } catch (e) { setMsg((e as Error).message); }
  }, [prompt]);

  const running = bench?.status === 'running';
  const res = bench?.result;
  const results = res?.results || [];
  // Skeleton rows for backends still being measured (or one placeholder before
  // the first result lands so the run never looks stalled).
  const pending = running ? (res?.pending ?? (res ? 0 : 1)) : 0;
  const hasOutput = running || results.length > 0 || !!res?.error;

  return (
    <div style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
      <div className="hub-row" style={{ border: 0, padding: 0 }}>
        <div className="hub-row-main">
          <div className="hub-row-title">Compare models</div>
          <div className="hub-row-sub">Run the same prompt on every backend — throughput and time-to-first-token, side by side.</div>
        </div>
        <button className="hub-btn sm" onClick={run} disabled={running}>
          <Icon name={running ? 'refresh' : 'chart'} />{running ? 'Benchmarking…' : 'Run benchmark'}
        </button>
      </div>
      <input className="hub-input" style={{ marginTop: 10 }} value={prompt}
        onChange={(e) => setPrompt(e.target.value)} placeholder="Optional prompt (default: a short standard prompt)" />
      {msg && <div className="hub-msg err">{msg}</div>}

      {hasOutput && (
        <div className="hub-preview bench-preview" style={{ marginTop: 12 }}>
          <div className="hub-preview-head">
            <Icon name="chart" /> {res?.prompt ? `"${res.prompt.slice(0, 56)}"` : 'results'}
          </div>
          <div className="bench-body">
            {res?.error ? (
              <div className="hub-msg err" style={{ margin: 0 }}>{res.error}</div>
            ) : (
              <>
                {results.length > 0 && <BenchTable results={results} winner={res?.winner} />}
                {pending > 0 && (
                  <div className="bench-cmp">
                    {Array.from({ length: pending }).map((_, i) => (
                      <div className="bench-row pending" key={'p' + i}>
                        <div className="bench-name"><span className="bench-skel skel-name" /></div>
                        <div className="bench-metric"><span className="bench-skel skel-bar" /></div>
                        <div className="bench-metric"><span className="bench-skel skel-bar" /></div>
                      </div>
                    ))}
                  </div>
                )}
                {!running && results.length === 1 && (
                  <div className="bench-hint">
                    <Icon name="info" /> One model measured. Add another backend under
                    <b> Inference backend</b> above to compare them head-to-head.
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {!running && bench?.status === 'done' && results.length === 0 && !res?.error && (
        <EmptyState text="No models configured to benchmark — add an inference backend above, then run the comparison." />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Agent
// ─────────────────────────────────────────────────────────────────────────────
// A status-board row: a tone dot + label + value, so a panel's checks scan
// top-to-bottom instead of as a mixed pile of badges and inline text. Shared by
// the Agent runtime and Voice gate panels.
function StatRow({ label, value, tone }: { label: string; value: React.ReactNode; tone: 'ok' | 'warn' | 'err' | 'muted' }) {
  return (
    <div className="stat-row">
      <span className={`stat-row-dot tone-${tone}`} aria-hidden="true" />
      <span className="stat-row-label">{label}</span>
      <span className="stat-row-val">{value}</span>
    </div>
  );
}

function AgentPanel({ onRestart }: { onRestart: () => void }) {
  const [st, setSt] = useState<AgentStatus | null>(null);
  const [steps, setSteps] = useState<{ step: string; ok: boolean; detail: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState('');

  const load = useCallback(() => { hub.agentStatus().then(setSt).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);

  const provision = useCallback(async () => {
    setBusy(true); setSteps(null); setDetail('');
    try {
      const r = await hub.agentProvision();
      setSteps(r.steps || []); setDetail(r.detail || '');
      load();
    } catch (e) { setDetail((e as Error).message); }
    setBusy(false);
  }, [load]);

  return (
    <>
    <Panel
      title="Agent runtime"
      subtitle="NemoClaw gives Ava a sandbox, tools, egress policies, and persistent memory. Without it, chat still works (tool-less)."
      right={st ? (
        st.available ? <Badge tone="ok">active</Badge>
          : st.enabled === false ? <Badge tone="muted">disabled</Badge>
            : <Badge tone="warn">not ready</Badge>
      ) : null}
    >
      {st ? (
        <div className="stat-rows">
          <StatRow label="Runtime"
            value={`${st.runtime}${st.required ? ' · required' : ''}`}
            tone={st.available ? 'ok' : st.enabled === false ? 'muted' : 'warn'} />
          <StatRow label="CLI"
            value={st.cli || 'not installed'}
            tone={st.cli ? 'ok' : 'warn'} />
          <StatRow label="Sandbox"
            value={st.sandbox ? `${st.sandbox}${st.sandbox_exists ? '' : ' · missing'}` : 'none'}
            tone={st.sandbox_exists ? 'ok' : 'warn'} />
          <StatRow label="Tools"
            value={st.tools ? 'available' : st.enabled === false ? 'disabled' : 'unavailable'}
            tone={st.tools ? 'ok' : st.enabled === false ? 'muted' : 'warn'} />
        </div>
      ) : <EmptyState text="Loading agent status…" />}

      {st && st.enabled === false ? (
        <div className="hub-note" style={{ marginTop: 14 }}>
          The agent is <b>turned off for this instance</b>{' '}
          {st.enabled_env_override
            ? <>— forced by the <code>{st.enabled_env_override}</code> env var in this
              instance's launch command, which shadows <code>ava.yaml</code>. Remove it
              and restart to get tools, memory, and skills.</>
            : <>(<code>agent.enabled: false</code> in ava.yaml) — so chat runs tool-less
              by design, and the CLI/sandbox rows above are just what's present on the
              host. Enable it in <b>System</b> and restart to get tools, memory, and
              skills.</>}
        </div>
      ) : st && !st.cli && (
        <div className="hub-note" style={{ marginTop: 14 }}>
          The NemoClaw CLI isn't installed. Run <b>ava agent provision --install</b> in a terminal
          (it installs the CLI, then guides <b>nemoclaw onboard</b>), then click Re-check below.
        </div>
      )}

      <div className="hub-btn-row">
        <button className="hub-btn" onClick={provision} disabled={busy}>
          <Icon name="refresh" />{busy ? 'Provisioning…' : 'Provision / re-check'}
        </button>
      </div>

      {steps && (
        <div className="hub-steps">
          {steps.map((s, i) => (
            <div className="hub-step" key={i}>
              <span className={'hub-step-mark ' + (s.ok ? 'ok' : 'bad')}><Icon name={s.ok ? 'check' : 'close'} /></span>
              <span style={{ minWidth: 0 }}>
                <b>{s.step}</b>
                <div className="hub-step-detail">{s.detail}</div>
              </span>
            </div>
          ))}
          {detail && <div className="hub-msg" style={{ color: 'var(--muted)' }}>{detail}</div>}
        </div>
      )}
    </Panel>

    <div className="hub-section" />
    <BrainManager onRestart={onRestart} />

    <div className="hub-section" />
    <SkillsPanel />

    <div className="hub-section" />
    <ModelStorePanel />
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Skills — the agent's SKILL.md capabilities, auto-discovered from the filesystem
// (agent/skills + overlay). Adding a folder surfaces it here with no code change;
// each skill shows what it does, the tools it uses, and whether it's actually
// deployed into the sandbox vs newly added in the repo.
const SKILL_ICONS = new Set([
  'image', 'chart', 'code', 'grid', 'cloud', 'calendar', 'chats', 'megaphone',
  'sparkles', 'graduation', 'bot', 'db', 'gauge', 'search', 'panel',
]);

function skillDeployBadge(state: Skill['deployed']) {
  switch (state) {
    case 'deployed':
      return <Badge tone="ok">live</Badge>;
    case 'stale':
      return <Badge tone="warn">edited · re-provision</Badge>;
    case 'undeployed':
      return <Badge tone="warn">not deployed · re-provision</Badge>;
    default:
      return <Badge tone="muted">provision to load</Badge>;
  }
}

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
      <button className="skill-head" onClick={onToggle} aria-expanded={open}>
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
        {skillDeployBadge(s.deployed)}
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

function SkillsPanel() {
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
              <button
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
                        <button
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
                          <button
                            className="skill-group-rename"
                            title={`Rename “${cat}”`}
                            aria-label={`Rename category ${cat}`}
                            onClick={() => { setEditingCat(cat); setEditVal(cat); }}
                          >
                            <Icon name="pencil" />
                          </button>
                        )}
                        {mode === 'category' && cat !== 'General' && list.length === 0 && (
                          <button
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

// ─────────────────────────────────────────────────────────────────────────────
// Connectors
// The JIT permission sheet (iOS Settings→Privacy style): every action grouped
// by capability, with its tier and grant state. Reads always run; writes toggle
// between "asks first time" and an "always allow" grant; destructive stays 🔒.
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
        <span className="conn-ic" aria-hidden="true"
          style={{ color: accent, background: `color-mix(in srgb, ${accent} 15%, transparent)` }}>
          <Icon name={appIcon(ident)} />
        </span>
        <div className="conn-id">
          <div className="conn-title-row">
            <span className="conn-title">{c.label}</span>
            {c.app && <Badge tone="accent">APP</Badge>}
            {(c.mcp || c.discover || c.actions > 0) && <Badge tone="accent">MCP</Badge>}
            {c.builtin && <Badge>built-in</Badge>}
          </div>
          <div className="conn-meta">
            {c.enabled
              ? <span className="conn-stat ok" title="Ava can use this connector"><i />enabled</span>
              : <span className="conn-stat off" title="Turned off — Ava won't use it"><i />disabled</span>}
            {c.actions > 0 && <><span className="conn-sep">·</span><span>{c.actions} action{c.actions === 1 ? '' : 's'}</span></>}
            {hasAgentSurface && c.enabled && (
              <><span className="conn-sep">·</span>
              {deployed
                ? <span className="conn-stat ok" title="Tools and egress policy are up to date in the agent"><Icon name="check" />deployed</span>
                : <span className="conn-stat warn" title={`${drift || 'tools'} out of date — Deploy regenerates ${drift ? 'them' : 'the tools'} into the agent`}><Icon name="alert" />needs deploy</span>}
              </>
            )}
          </div>
        </div>
        <div className="conn-actions">
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

function ConnectorsPanel() {
  const [conns, setConns] = useState<HubConnector[] | null>(null);
  const [loadErr, setLoadErr] = useState('');
  const [badManifests, setBadManifests] = useState<ConnectorLoadError[]>([]);
  const load = useCallback(() => {
    // Connect/edit/remove may have changed the app registry — tell the shell
    // so a new tile appears in the rail without a page refresh.
    window.dispatchEvent(new Event('ava:apps-changed'));
    hub.connectors()
      .then((r) => { setConns(r.connectors.filter(isExternalApp)); setBadManifests(r.errors || []); setLoadErr(''); })
      .catch((e) => { setLoadErr((e as Error).message || 'could not load connectors'); });
  }, []);
  useEffect(() => { load(); }, [load]);
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
        <div className="conn-legend">
          <div className="conn-legend-title">What the actions do</div>
          <dl className="conn-legend-grid">
            <dt className="conn-legend-term"><Icon name="check" />Deploy</dt>
            <dd className="conn-legend-desc">
              Appears only when a connector's tools or egress policy are out of date — regenerates them into the
              agent so Ava can use it. Up-to-date connectors read <b>deployed</b>; redeploy anytime from the ⋯ menu.
            </dd>
            <dt className="conn-legend-term"><Icon name="lock" />Permissions</dt>
            <dd className="conn-legend-desc">
              What Ava may do here — reads run silently, writes ask once, destructive actions always ask.
            </dd>
            <dt className="conn-legend-term"><Icon name="code" />Preview</dt>
            <dd className="conn-legend-desc">
              The tools and egress policy generated from the manifest, without touching the agent.
            </dd>
            <dt className="conn-legend-term"><Icon name="more" />More</dt>
            <dd className="conn-legend-desc">
              Push token, appearance, manifest editor, and disable&nbsp;/&nbsp;remove.
            </dd>
          </dl>
          <div className="conn-legend-foot">
            <div>Agent host unreachable from here? Run <code>cd agent &amp;&amp; ./install.sh</code> there to deploy.</div>
            <div>Docs — connectors: <b>docs/CONNECTOR_SDK.md</b> · hardware: <b>docs/DEVICE_CONNECTORS.md</b></div>
          </div>
        </div>
      </Panel>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Voice — enrollment recorder + similarity test
// ─────────────────────────────────────────────────────────────────────────────
function useRecorder() {
  const [recording, setRecording] = useState(false);
  const mrRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);

  const start = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mr = new MediaRecorder(stream);
    chunksRef.current = [];
    mr.ondataavailable = (e) => chunksRef.current.push(e.data);
    mr.start();
    mrRef.current = mr;
    setRecording(true);
  }, []);

  const stop = useCallback((): Promise<Blob> => new Promise((resolve) => {
    const mr = mrRef.current;
    if (!mr) return resolve(new Blob());
    mr.onstop = () => {
      mr.stream.getTracks().forEach((t) => t.stop());
      resolve(new Blob(chunksRef.current, { type: mr.mimeType || 'audio/webm' }));
    };
    mr.stop();
    setRecording(false);
  }), []);

  return { recording, start, stop };
}

const ENROLL_PHRASES = [
  'Read a few sentences naturally, like you are talking to a friend.',
  'Describe what you did today, or read a paragraph from any article.',
  'Aim for 10–15 seconds per clip. Three clips give a solid voiceprint.',
];

function VoicePanel({ onRestart }: { onRestart: () => void }) {
  const [st, setSt] = useState<VoiceStatus | null>(null);
  const [clips, setClips] = useState<Blob[]>([]);
  const [result, setResult] = useState<EnrollResult | null>(null);
  const [testSim, setTestSim] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const rec = useRecorder();
  const [mode, setMode] = useState<'enroll' | 'test' | null>(null);

  const load = useCallback(() => { hub.voiceStatus().then(setSt).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);

  const toggleRecord = useCallback(async (m: 'enroll' | 'test') => {
    setMsg('');
    if (typeof MediaRecorder === 'undefined') {
      setMsg('This browser cannot record audio in-page — enroll from a file with enroll_from_file.py instead.');
      return;
    }
    if (rec.recording) {
      const blob = await rec.stop();
      setMode(null);
      if (blob.size < 1000) { setMsg('Recording too short — try again.'); return; }
      if (m === 'enroll') setClips((c) => [...c, blob]);
      else {
        setBusy(true);
        try {
          const r = await hub.voiceTest(blob);
          if (r.ok && r.similarity != null) setTestSim(r.similarity);
          else setMsg(r.error || 'test failed');
        } catch (e) { setMsg((e as Error).message); }
        setBusy(false);
      }
    } else {
      setTestSim(null);
      try { setMode(m); await rec.start(); }
      catch (e) {
        setMode(null);
        setMsg((e as Error)?.name === 'NotAllowedError'
          ? 'Microphone access denied — allow the mic for this site and retry.'
          : `Could not start recording: ${(e as Error).message}`);
      }
    }
  }, [rec]);

  const applyThreshold = useCallback(async (v: number) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.voiceThreshold(v);
      if (r.error) setMsg(r.error);
      else { onRestart(); load(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [onRestart, load]);

  const enroll = useCallback(async () => {
    setBusy(true); setMsg(''); setResult(null);
    try {
      const r = await hub.voiceEnroll(clips);
      if (r.ok) { setResult(r); setClips([]); load(); }
      else setMsg(r.error || 'enrollment failed');
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [clips, load]);

  const enableVoice = useCallback(async () => {
    setBusy(true);
    try { await hub.save({ features: { voice: true } }); load(); onRestart(); }
    catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [load, onRestart]);

  return (
    <>
      <Panel
        title="Voice & biometric gate"
        subtitle="Everything runs on your machine: local speech-to-text, local TTS, and a speaker-verification gate so Ava answers your voice only."
        right={st ? (
          !st.enabled ? <Badge tone="muted">voice off</Badge>
            : st.enrolled ? <Badge tone="ok">gate closed</Badge>
              : <Badge tone="err">gate open</Badge>
        ) : null}
      >
        {st == null ? <EmptyState text="Loading voice status…" /> : (
          <div className="stat-rows">
            <StatRow label="Voice feature" tone={st.enabled ? 'ok' : 'muted'}
              value={st.enabled ? 'on' : (
                <>off<button className="hub-btn ghost sm" onClick={enableVoice} disabled={busy}>Enable</button></>
              )} />
            <StatRow label="Voiceprint" tone={st.enrolled ? 'ok' : 'warn'}
              value={st.enrolled ? 'enrolled' : 'not enrolled — record clips below'} />
            <StatRow label="Dependencies" tone={st.deps_ok ? 'ok' : 'warn'}
              value={st.deps_ok ? 'installed' : (st.deps_error || 'missing')} />
            <StatRow label="Gate threshold" tone="muted"
              value={<>{st.threshold} <span style={{ color: 'var(--muted)' }}>cosine similarity · set from enrollment, or voice.threshold in ava.yaml</span></>} />
          </div>
        )}
        {st?.enabled && !st.enrolled && (
          <div className="hub-restart" style={{ marginTop: 14, marginBottom: 0 }}>
            <Icon name="alert" />
            <span><b>The gate is open:</b> voice is on but no voiceprint is enrolled, so
            {' '}<b>anyone</b> can talk to Ava. Enroll below to close it.</span>
          </div>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Enroll your voice" subtitle="Record a few clips of natural speech; Ava builds an averaged voiceprint (nothing is uploaded anywhere — it stays on this machine).">
        <ul className="voice-tips">
          {ENROLL_PHRASES.map((p, i) => <li key={i}>{p}</li>)}
        </ul>

        <div className="hub-btn-row">
          <button
            className={'hub-btn' + (rec.recording && mode === 'enroll' ? '' : ' ghost')}
            onClick={() => toggleRecord('enroll')}
            disabled={busy || !st?.deps_ok || (rec.recording && mode !== 'enroll')}
          >
            <Icon name="mic" />{rec.recording && mode === 'enroll' ? 'Stop recording' : `Record clip ${clips.length + 1}`}
          </button>
          {clips.length > 0 && (
            <button className="hub-btn" onClick={enroll} disabled={busy || rec.recording}>
              <Icon name="check" />{busy ? 'Building voiceprint…' : `Build voiceprint from ${clips.length} clip${clips.length === 1 ? '' : 's'}`}
            </button>
          )}
          {clips.length > 0 && !rec.recording && (
            <button className="hub-btn ghost" onClick={() => setClips([])} disabled={busy}>
              <Icon name="trash" />Discard clips
            </button>
          )}
        </div>

        {rec.recording && mode === 'enroll' && (
          <div className="hub-msg" style={{ color: 'var(--err)' }}>● Recording — speak naturally, then Stop.</div>
        )}
        {clips.length > 0 && !rec.recording && (
          <div className="hub-msg" style={{ color: 'var(--muted)' }}>
            {clips.length} clip{clips.length === 1 ? '' : 's'} ready{clips.length < 3 ? ' — 3+ recommended' : ''}.
          </div>
        )}

        {result && (
          <div className="hub-note" style={{ marginTop: 12 }}>
            <b>Voiceprint saved.</b> {result.seconds}s of audio → {result.windows} voice windows
            {result.dropped ? ` (${result.dropped} outliers dropped)` : ''}.
            Consistency {result.consistency?.mean}.{' '}
            Suggested threshold: <b>{result.suggested_threshold}</b>
            {result.suggested_threshold != null && (
              <button className="hub-btn sm" style={{ marginLeft: 10 }} disabled={busy}
                onClick={() => applyThreshold(result.suggested_threshold!)}>
                <Icon name="check" />Apply threshold
              </button>
            )}
            {result.low_consistency && <div style={{ color: 'var(--warn)', marginTop: 4 }}>Consistency is a bit low — re-record in a quieter room for a stronger gate.</div>}
          </div>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Test the gate" subtitle="Record a short clip and see how it scores against the enrolled voiceprint.">
        <div className="hub-btn-row" style={{ marginTop: 0 }}>
          <button
            className={'hub-btn' + (rec.recording && mode === 'test' ? '' : ' ghost')}
            onClick={() => toggleRecord('test')}
            disabled={busy || !st?.deps_ok || !st?.enrolled || (rec.recording && mode !== 'test')}
          >
            <Icon name="mic" />{rec.recording && mode === 'test' ? 'Stop & score' : 'Record test clip'}
          </button>
        </div>
        {testSim != null && st && (
          <div className="hub-msg" style={{ fontSize: 'var(--fs-md)' }}>
            Similarity <b style={{ color: testSim >= st.threshold ? 'var(--ok)' : 'var(--err)' }}>{testSim}</b>
            {' '}vs threshold {st.threshold} — {testSim >= st.threshold
              ? <span style={{ color: 'var(--ok)' }}>Ava would answer this voice.</span>
              : <span style={{ color: 'var(--err)' }}>Ava would ignore this voice.</span>}
          </div>
        )}
        {!st?.enrolled && <div className="hub-msg" style={{ color: 'var(--muted)' }}>Enroll a voiceprint first.</div>}
      </Panel>
      {msg && <div className="hub-msg err">{msg}</div>}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Budgets — spend/energy caps + live meter
// ─────────────────────────────────────────────────────────────────────────────
function meterTone(pct: number): 'ok' | 'warn' | 'err' {
  return pct >= 100 ? 'err' : pct >= 80 ? 'warn' : 'ok';
}

const fmtMoney = (n: number) => `$${n.toFixed(2)}`;
const fmtCap = (n: number, unit: '$' | 'kWh') => (unit === '$' ? `$${n}` : `${n} kWh`);

// A budget meter row: usage against a cap with a live track, remaining, and %.
// Unlike the old bar it ALWAYS renders — so you see today's spend/energy even
// before any cap is set (the track just reads "no cap set"), and the energy row
// converts kWh to money at your rate so the two costs read in the same terms.
function BudgetMeter({ label, used, cap, unit, rate }: {
  label: string; used: number; cap: number | null; unit: '$' | 'kWh'; rate?: number;
}) {
  const has = cap != null && cap > 0;
  const ratio = has ? used / (cap as number) : 0;
  const pct = has ? Math.min(100, Math.round(ratio * 100)) : 0;
  const tone = has ? meterTone(ratio * 100) : 'muted';
  const fmt = (n: number) => (unit === '$' ? fmtMoney(n) : `${n.toFixed(2)} kWh`);
  const remaining = has ? Math.max(0, (cap as number) - used) : 0;
  const energyCost = unit === 'kWh' && rate ? used * rate : null;
  return (
    <div className="bud-meter">
      <div className="bud-meter-head">
        <span className="bud-meter-label">{label}</span>
        <span className="bud-meter-val">
          <b className={`bud-tone-${tone}`}>{fmt(used)}</b>
          <span className="bud-cap">{has ? ` / ${fmtCap(cap as number, unit)}` : ' · no cap set'}</span>
        </span>
      </div>
      <div className="bud-track" aria-hidden="true">
        <div className={`bud-fill bud-tone-${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="bud-meter-foot">
        <span>{energyCost != null ? `≈ ${fmtMoney(energyCost)} today at your rate` : ' '}</span>
        <span>{has ? `${fmt(remaining)} left · ${pct}%` : 'set a cap below to meter it'}</span>
      </div>
    </div>
  );
}

function BudgetsPanel() {
  const [c, setC] = useState<CostSettings | null>(null);
  const [rate, setRate] = useState('');
  const [du, setDu] = useState('');
  const [mu, setMu] = useState('');
  const [dk, setDk] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const load = useCallback(() => {
    hub.cost().then((r) => {
      setC(r);
      setRate(String(r.electricity_rate_per_kwh ?? ''));
      setDu(r.budgets.daily_usd != null ? String(r.budgets.daily_usd) : '');
      setMu(r.budgets.monthly_usd != null ? String(r.budgets.monthly_usd) : '');
      setDk(r.budgets.daily_kwh != null ? String(r.budgets.daily_kwh) : '');
    }).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = useCallback(async () => {
    setBusy(true); setMsg('');
    const num = (s: string) => (s.trim() === '' ? null : Number(s));
    try {
      const r = await hub.saveCost({
        electricity_rate_per_kwh: rate.trim() === '' ? 0 : Number(rate),
        budgets: { daily_usd: num(du), monthly_usd: num(mu), daily_kwh: num(dk) },
      } as Partial<CostSettings>);
      if (r.error) setMsg(r.error);
      else { setMsg('Saved.'); load(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [rate, du, mu, dk, load]);

  const anyCap = c && (c.budgets.daily_usd != null || c.budgets.monthly_usd != null || c.budgets.daily_kwh != null);

  return (
    <>
      <Panel title="Today's spend & energy" subtitle="Live usage against your caps. Cloud API calls cost real money; local generation only costs electricity.">
        {c ? (
          <>
            <BudgetMeter label="Cloud spend today" used={c.daily_spend_usd} cap={c.budgets.daily_usd} unit="$" />
            <BudgetMeter label={`GPU energy today${c.power_measured ? '' : ' (est.)'}`} used={c.daily_energy_kwh} cap={c.budgets.daily_kwh} unit="kWh" rate={c.electricity_rate_per_kwh} />
            {c.budgets.monthly_usd != null && (
              <div className="bud-monthly">
                <Icon name="calendar" />
                <span>Monthly cloud cap <b>{fmtMoney(c.budgets.monthly_usd)}</b> — alerts fire at 80% and 100% on the <b>Operations</b> page.</span>
              </div>
            )}
            {!anyCap && (
              <div className="bud-monthly">
                <Icon name="info" />
                <span>No caps set yet — usage is shown above; add a cap below to turn on the meter and alerts.</span>
              </div>
            )}
          </>
        ) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Set budgets" subtitle="Blank = that budget is off. Nothing is ever blocked — caps only drive alerts on the Operations page.">
        <div className="hub-field" style={{ maxWidth: 340 }}>
          <label>Electricity rate ({c?.currency || '$'} per kWh)</label>
          <input className="hub-input" value={rate} onChange={(e) => setRate(e.target.value)} placeholder="0.15" inputMode="decimal" />
          <div className="bud-field-hint">Turns GPU energy into a dollar figure. Leave at 0 to show energy in kWh only.</div>
        </div>
        <div className="bud-caps-head">Caps <span>— leave blank to disable</span></div>
        <div className="hub-fieldrow">
          <div className="hub-field"><label>Daily cloud spend</label>
            <input className="hub-input" value={du} onChange={(e) => setDu(e.target.value)} placeholder="none" inputMode="decimal" /></div>
          <div className="hub-field"><label>Monthly cloud spend</label>
            <input className="hub-input" value={mu} onChange={(e) => setMu(e.target.value)} placeholder="none" inputMode="decimal" /></div>
          <div className="hub-field"><label>Daily energy (kWh)</label>
            <input className="hub-input" value={dk} onChange={(e) => setDk(e.target.value)} placeholder="none" inputMode="decimal" /></div>
        </div>
        <div className="hub-btn-row">
          <button className="hub-btn" onClick={save} disabled={busy}><Icon name="check" />{busy ? 'Saving…' : 'Save budgets'}</button>
        </div>
        {msg && <div className={'hub-msg' + (msg === 'Saved.' ? ' ok' : ' err')}>{msg}</div>}

        <div className="conn-legend">
          <div className="conn-legend-title">How budgets work</div>
          <dl className="conn-legend-grid">
            <dt className="conn-legend-term"><Icon name="cloud" />Cloud $</dt>
            <dd className="conn-legend-desc">Real API spend — only calls to a cloud model cost money. Everything Ava runs locally is free.</dd>
            <dt className="conn-legend-term"><Icon name="gauge" />Local energy</dt>
            <dd className="conn-legend-desc">GPU electricity at your rate{c && !c.power_measured ? ', estimated from GPU load until a power meter is present' : ''}.</dd>
            <dt className="conn-legend-term"><Icon name="alert" />Alerts, not blocks</dt>
            <dd className="conn-legend-desc">Nothing is ever stopped. Hitting a cap raises an alert on the <b>Operations</b> page at 80% and 100%.</dd>
            <dt className="conn-legend-term"><Icon name="activity" />Idle-burn watch</dt>
            <dd className="conn-legend-desc">Always on — flags more than ~5k tokens generated in 10 minutes while you're away ("what did it spend while I slept").</dd>
          </dl>
        </div>
      </Panel>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// History — the flight recorder (durable audit ledger)
// ─────────────────────────────────────────────────────────────────────────────
// Every audit kind the backend records → a typed identity (glyph + tone) and a
// human label, so the ledger reads like the connectors/memory lists instead of
// raw event names. Tone: accent = the agent changed something, warn/err =
// permission or destructive, ok = a normal turn, muted = passive/system.
// Unknown kinds fall back to a humanised label so nothing renders bare.
const EVENT_META: Record<string, { icon: string; label: string; tone: string }> = {
  turn: { icon: 'chats', label: 'Chat turn', tone: 'ok' },
  code_change: { icon: 'code', label: 'Self-edit', tone: 'accent' },
  egress: { icon: 'code', label: 'Tool call', tone: 'info' },
  memory_recall: { icon: 'db', label: 'Memory recall', tone: 'muted' },
  memory_distill: { icon: 'sparkles', label: 'Memory distilled', tone: 'accent' },
  memory_edit: { icon: 'pencil', label: 'Memory edit', tone: 'warn' },
  grant: { icon: 'lock', label: 'Permission granted', tone: 'warn' },
  revoke: { icon: 'lock', label: 'Permission revoked', tone: 'err' },
  approval: { icon: 'check', label: 'Approval', tone: 'warn' },
  route: { icon: 'activity', label: 'Intent routed', tone: 'muted' },
  job: { icon: 'image', label: 'Media job', tone: 'ok' },
  data_export: { icon: 'file', label: 'Data export', tone: 'muted' },
  data_maintenance: { icon: 'db', label: 'Data maintenance', tone: 'muted' },
};
const humanize = (s: string) => s.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
function eventMeta(kind: string) {
  return EVENT_META[kind] || { icon: 'info', label: humanize(kind), tone: 'muted' };
}
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

function HistoryPanel() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [cat, setCat] = useState('');
  const [err, setErr] = useState('');
  const load = useCallback(() => {
    setErr('');
    hub.audit(300).then((r) => setEvents(r.events)).catch((e) => setErr((e as Error).message));
  }, []);
  useEffect(() => { load(); }, [load]);

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
                  <span className={`hist-ic tone-${m.tone}`} aria-hidden="true"><Icon name={m.icon} /></span>
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
      <div className="conn-legend">
        <div className="conn-legend-title">What's in the ledger</div>
        <dl className="conn-legend-grid">
          <dt className="conn-legend-term"><Icon name="chats" />Chats</dt>
          <dd className="conn-legend-desc">Every message Ava answered — the model, tools used, and how long it took.</dd>
          <dt className="conn-legend-term"><Icon name="code" />Self-edits</dt>
          <dd className="conn-legend-desc">Changes the agent made to its own code, the commit, and who approved it.</dd>
          <dt className="conn-legend-term"><Icon name="db" />Memory</dt>
          <dd className="conn-legend-desc">Recalls folded into a reply, plus facts distilled from chats or edited by you.</dd>
          <dt className="conn-legend-term"><Icon name="lock" />Permissions</dt>
          <dd className="conn-legend-desc">Connector grants, revokes, and one-off approvals.</dd>
          <dt className="conn-legend-term"><Icon name="activity" />System</dt>
          <dd className="conn-legend-desc">Intent routing, media jobs, and data export / maintenance.</dd>
        </dl>
        <div className="conn-legend-foot">
          <div>Append-only — nothing here can be altered after the fact. The full audit with export lives on the <b>Data</b> page.</div>
        </div>
      </div>
    </Panel>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// System
// ─────────────────────────────────────────────────────────────────────────────
// Human label for a retention window in days (0 == forever).
const RETENTION_LABELS: Record<number, string> = {
  0: 'Forever', 30: '1 month', 90: '3 months', 183: '6 months', 365: '1 year', 730: '2 years',
};
function retentionLabel(days: number): string {
  return RETENTION_LABELS[days] || (days > 0 ? `${days} days` : 'Forever');
}

// Optional-feature capability key → a typed glyph, so each feature row carries
// an identity like the connector/memory rows. Unknown keys fall back to sliders.
const FEATURE_ICONS: Record<string, string> = {
  image: 'image', web_search: 'search', voice: 'mic', memory: 'db', code: 'code',
};
const featureIcon = (key: string) => FEATURE_ICONS[key] || 'sliders';

// Self-editing modes, safest → most permissive, each with a glyph that reads its
// posture at a glance (locked / selective / hands-off).
const APPROVALS: { id: string; title: string; sub: string; icon: string }[] = [
  { id: 'all', title: 'All changes need approval', icon: 'lock', sub: 'Safest. Every edit Ava makes to its own code waits for you.' },
  { id: 'policy', title: 'Only sensitive paths', icon: 'sliders', sub: 'Auth/config/deploy edits are gated; routine edits auto-commit to git.' },
  { id: 'none', title: 'Auto-apply', icon: 'bot', sub: 'Trusted box — all non-secret edits commit automatically.' },
];

function SystemPanel({ onRestart }: { onRestart: () => void }) {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [msgOk, setMsgOk] = useState(false);
  const note = (text: string, ok = false) => { setMsg(text); setMsgOk(ok); };

  const load = useCallback(() => { hub.system().then(setSys).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);

  const setApproval = useCallback(async (mode: string) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.setApproval(mode);
      if (r.error) note(r.error);
      else {
        setSys((s) => (s ? { ...s, code_approval: mode } : s));
        // Approval now applies live (no restart); only nudge a restart if the
        // backend still asks for one.
        if (r.restart_required) onRestart();
        else note('Saved — in effect now.', true);
      }
    } catch (e) { note((e as Error).message); }
    setBusy(false);
  }, [onRestart]);

  const saveFeatures = useCallback(async (patch: Record<string, boolean>) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.save({ features: patch });
      if (r.error) note(r.error);
      else { load(); onRestart(); }
    } catch (e) { note((e as Error).message); }
    setBusy(false);
  }, [load, onRestart]);

  const setRetention = useCallback(async (days: number) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.setRetention(days);
      if (r.error) note(r.error);
      else { setSys((s) => (s ? { ...s, retention_days: days } : s)); onRestart(); }
    } catch (e) { note((e as Error).message); }
    setBusy(false);
  }, [onRestart]);

  const overrides = Object.entries(sys?.env_overrides || {});

  return (
    <>
      <Panel title="About" subtitle="Your instance">
        {sys ? (
          <dl className="hub-kv">
            <dt>Name</dt><dd>{sys.brand}</dd>
            <dt>Version</dt><dd>{sys.version}</dd>
            <dt>Runtime</dt><dd>{sys.docker ? 'Docker container' : 'Native process'}</dd>
          </dl>
        ) : <EmptyState text="Loading…" />}
        {overrides.length > 0 && (
          <div className="hub-note" style={{ marginTop: 14 }}>
            <b>Environment overrides active:</b> {overrides.map(([k, v]) => (
              <span key={k} style={{ marginRight: 8 }}><code>{v}</code> ({k.replace(/_/g, ' ')})</span>
            ))}
            — these env vars shadow ava.yaml, so edits to the matching settings
            below apply live but revert to the env value on restart. Unset the
            variable in your launch command/unit to make yaml wins stick.
          </div>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Self-editing governance" subtitle="How Ava's code-change agent applies edits to its own repo (secrets, models/ and .git are always denied).">
        <div className="sys-gov">
          {APPROVALS.map((a) => {
            const on = sys?.code_approval === a.id;
            return (
              <button key={a.id} className={'sys-gov-opt' + (on ? ' sel' : '')}
                disabled={busy} aria-pressed={on} onClick={() => setApproval(a.id)}>
                <span className="sys-gov-ic"><Icon name={a.icon} /></span>
                <span className="sys-gov-txt"><b>{a.title}</b><small>{a.sub}</small></span>
                {on ? <span className="sys-gov-check"><Icon name="check" />Current</span> : <span className="sys-gov-pick">Use</span>}
              </button>
            );
          })}
        </div>
      </Panel>

      <div className="hub-section" />
      <Panel title="Data retention" subtitle="How long Ava keeps performance metrics and hardware history. Older data is pruned automatically; the dashboard's time-range filters can only reach back as far as this.">
        {sys ? (
          <>
            <div className="hub-field" style={{ maxWidth: 340 }}>
              <label>Keep data for</label>
              <select className="hub-select" value={sys.retention_days} disabled={busy}
                onChange={(e) => setRetention(Number(e.target.value))}>
                {(sys.retention_choices || [30, 90, 183, 365, 730, 0]).map((d) => (
                  <option key={d} value={d}>{retentionLabel(d)}{d === 183 ? ' (default)' : ''}</option>
                ))}
              </select>
            </div>
            <div className="hub-note" style={{ marginTop: 12 }}>
              Currently keeping <b>{retentionLabel(sys.retention_days)}</b> of history.
              Changing this takes effect after a restart and prunes anything older on the next cleanup.
            </div>
          </>
        ) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Optional features" subtitle="All off by default so a fresh install stays minimal — turn on only what you need.">
        {sys ? (sys.features || []).map((f) => (
          // Rendered straight from the backend capability registry
          // (ava_bridge/features.py) — a newly registered capability gets its
          // row here with no UI change. Per-key extras (like the voice
          // enrollment badge) hang off the key below.
          <label className={'sys-feat' + (f.enabled ? ' on' : '')} key={f.key}>
            <span className="sys-feat-ic" aria-hidden="true"><Icon name={featureIcon(f.key)} /></span>
            <span className="sys-feat-main">
              <span className="sys-feat-title">{f.label}</span>
              <span className="sys-feat-sub">
                {f.sub}
                {f.key === 'voice' && f.enabled && (
                  <>{' '}{sys.voiceprint
                    ? <Badge tone="ok">voiceprint enrolled</Badge>
                    : <Badge tone="warn">no voiceprint — enroll on the Voice tab</Badge>}</>
                )}
              </span>
            </span>
            <input type="checkbox" className="sys-feat-check" checked={f.enabled} disabled={busy}
              onChange={(e) => saveFeatures({ [f.key]: e.target.checked })} />
          </label>
        )) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Learning" subtitle="Periodic local-first self-analysis that parks improvement proposals for your approval.">
        {sys && (
          <dl className="hub-kv">
            <dt>Status</dt><dd>{sys.learning_enabled ? <Badge tone="ok">on · every {sys.learning_interval_h}h</Badge> : <Badge tone="muted">off</Badge>}</dd>
            <dt>Proposals</dt><dd><span style={{ color: 'var(--muted)' }}>Review &amp; run cycles on the Operations → Control Center page.</span></dd>
          </dl>
        )}
      </Panel>
      {msg && <div className={'hub-msg ' + (msgOk ? 'ok' : 'err')}>{msg}</div>}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────
export function HubView() {
  // Tab lives in the URL hash so it survives a refresh (#hub/<tab>).
  const [tab, setTabState] = useState<TabId>(() => tabFromHash());
  const setTab = useCallback((t: TabId) => { setTabState(t); writeTabHash(t); }, []);
  // Back/forward and manual hash edits move the tab too.
  useEffect(() => {
    const onHash = () => setTabState(tabFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const [restart, setRestart] = useState(false);
  const [brand, setBrand] = useState('Ava');
  useEffect(() => { api.brand().then((b) => b?.name && setBrand(b.name)).catch(() => {}); }, []);
  const notifyRestart = useCallback(() => setRestart(true), []);

  return (
    <div className="hub view-scroll">
      <div className="hub-inner">
        <div className="hub-head" style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
          <div>
            <h2>Set up {brand}</h2>
            <p>Configure your hardware, agent, apps, and system — all from here, written to your config, nothing to source.</p>
          </div>
          <form method="post" action="/logout" style={{ flexShrink: 0 }}>
            <button type="submit" className="hub-btn ghost sm">
              <Icon name="lock" />Sign out
            </button>
          </form>
        </div>

        <ApprovalsBanner />
        <RestartBanner show={restart} />

        <div className="hub-tabs">
          {TABS.map((t) => (
            <button key={t.id} className={'hub-tab' + (tab === t.id ? ' active' : '')} onClick={() => setTab(t.id)}>
              <Icon name={t.icon} />{t.label}
            </button>
          ))}
        </div>

        {tab === 'overview' && <Overview onGo={setTab} />}
        {tab === 'hardware' && <HardwarePanel />}
        {tab === 'agent' && <AgentPanel onRestart={notifyRestart} />}
        {tab === 'connectors' && <ConnectorsPanel />}
        {tab === 'voice' && <VoicePanel onRestart={notifyRestart} />}
        {tab === 'memory' && <MemoryPanel />}
        {tab === 'budgets' && <BudgetsPanel />}
        {tab === 'history' && <HistoryPanel />}
        {tab === 'system' && <SystemPanel onRestart={notifyRestart} />}
      </div>
    </div>
  );
}
