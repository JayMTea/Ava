import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../lib/icons';
import { EmptyState, Panel } from '../dashboard/primitives';
import { api } from '../../lib/api';
import { hub } from './hubApi';
import type {
  AgentStatus, BackendProbe, GenerateResult, HardwareInfo, HubConnector, SystemInfo,
} from './hubApi';

// ─────────────────────────────────────────────────────────────────────────────
// Shared bits
// ─────────────────────────────────────────────────────────────────────────────
type TabId = 'overview' | 'models' | 'agent' | 'connectors' | 'system';

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'gauge' },
  { id: 'models', label: 'Models', icon: 'cloud' },
  { id: 'agent', label: 'Agent', icon: 'bot' },
  { id: 'connectors', label: 'Connectors', icon: 'panel' },
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

// ─────────────────────────────────────────────────────────────────────────────
// Overview
// ─────────────────────────────────────────────────────────────────────────────
function Overview({ onGo }: { onGo: (t: TabId) => void }) {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [agent, setAgent] = useState<AgentStatus | null>(null);
  const [conns, setConns] = useState<HubConnector[]>([]);
  const [backends, setBackends] = useState<BackendProbe | null>(null);

  useEffect(() => {
    hub.system().then(setSys).catch(() => {});
    hub.agentStatus().then(setAgent).catch(() => {});
    hub.connectors().then((r) => setConns(r.connectors)).catch(() => {});
    hub.backends().then(setBackends).catch(() => {});
  }, []);

  const engineUp = backends && (backends.vllm || backends.ollama);
  const enabledConns = conns.filter((c) => c.enabled).length;

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
          Each tab configures one piece: your <b>model</b>, the <b>agent</b> runtime that gives Ava
          tools and memory, the <b>connectors</b> that wire in your apps, and <b>system</b> settings
          like self-editing governance. Changes are written to <b>ava.yaml</b> — never to source.
        </p>
      </Panel>

      <div className="hub-section" />
      <div className="db-grid db-grid-2">
        {card('models', 'cloud', 'Model',
          engineUp ? <Badge tone="ok">local engine up</Badge> : <Badge tone="warn">not detected</Badge>,
          'inference backend & model')}
        {card('agent', 'bot', 'Agent runtime',
          agent?.available ? <Badge tone="ok">{agent.name} ready</Badge> : <Badge tone="warn">not provisioned</Badge>,
          'tools · memory · sandbox')}
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
function ModelsPanel({ onRestart }: { onRestart: () => void }) {
  const [hw, setHw] = useState<HardwareInfo | null>(null);
  const [be, setBe] = useState<BackendProbe | null>(null);
  const [mode, setMode] = useState<'local' | 'cloud'>('local');
  const [engine, setEngine] = useState('vllm');
  const [baseUrl, setBaseUrl] = useState('http://127.0.0.1:8002/v1');
  const [model, setModel] = useState('');
  const [cBase, setCBase] = useState('https://api.openai.com/v1');
  const [cModel, setCModel] = useState('');
  const [cKey, setCKey] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    hub.hardware().then(setHw).catch(() => {});
    hub.backends().then((b) => {
      setBe(b);
      if (b.ollama && !b.vllm) { setEngine('ollama'); setBaseUrl('http://127.0.0.1:11434/v1'); }
    }).catch(() => {});
  }, []);

  const save = useCallback(async () => {
    setBusy(true); setMsg('');
    const inference = mode === 'cloud'
      ? { mode, base_url: cBase.trim(), model: cModel.trim(), api_key: cKey.trim() }
      : { mode, engine, base_url: baseUrl.trim(), model: model.trim() };
    try {
      const r = await hub.save({ inference });
      if (r.error) { setMsg(r.error); }
      else { setMsg(''); onRestart(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [mode, engine, baseUrl, model, cBase, cModel, cKey, onRestart]);

  return (
    <>
      <Panel title="Your hardware" subtitle="Detected automatically — picks a sensible model tier.">
        {hw ? (
          <dl className="hub-kv">
            <dt>Compute</dt><dd>{hw.gpu || 'No local GPU detected'}</dd>
            <dt>Usable memory</dt><dd>{hw.fit_gb != null ? `${hw.fit_gb} GB (${hw.source || 'detected'})` : '—'}</dd>
            <dt>Recommended tier</dt><dd><Badge tone="accent">{hw.tier}</Badge> &nbsp;<span style={{ color: 'var(--muted)' }}>{hw.hint}</span></dd>
          </dl>
        ) : <EmptyState text="Detecting hardware…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Inference backend" subtitle="Where Ava's chat runs. Local keeps everything on your box; cloud uses any OpenAI-compatible provider.">
        <div className="hub-opts">
          <button className={'hub-opt' + (mode === 'local' ? ' sel' : '')} onClick={() => setMode('local')}>
            <b>Local engine</b>
            <small>{be?.ollama ? 'Ollama detected on :11434' : be?.vllm ? 'vLLM detected on :8002' : 'point at a running local engine'}</small>
          </button>
          <button className={'hub-opt' + (mode === 'cloud' ? ' sel' : '')} onClick={() => setMode('cloud')}>
            <b>Cloud API key</b>
            <small>any OpenAI-compatible provider</small>
          </button>
        </div>

        {mode === 'local' ? (
          <>
            <div className="hub-field">
              <label>Engine</label>
              <select className="hub-select" value={engine} onChange={(e) => setEngine(e.target.value)}>
                <option value="vllm">vLLM</option>
                <option value="ollama">Ollama</option>
                <option value="llamacpp">llama.cpp</option>
              </select>
            </div>
            <div className="hub-fieldrow">
              <div className="hub-field"><label>Base URL</label>
                <input className="hub-input" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></div>
              <div className="hub-field"><label>Model</label>
                <input className="hub-input" value={model} onChange={(e) => setModel(e.target.value)} placeholder="e.g. llama3.2" /></div>
            </div>
          </>
        ) : (
          <>
            <div className="hub-field"><label>Provider base URL</label>
              <input className="hub-input" value={cBase} onChange={(e) => setCBase(e.target.value)} /></div>
            <div className="hub-fieldrow">
              <div className="hub-field"><label>Model</label>
                <input className="hub-input" value={cModel} onChange={(e) => setCModel(e.target.value)} placeholder="e.g. gpt-4o-mini" /></div>
              <div className="hub-field"><label>API key <span style={{ opacity: 0.7 }}>(stored in secrets/, never in ava.yaml)</span></label>
                <input className="hub-input" type="password" value={cKey} onChange={(e) => setCKey(e.target.value)} /></div>
            </div>
          </>
        )}

        <div className="hub-btn-row">
          <button className="hub-btn" onClick={save} disabled={busy}>
            <Icon name="check" />{busy ? 'Saving…' : 'Save inference config'}
          </button>
        </div>
        {msg && <div className="hub-msg err">{msg}</div>}
        <div className="hub-note" style={{ marginTop: 14 }}>
          To download a model sized to your hardware, run <b>ava models pull --auto</b> in a terminal.
        </div>
      </Panel>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Agent
// ─────────────────────────────────────────────────────────────────────────────
function AgentPanel() {
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
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Connectors
// ─────────────────────────────────────────────────────────────────────────────
function ConnectorRow({ c }: { c: HubConnector }) {
  const [gen, setGen] = useState<GenerateResult | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const preview = useCallback(async () => {
    setBusy(true); setMsg('');
    try { setGen(await hub.generate(c.id, false)); setOpen(true); }
    catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [c.id]);

  const write = useCallback(async () => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.generate(c.id, true);
      setGen(r);
      setMsg(`Wrote ${r.wrote?.length || 0} file(s). Run \`cd agent && ./install.sh\` to deploy into the sandbox.`);
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [c.id]);

  return (
    <div style={{ borderBottom: '1px solid var(--line)', padding: '12px 0' }}>
      <div className="hub-row" style={{ border: 0, padding: 0 }}>
        <div className="hub-row-main">
          <div className="hub-row-title">
            {c.label} <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 'var(--fs-xs)' }}>· {c.kind}</span>
          </div>
          <div className="hub-row-sub" style={{ display: 'flex', gap: 6, marginTop: 5, flexWrap: 'wrap' }}>
            {c.enabled ? <Badge tone="ok">enabled</Badge> : <Badge>disabled</Badge>}
            {c.actions > 0 && <Badge tone="accent">{c.actions} action{c.actions === 1 ? '' : 's'}</Badge>}
            {c.actions > 0 && (c.has_tools ? <Badge tone="ok">tools ✓</Badge> : <Badge tone="warn">tools stale</Badge>)}
            {c.renders_policy && (c.has_policy ? <Badge tone="ok">policy ✓</Badge> : <Badge tone="warn">policy stale</Badge>)}
          </div>
        </div>
        {c.actions > 0 && (
          <div className="hub-row-actions">
            <button className="hub-btn ghost sm" onClick={preview} disabled={busy}>
              <Icon name="code" />Preview
            </button>
            <button className="hub-btn sm" onClick={write} disabled={busy}>
              <Icon name="check" />Generate & deploy
            </button>
          </div>
        )}
      </div>
      {msg && <div className="hub-msg ok" style={{ marginTop: 8 }}>{msg}</div>}
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

function ConnectorsPanel() {
  const [conns, setConns] = useState<HubConnector[] | null>(null);
  useEffect(() => { hub.connectors().then((r) => setConns(r.connectors)).catch(() => setConns([])); }, []);
  return (
    <Panel
      title="Connectors"
      subtitle="Each connector is one manifest that wires an app into Ava — its health, metrics, agent tools, and egress security policy."
    >
      {conns == null ? <EmptyState text="Loading connectors…" />
        : conns.length === 0 ? <EmptyState text="No connectors yet." />
          : conns.map((c) => <ConnectorRow key={c.id} c={c} />)}
      <div className="hub-note" style={{ marginTop: 16 }}>
        Add a new app with <b>ava connector new &lt;name&gt;</b>, edit its <b>connector.yaml</b>,
        then use <b>Generate &amp; deploy</b> above. See <b>docs/CONNECTOR_SDK.md</b>.
      </div>
    </Panel>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// System
// ─────────────────────────────────────────────────────────────────────────────
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
                    : <Badge tone="warn">no voiceprint — run enroll_voice.py</Badge>)}
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
        <div className="hub-head">
          <h2>Set up {brand}</h2>
          <p>Configure your model, agent, apps, and system — all from here, written to your config, nothing to source.</p>
        </div>

        <RestartBanner show={restart} />

        <div className="hub-tabs">
          {TABS.map((t) => (
            <button key={t.id} className={'hub-tab' + (tab === t.id ? ' active' : '')} onClick={() => setTab(t.id)}>
              <Icon name={t.icon} />{t.label}
            </button>
          ))}
        </div>

        {tab === 'overview' && <Overview onGo={setTab} />}
        {tab === 'models' && <ModelsPanel onRestart={notifyRestart} />}
        {tab === 'agent' && <AgentPanel />}
        {tab === 'connectors' && <ConnectorsPanel />}
        {tab === 'system' && <SystemPanel onRestart={notifyRestart} />}
      </div>
    </div>
  );
}
