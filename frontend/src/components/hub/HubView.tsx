import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../lib/icons';
import { EmptyState, Panel } from '../dashboard/primitives';
import { api } from '../../lib/api';
import { hub } from './hubApi';
import type {
  AgentStatus, AuditEvent, Backend, BackendList, BackendProbe, BackendTestResult, BenchResult,
  BenchStatus, ConnectorLoadError, CostSettings, DeviceEvent, EnrollResult, GenerateResult,
  GrantAction, HardwareInfo, HubConnector, IngestToken, MemoryCounts, MemoryItem, ModelStore,
  NewConnectorBody, PendingApproval, ProbeResult, PullStatus, SystemInfo, VoiceStatus,
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

function Badge({ tone, children }: { tone?: 'ok' | 'warn' | 'err' | 'accent'; children: React.ReactNode }) {
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
    <button className="hub-opt" onClick={() => onGo(t)} style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
      <span style={{ color: 'var(--accent)', display: 'inline-flex' }}><Icon name={icon} /></span>
      <span style={{ minWidth: 0 }}>
        <b>{title}</b>
        <div style={{ marginTop: 3 }}>{value}</div>
        <small>{sub}</small>
      </span>
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
      <div className="db-grid db-grid-2">
        {card('hardware', 'chart', 'Hardware',
          hw ? <Badge tone="accent">{hw.tier} tier</Badge> : <Badge>detecting…</Badge>,
          hw?.gpu ? hw.gpu : 'GPU · memory · model tier')}
        {card('agent', 'bot', 'Agent',
          agent?.available ? <Badge tone="ok">{agent.name} ready</Badge> : <Badge tone="warn">not provisioned</Badge>,
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
        <dl className="hub-kv">
          <dt>Compute</dt><dd>{hw.gpu || 'No local GPU detected'}</dd>
          <dt>Usable memory</dt><dd>{hw.fit_gb != null ? `${hw.fit_gb} GB (${hw.source || 'detected'})` : '—'}</dd>
          <dt>Recommended tier</dt><dd><Badge tone="accent">{hw.tier}</Badge> &nbsp;<span style={{ color: 'var(--muted)' }}>{hw.hint}</span></dd>
        </dl>
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

  const preset = ENGINE_PRESETS.find((p) => p.value === engine) ?? ENGINE_PRESETS[0];
  const isCloud = !!preset.cloud;

  const load = useCallback(() => { hub.backendList().then(setList).catch(() => {}); }, []);
  useEffect(() => {
    load();
    hub.backends().then(setBe).catch(() => {});
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

  return (
    <Panel
      title="Ava's brain"
      subtitle="Link any model — a local engine (Ollama, MLX, LM Studio, llama.cpp, vLLM) or any OpenAI-compatible cloud provider — and pick which one Ava thinks with."
      right={<button className="hub-btn sm" onClick={openAdd}><Icon name="sparkles" />Add a model</button>}
    >
      {list == null ? <EmptyState text="Loading models…" />
        : backends.length === 0 && !showForm
          ? <EmptyState text="No model linked yet — click “Add a model” to connect Ava's brain." />
          : (
            <div className="hub-model-list">
              {backends.map((b) => (
                <div key={b.id} className={'hub-opt' + (b.is_brain ? ' sel' : '')}
                     style={{ display: 'flex', alignItems: 'center', gap: 12, cursor: 'default' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <b style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {b.label}
                      {b.is_brain && <Badge tone="accent">brain</Badge>}
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
                ? `✓ Connected${test.ms != null ? ` (${test.ms} ms)` : ''}${test.reply ? ` — replied “${test.reply}”` : ''}`
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
                <div className="bench-metric-head"><b>{r.ttft_ms}</b> ms TTFT</div>
                <BenchBar pct={clampPct((1 - (r.ttft_ms || 0) / maxTtft) * 100)} mark={ttftMark}
                  kind="ttft" markTitle="snappy ≤ 500 ms" />
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
      right={st ? (st.available ? <Badge tone="ok">active</Badge> : <Badge tone="warn">not ready</Badge>) : null}
    >
      {st ? (
        <dl className="hub-kv">
          <dt>Configured</dt><dd>{st.runtime}{st.required ? ' · required' : ''}</dd>
          <dt>CLI</dt><dd>{st.cli || <span style={{ color: 'var(--warn)' }}>not installed</span>}</dd>
          <dt>Sandbox</dt><dd>{st.sandbox} {st.sandbox_exists ? <Badge tone="ok">exists</Badge> : <Badge tone="warn">missing</Badge>}</dd>
          <dt>Tools</dt><dd>{st.tools ? <Badge tone="ok">available</Badge> : <Badge tone="warn">unavailable</Badge>}</dd>
        </dl>
      ) : <EmptyState text="Loading agent status…" />}

      {st && !st.cli && (
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
    <ModelStorePanel />
    </>
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

  const hasAgentSurface = c.actions > 0 || ((c.mcp || c.discover) && c.renders_policy);

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

  return (
    <div style={{ borderBottom: '1px solid var(--line)', padding: '12px 0', opacity: c.enabled ? 1 : 0.6 }}>
      <div className="hub-row" style={{ border: 0, padding: 0 }}>
        <div className="hub-row-main">
          <div className="hub-row-title">
            {c.label} <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 'var(--fs-xs)' }}>· {c.kind}</span>
          </div>
          <div className="hub-row-sub" style={{ display: 'flex', gap: 6, marginTop: 5, flexWrap: 'wrap' }}>
            {c.enabled ? <Badge tone="ok">enabled</Badge> : <Badge tone="warn">disabled</Badge>}
            {c.builtin && <Badge>built-in</Badge>}
            {c.mcp && <Badge tone="accent">MCP server</Badge>}
            {c.actions > 0 && <Badge tone="accent">{c.actions} action{c.actions === 1 ? '' : 's'}</Badge>}
            {c.actions > 0 && (c.has_tools ? <Badge tone="ok">tools ok</Badge> : <Badge tone="warn">tools stale</Badge>)}
            {c.renders_policy && (c.has_policy ? <Badge tone="ok">policy ok</Badge> : <Badge tone="warn">policy stale</Badge>)}
          </div>
        </div>
        <div className="hub-row-actions">
          <button className="hub-btn ghost sm" onClick={showToken} disabled={busy} title="Show the push token a device presents to send readings/events">
            <Icon name="lock" />Push token
          </button>
          {hasAgentSurface && c.enabled && (
            <button className="hub-btn ghost sm" onClick={() => setShowPerms((v) => !v)}
              title="What Ava may do in this app — reads run silently, writes ask once, destructive always asks">
              <Icon name="lock" />Permissions
            </button>
          )}
          {hasAgentSurface && c.enabled && (
            <button className="hub-btn ghost sm" onClick={preview} disabled={busy}>
              <Icon name="code" />Preview
            </button>
          )}
          {hasAgentSurface && c.enabled && (
            <button className="hub-btn sm" onClick={deploy} disabled={busy}>
              <Icon name="check" />{busy ? 'Deploying…' : 'Deploy'}
            </button>
          )}
          {!c.builtin && (
            <button className="hub-btn ghost sm" onClick={toggleEnabled} disabled={busy}
              title={c.enabled ? 'Stop Ava from using this connector' : 'Turn this connector back on'}>
              {c.enabled ? 'Disable' : 'Enable'}
            </button>
          )}
          {!c.builtin && (
            <button className="hub-btn ghost sm" onClick={openEdit} disabled={busy} title="Edit this connector's manifest">
              <Icon name="pencil" />Edit
            </button>
          )}
          {!c.builtin && (
            <button className="hub-btn ghost sm" onClick={remove} disabled={busy} aria-label="Remove connector" title="Remove connector">
              <Icon name="trash" />
            </button>
          )}
        </div>
      </div>
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
        <div className="hub-note" style={{ marginTop: 16, display: 'grid', gap: 6 }}>
          <div><b>Push token</b> — reveals the secret a device presents to send its readings and events.</div>
          <div><b>Permissions</b> — what Ava may do in this app: reads run silently, writes ask once, destructive actions always ask.</div>
          <div><b>Preview</b> — shows the tools and egress policy generated from the manifest, without touching the agent.</div>
          <div>
            <b>Deploy</b> — loads the connector's tools into the agent so Ava can read or command it
            (if the sandbox isn't reachable from here, run <b>cd agent &amp;&amp; ./install.sh</b> on the agent host).
          </div>
          <div>Full schema: <b>docs/CONNECTOR_SDK.md</b>; hardware: <b>docs/DEVICE_CONNECTORS.md</b>.</div>
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
        right={st ? (st.enrolled ? <Badge tone="ok">voiceprint enrolled</Badge> : <Badge tone="warn">not enrolled</Badge>) : null}
      >
        {st == null ? <EmptyState text="Loading voice status…" /> : (
          <dl className="hub-kv">
            <dt>Voice feature</dt>
            <dd>{st.enabled ? <Badge tone="ok">on</Badge> : (
              <span>
                <Badge tone="warn">off</Badge>{' '}
                <button className="hub-btn ghost sm" style={{ marginLeft: 8 }} onClick={enableVoice} disabled={busy}>Enable</button>
              </span>
            )}</dd>
            <dt>Dependencies</dt>
            <dd>{st.deps_ok ? <Badge tone="ok">installed</Badge>
              : <span style={{ color: 'var(--warn)' }}>{st.deps_error}</span>}</dd>
            <dt>Gate threshold</dt>
            <dd>{st.threshold} <span style={{ color: 'var(--muted)' }}>(cosine similarity — applied from enrollment below, or voice.threshold in ava.yaml)</span></dd>
          </dl>
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
        <div className="hub-note">
          {ENROLL_PHRASES.map((p, i) => <div key={i}>· {p}</div>)}
        </div>

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

function BudgetBar({ label, used, cap, unit }: { label: string; used: number; cap: number | null; unit: string }) {
  if (!cap) return null;
  const pct = Math.min(100, Math.round((used / cap) * 100));
  const tone = meterTone((used / cap) * 100);
  const col = tone === 'err' ? 'var(--err)' : tone === 'warn' ? 'var(--warn)' : 'var(--ok)';
  return (
    <div style={{ margin: '10px 0' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--fs-sm)', marginBottom: 4 }}>
        <span style={{ color: 'var(--muted)' }}>{label}</span>
        <span><b style={{ color: col }}>{unit === '$' ? `$${used.toFixed(2)}` : `${used.toFixed(2)} ${unit}`}</b> <span style={{ color: 'var(--muted)' }}>/ {unit === '$' ? `$${cap}` : `${cap} ${unit}`}</span></span>
      </div>
      <div style={{ height: 8, borderRadius: 999, background: 'var(--panel2)', overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: col, transition: 'width .3s' }} />
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

  return (
    <>
      <Panel title="Spend & energy meter" subtitle="Today's usage against your caps. Cloud API $ is real; local generation is free (energy only).">
        {c ? (
          (c.budgets.daily_usd || c.budgets.monthly_usd || c.budgets.daily_kwh) ? (
            <>
              <BudgetBar label="Cloud spend today" used={c.daily_spend_usd} cap={c.budgets.daily_usd} unit="$" />
              <BudgetBar label={`GPU energy today${c.power_measured ? '' : ' (est.)'}`} used={c.daily_energy_kwh} cap={c.budgets.daily_kwh} unit="kWh" />
              {c.budgets.monthly_usd != null && (
                <div className="hub-note" style={{ marginTop: 10 }}>
                  Monthly cap ${c.budgets.monthly_usd} — alerts fire at 80% and 100% (Operations page).
                </div>
              )}
            </>
          ) : <EmptyState text="No budgets set yet — add one below to turn on the meter and alerts." />
        ) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Set budgets" subtitle="Leave a field blank to disable that budget. Alerts appear on the Operations page; nothing is ever blocked automatically.">
        <div className="hub-field" style={{ maxWidth: 320 }}>
          <label>Electricity rate ({c?.currency || '$'} / kWh)</label>
          <input className="hub-input" value={rate} onChange={(e) => setRate(e.target.value)} placeholder="0.15" inputMode="decimal" />
        </div>
        <div className="hub-fieldrow">
          <div className="hub-field"><label>Daily cloud $ cap</label>
            <input className="hub-input" value={du} onChange={(e) => setDu(e.target.value)} placeholder="none" inputMode="decimal" /></div>
          <div className="hub-field"><label>Monthly cloud $ cap</label>
            <input className="hub-input" value={mu} onChange={(e) => setMu(e.target.value)} placeholder="none" inputMode="decimal" /></div>
          <div className="hub-field"><label>Daily energy cap (kWh)</label>
            <input className="hub-input" value={dk} onChange={(e) => setDk(e.target.value)} placeholder="none" inputMode="decimal" /></div>
        </div>
        <div className="hub-btn-row">
          <button className="hub-btn" onClick={save} disabled={busy}><Icon name="check" />{busy ? 'Saving…' : 'Save budgets'}</button>
        </div>
        {msg && <div className={'hub-msg' + (msg === 'Saved.' ? ' ok' : ' err')}>{msg}</div>}
        <div className="hub-note" style={{ marginTop: 14 }}>
          <b>Idle-burn watch</b> is always on: if the agent generates more than ~5k tokens
          in 10 minutes while you're away, Operations raises an alert — the "what did it
          spend while I slept" signal.
        </div>
      </Panel>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// History — the flight recorder (durable audit ledger)
// ─────────────────────────────────────────────────────────────────────────────
function evtColor(kind: string): string {
  return kind === 'code_change' ? 'var(--accent)' : kind === 'egress' ? 'var(--info)' : 'var(--ok)';
}
function evtSummary(e: AuditEvent): string {
  if (e.kind === 'turn') return `Chat turn · ${e.status}${e.tools?.length ? ' · tools: ' + e.tools.join(', ') : ' · no tools'}${e.duration_s ? ` · ${e.duration_s}s` : ''}`;
  if (e.kind === 'code_change') return `Self-edit · ${e.outcome}${e.commit ? ' · ' + e.commit : ''}${e.paths?.length ? ' · ' + e.paths.length + ' file(s)' : ''}${e.approved_by ? ' · approved by ' + e.approved_by : ''}`;
  if (e.kind === 'egress') return `Tool call · ${e.connector}/${e.tool} → ${e.status}`;
  if (e.kind === 'memory_recall') return `Memory recall · ${e.count ?? '?'} item(s) folded into a turn`;
  if (e.kind === 'memory_distill') return `Memory distilled · ${e.added ?? '?'} new fact(s) from ${e.messages ?? '?'} messages`;
  if (e.kind === 'memory_edit') return `Memory ${e.action} · item #${e.id}`;
  return e.kind;
}

// ─────────────────────────────────────────────────────────────────────────────
// Memory — everything Ava remembers long-term, readable and correctable.
// Facts come from the learning cycle's distiller or manual entry; doc chunks
// from uploads. Recalls that influenced a turn are in History (memory_recall).
// ─────────────────────────────────────────────────────────────────────────────
function memorySourceLabel(m: MemoryItem): string {
  if (m.source === 'distilled') return 'learned from chats';
  if (m.source === 'manual') return 'added by you';
  if (m.source.startsWith('upload:')) return m.source.slice(7);
  return m.source || m.kind;
}

function MemoryPanel() {
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

function HistoryPanel() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [kind, setKind] = useState('');
  const [err, setErr] = useState('');
  const load = useCallback(() => {
    setErr('');
    hub.audit(300, kind).then((r) => setEvents(r.events)).catch((e) => setErr((e as Error).message));
  }, [kind]);
  useEffect(() => { load(); }, [load]);

  const FILTERS: { id: string; label: string }[] = [
    { id: '', label: 'All' }, { id: 'turn', label: 'Chat turns' },
    { id: 'code_change', label: 'Self-edits' }, { id: 'egress', label: 'Tool calls' },
    { id: 'memory_recall', label: 'Memory' },
  ];

  return (
    <Panel
      title="Flight recorder"
      subtitle="A durable, append-only record of everything the agent did — turns, self-edits, and tool calls. Survives restarts (logs/audit.jsonl)."
      right={<button className="hub-btn ghost sm" onClick={load}><Icon name="refresh" />Refresh</button>}
    >
      <div className="hub-tabs" style={{ marginBottom: 14, borderBottom: 0 }}>
        {FILTERS.map((f) => (
          <button key={f.id} className={'hub-tab' + (kind === f.id ? ' active' : '')} onClick={() => setKind(f.id)}>{f.label}</button>
        ))}
      </div>
      {err && <div className="hub-msg err">{err}</div>}
      {events == null ? <EmptyState text="Loading…" />
        : events.length === 0 ? <EmptyState text="No events recorded yet. Actions will appear here as the agent works." />
          : (
            <div>
              {events.map((e, i) => (
                <div key={i} className="hub-row">
                  <div className="hub-row-main">
                    <div className="hub-row-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <i style={{ width: 7, height: 7, borderRadius: '50%', background: evtColor(e.kind), flexShrink: 0 }} />
                      {evtSummary(e)}
                    </div>
                    {(e.error || e.request) && (
                      <div className="hub-row-sub" style={{ color: e.error ? 'var(--err)' : 'var(--muted)' }}>
                        {e.error ? `error: ${e.error}` : e.request}
                      </div>
                    )}
                  </div>
                  <div className="hub-row-sub" style={{ flexShrink: 0, whiteSpace: 'nowrap' }}>
                    {new Date(e.ts * 1000).toLocaleString()}
                  </div>
                </div>
              ))}
            </div>
          )}
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

function SystemPanel({ onRestart }: { onRestart: () => void }) {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const load = useCallback(() => { hub.system().then(setSys).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);

  const setApproval = useCallback(async (mode: string) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.setApproval(mode);
      if (r.error) setMsg(r.error);
      else { setSys((s) => (s ? { ...s, code_approval: mode } : s)); onRestart(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [onRestart]);

  const saveFeatures = useCallback(async (patch: Record<string, boolean>) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.save({ features: patch });
      if (r.error) setMsg(r.error);
      else { load(); onRestart(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [load, onRestart]);

  const setRetention = useCallback(async (days: number) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.setRetention(days);
      if (r.error) setMsg(r.error);
      else { setSys((s) => (s ? { ...s, retention_days: days } : s)); onRestart(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [onRestart]);

  const APPROVALS: { id: string; title: string; sub: string }[] = [
    { id: 'all', title: 'All changes need approval', sub: 'Safest. Every edit Ava makes to its own code waits for you.' },
    { id: 'policy', title: 'Only sensitive paths', sub: 'Auth/config/deploy edits are gated; routine edits auto-commit to git.' },
    { id: 'none', title: 'Auto-apply', sub: 'Trusted box — all non-secret edits commit automatically.' },
  ];

  return (
    <>
      <Panel title="About" subtitle="Your instance">
        {sys ? (
          <dl className="hub-kv">
            <dt>Name</dt><dd>{sys.brand}</dd>
            <dt>Version</dt><dd>{sys.version}</dd>
          </dl>
        ) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Self-editing governance" subtitle="How Ava's code-change agent applies edits to its own repo (secrets, models/ and .git are always denied).">
        <div className="hub-opts" style={{ gridTemplateColumns: '1fr' }}>
          {APPROVALS.map((a) => (
            <button key={a.id} className={'hub-opt' + (sys?.code_approval === a.id ? ' sel' : '')}
              disabled={busy} onClick={() => setApproval(a.id)}>
              <b>{a.title}{sys?.code_approval === a.id ? ' — current' : ''}</b>
              <small>{a.sub}</small>
            </button>
          ))}
        </div>
      </Panel>

      <div className="hub-section" />
      <Panel title="Data retention" subtitle="How long Ava keeps performance metrics and hardware history. Older data is pruned automatically; the dashboard's time-range filters can only reach back as far as this.">
        {sys ? (
          <>
            <div className="hub-field" style={{ maxWidth: 320 }}>
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
      <Panel title="Optional features" subtitle="All off by default so a fresh install stays minimal.">
        {sys && (
          <>
            <label className="hub-check">
              <input type="checkbox" checked={sys.image} disabled={busy}
                onChange={(e) => saveFeatures({ image: e.target.checked })} />
              <span className="hub-check-main">
                <span className="hub-check-title">Image / video generation</span>
                <span className="hub-check-sub">via the the GPU service connector</span>
              </span>
            </label>
            <label className="hub-check">
              <input type="checkbox" checked={sys.web_search} disabled={busy}
                onChange={(e) => saveFeatures({ web_search: e.target.checked })} />
              <span className="hub-check-main">
                <span className="hub-check-title">Web search</span>
                <span className="hub-check-sub">self-hosted SearXNG + guarded fetch</span>
              </span>
            </label>
            <label className="hub-check">
              <input type="checkbox" checked={sys.voice} disabled={busy}
                onChange={(e) => saveFeatures({ voice: e.target.checked })} />
              <span className="hub-check-main">
                <span className="hub-check-title">Voice</span>
                <span className="hub-check-sub">
                  push-to-talk (needs requirements-voice.txt).{' '}
                  {sys.voice && (sys.voiceprint
                    ? <Badge tone="ok">voiceprint enrolled</Badge>
                    : <Badge tone="warn">no voiceprint — enroll on the Voice tab</Badge>)}
                </span>
              </span>
            </label>
          </>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Learning" subtitle="Periodic local-first self-analysis that parks improvement proposals for your approval.">
        {sys && (
          <dl className="hub-kv">
            <dt>Status</dt><dd>{sys.learning_enabled ? <Badge tone="ok">on · every {sys.learning_interval_h}h</Badge> : <Badge tone="warn">off</Badge>}</dd>
            <dt>Proposals</dt><dd><span style={{ color: 'var(--muted)' }}>Review &amp; run cycles on the Operations → Control Center page.</span></dd>
          </dl>
        )}
      </Panel>
      {msg && <div className="hub-msg err">{msg}</div>}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────
export function HubView() {
  const [tab, setTab] = useState<TabId>('overview');
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
