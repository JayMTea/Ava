import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../lib/icons';
import { EmptyState, Panel } from '../dashboard/primitives';
import { api } from '../../lib/api';
import { hub } from './hubApi';
import type {
  AgentStatus, AuditEvent, BackendProbe, BenchStatus, CostSettings, EnrollResult,
  GenerateResult, HardwareInfo, HubConnector, ModelStore, PullStatus, SystemInfo, VoiceStatus,
} from './hubApi';

// ─────────────────────────────────────────────────────────────────────────────
// Shared bits
// ─────────────────────────────────────────────────────────────────────────────
type TabId = 'overview' | 'models' | 'agent' | 'connectors' | 'voice' | 'budgets' | 'history' | 'system';

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'gauge' },
  { id: 'models', label: 'Models', icon: 'cloud' },
  { id: 'agent', label: 'Agent', icon: 'bot' },
  { id: 'connectors', label: 'Connectors', icon: 'panel' },
  { id: 'voice', label: 'Voice', icon: 'mic' },
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
      </Panel>

      <div className="hub-section" />
      <ModelStorePanel />
    </>
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

function BenchPanel() {
  const [bench, setBench] = useState<BenchStatus | null>(null);
  const [prompt, setPrompt] = useState('');
  const [msg, setMsg] = useState('');
  const load = useCallback(() => { hub.benchStatus().then(setBench).catch(() => {}); }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (bench?.status !== 'running') return;
    const t = setInterval(() => hub.benchStatus().then(setBench).catch(() => {}), 1500);
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
  return (
    <div style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
      <div className="hub-row" style={{ border: 0, padding: 0 }}>
        <div className="hub-row-main">
          <div className="hub-row-title">Compare models</div>
          <div className="hub-row-sub">Run the same prompt on every backend — TTFT and tokens/sec side by side.</div>
        </div>
        <button className="hub-btn sm" onClick={run} disabled={running}>
          <Icon name={running ? 'refresh' : 'chart'} />{running ? 'Benchmarking…' : 'Run benchmark'}
        </button>
      </div>
      <input className="hub-input" style={{ marginTop: 10 }} value={prompt}
        onChange={(e) => setPrompt(e.target.value)} placeholder="Optional prompt (default: a short standard prompt)" />
      {msg && <div className="hub-msg err">{msg}</div>}
      {res && res.results.length > 0 && (
        <div className="hub-preview" style={{ marginTop: 12 }}>
          <div className="hub-preview-head"><Icon name="chart" /> {res.prompt ? `"${res.prompt.slice(0, 60)}"` : 'results'}</div>
          <div style={{ padding: 12 }}>
            {res.results.map((r) => (
              <div className="hub-row" key={r.id} style={{ padding: '8px 0' }}>
                <div className="hub-row-main">
                  <div className="hub-row-title" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    {r.id === res.winner && <Badge tone="ok">fastest</Badge>}
                    {r.id} <span style={{ color: 'var(--muted)', fontWeight: 400, fontSize: 'var(--fs-xs)' }}>· {r.engine}</span>
                  </div>
                  {!r.ok && <div className="hub-row-sub" style={{ color: 'var(--err)' }}>{r.error}</div>}
                </div>
                {r.ok && (
                  <div className="hub-row-sub" style={{ flexShrink: 0, textAlign: 'right', fontFamily: 'var(--font-mono)' }}>
                    <b style={{ color: 'var(--txt)' }}>{r.tok_s}</b> tok/s · {r.ttft_ms}ms TTFT · {r.estimated_tokens ? '~' : ''}{r.tokens} tok
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {res && res.results.length === 0 && (
        <div className="hub-msg" style={{ color: 'var(--muted)' }}>{res.error || 'No backends configured to benchmark.'}</div>
      )}
    </div>
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
            {c.mcp && <Badge tone="accent">MCP server</Badge>}
            {c.actions > 0 && <Badge tone="accent">{c.actions} action{c.actions === 1 ? '' : 's'}</Badge>}
            {c.actions > 0 && (c.has_tools ? <Badge tone="ok">tools ✓</Badge> : <Badge tone="warn">tools stale</Badge>)}
            {c.renders_policy && (c.has_policy ? <Badge tone="ok">policy ✓</Badge> : <Badge tone="warn">policy stale</Badge>)}
          </div>
        </div>
        {(c.actions > 0 || (c.mcp && c.renders_policy)) && (
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

interface ActionDraft { id: string; method: string; path: string; description: string }

function NewConnectorForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<'rest' | 'mcp'>('rest');
  const [id, setId] = useState('');
  const [label, setLabel] = useState('');
  const [probe, setProbe] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [actions, setActions] = useState<ActionDraft[]>([]);
  const [mcpUrl, setMcpUrl] = useState('');
  const [mcpCommand, setMcpCommand] = useState('');
  const [mcpToken, setMcpToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [done, setDone] = useState('');

  const setAction = (i: number, patch: Partial<ActionDraft>) =>
    setActions((a) => a.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  const create = useCallback(async () => {
    setBusy(true); setMsg(''); setDone('');
    try {
      const r = await hub.newConnector({
        id: id.trim().toLowerCase(),
        label: label.trim() || undefined,
        probe: probe.trim() || undefined,
        base_url: mode === 'rest' ? baseUrl.trim() || undefined : undefined,
        actions: mode === 'rest' ? actions.filter((a) => a.id.trim() && a.path.trim()) : undefined,
        mcp: mode === 'mcp' ? {
          url: mcpUrl.trim() || undefined,
          command: mcpCommand.trim() || undefined,
          token_env: mcpToken.trim() || undefined,
        } : undefined,
      });
      if (!r.ok) { setMsg(r.error || 'could not create connector'); }
      else {
        setDone(`Created ${r.path}. Now Preview / Generate & deploy its policy below.`);
        setId(''); setLabel(''); setProbe(''); setBaseUrl(''); setActions([]);
        setMcpUrl(''); setMcpCommand(''); setMcpToken('');
        onCreated();
      }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [id, label, probe, baseUrl, actions, mode, mcpUrl, mcpCommand, mcpToken, onCreated]);

  if (!open) {
    return (
      <div className="hub-btn-row" style={{ marginTop: 0 }}>
        <button className="hub-btn" onClick={() => setOpen(true)}><Icon name="plus" />New connector</button>
        {done && <span className="hub-msg ok" style={{ marginTop: 0, alignSelf: 'center' }}>{done}</span>}
      </div>
    );
  }
  return (
    <Panel title="New connector" subtitle="Describe your app; Ava writes the manifest. Actions become agent tools with a matching egress policy — preview both before deploying." right={
      <button className="hub-btn ghost sm" onClick={() => setOpen(false)}>Cancel</button>
    }>
      <div className="hub-fieldrow">
        <div className="hub-field"><label>ID (a-z, 0-9, -, _)</label>
          <input className="hub-input" value={id} onChange={(e) => setId(e.target.value)} placeholder="myapp" /></div>
        <div className="hub-field"><label>Label</label>
          <input className="hub-input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="My App" /></div>
      </div>
      <div className="hub-field">
        <label>What kind of app is this?</label>
        <div className="hub-opts">
          <button className={'hub-opt' + (mode === 'rest' ? ' sel' : '')} onClick={() => setMode('rest')}>
            <b>REST API</b><small>declare actions; each becomes an agent tool + egress rule</small>
          </button>
          <button className={'hub-opt' + (mode === 'mcp' ? ' sel' : '')} onClick={() => setMode('mcp')}>
            <b>MCP server</b><small>wrap any Model Context Protocol server — tools discovered live, sandboxed behind an egress policy</small>
          </button>
        </div>
      </div>

      <div className="hub-fieldrow">
        <div className="hub-field"><label>Health probe URL (optional)</label>
          <input className="hub-input" value={probe} onChange={(e) => setProbe(e.target.value)} placeholder="http://127.0.0.1:9000/health" /></div>
        {mode === 'rest' && (
          <div className="hub-field"><label>App base URL (where actions are sent)</label>
            <input className="hub-input" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://127.0.0.1:9000" /></div>
        )}
      </div>

      {mode === 'mcp' && (
        <>
          <div className="hub-fieldrow">
            <div className="hub-field"><label>Server URL (HTTP transport)</label>
              <input className="hub-input" value={mcpUrl} onChange={(e) => setMcpUrl(e.target.value)} placeholder="http://127.0.0.1:9200/mcp" /></div>
            <div className="hub-field"><label>… or command (stdio transport)</label>
              <input className="hub-input" value={mcpCommand} onChange={(e) => setMcpCommand(e.target.value)} placeholder="npx -y @modelcontextprotocol/server-github" /></div>
          </div>
          <div className="hub-field" style={{ maxWidth: 360 }}><label>Bearer token env var (optional)</label>
            <input className="hub-input" value={mcpToken} onChange={(e) => setMcpToken(e.target.value)} placeholder="MYMCP_TOKEN" /></div>
          <div className="hub-note">
            The agent never talks to this server directly — it reaches two policed bridge routes,
            and the generated egress policy allow-lists exactly those. A stdio command runs on this
            machine as you, like any MCP desktop client — only add servers you trust.
          </div>
        </>
      )}

      {mode === 'rest' && (
      <div className="hub-field">
        <label>Agent actions — each becomes a tool Ava can call (and an egress allow-rule)</label>
        {actions.map((a, i) => (
          <div className="hub-fieldrow" key={i} style={{ marginBottom: 8 }}>
            <input className="hub-input" style={{ flex: 1 }} value={a.id} placeholder="action_id"
              onChange={(e) => setAction(i, { id: e.target.value })} />
            <select className="hub-select" style={{ flex: '0 0 90px' }} value={a.method}
              onChange={(e) => setAction(i, { method: e.target.value })}>
              <option>POST</option><option>GET</option>
            </select>
            <input className="hub-input" style={{ flex: 2 }} value={a.path} placeholder="/api/do-thing"
              onChange={(e) => setAction(i, { path: e.target.value })} />
            <input className="hub-input" style={{ flex: 2 }} value={a.description} placeholder="what it does (shown to the agent)"
              onChange={(e) => setAction(i, { description: e.target.value })} />
            <button className="hub-btn ghost sm" style={{ flex: '0 0 auto' }} aria-label="Remove action"
              onClick={() => setActions((x) => x.filter((_, j) => j !== i))}><Icon name="trash" /></button>
          </div>
        ))}
        <button className="hub-btn ghost sm" onClick={() => setActions((a) => [...a, { id: '', method: 'POST', path: '', description: '' }])}>
          <Icon name="plus" />Add action
        </button>
      </div>
      )}

      <div className="hub-btn-row">
        <button className="hub-btn" onClick={create}
          disabled={busy || !id.trim() || (mode === 'mcp' && !mcpUrl.trim() && !mcpCommand.trim())}>
          <Icon name="check" />{busy ? 'Creating…' : 'Create connector'}
        </button>
      </div>
      {msg && <div className="hub-msg err">{msg}</div>}
    </Panel>
  );
}

function ConnectorsPanel() {
  const [conns, setConns] = useState<HubConnector[] | null>(null);
  const load = useCallback(() => {
    hub.connectors().then((r) => setConns(r.connectors)).catch(() => setConns([]));
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
        {conns == null ? <EmptyState text="Loading connectors…" />
          : conns.length === 0 ? <EmptyState text="No connectors yet — create one above." />
            : conns.map((c) => <ConnectorRow key={c.id} c={c} />)}
        <div className="hub-note" style={{ marginTop: 16 }}>
          After <b>Generate &amp; deploy</b>, run <b>cd agent &amp;&amp; ./install.sh</b> once to load the new
          tools into the sandbox. Full schema: <b>docs/CONNECTOR_SDK.md</b>.
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
  return e.kind;
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
        {tab === 'voice' && <VoicePanel onRestart={notifyRestart} />}
        {tab === 'budgets' && <BudgetsPanel />}
        {tab === 'history' && <HistoryPanel />}
        {tab === 'system' && <SystemPanel onRestart={notifyRestart} />}
      </div>
    </div>
  );
}
