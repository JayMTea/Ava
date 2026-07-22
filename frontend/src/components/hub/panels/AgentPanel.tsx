import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { AppDot, appAccent, appById, appForTool, appsForTools } from '../../../lib/appColor';
import { MarkdownLite } from '../../../lib/markdown';
import { api } from '../../../lib/api';
import { EmptyState, Panel } from '../../dashboard/primitives';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import type { Backend, BackendTestResult, BenchResult, PullStatus, Skill, SkillList } from '../hubApi';
import { Badge } from '../ui/Badge';
import { StatRow } from '../ui/StatRow';

// Agent — runtime status + provisioning, the multi-model brain manager, the
// model store, benchmarks, and the skills list. ENGINE_PRESETS is the "add a
// model" form's endpoint presets.
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

// The multi-model "brain" manager: link one or more OpenAI-compatible models
// (local engines or any cloud provider), test each before committing, and pick
// which one is Ava's brain. Cloud keys go to the secrets store, never ava.yaml.
function BrainManager({ onRestart }: { onRestart: () => void }) {
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

  const { data: list, reload: load } = useResource(() => hub.backendList());
  const { data: be } = useResource(() => hub.backends());
  // The agent sandbox's own model: while the agent runtime is active, THAT is
  // what chat turns think with — the backends below only serve the tool-less
  // fallback and router roles. Without pinning it here, this panel reads
  // "no model linked" on a machine where Ava is plainly answering.
  const { data: agent } = useResource(() => hub.agentStatus());
  // The router's live route: when nothing is configured it serves a built-in
  // default (`implicit`) — with the agent off, THAT is the operative brain.
  const { data: route } = useResource(() => api.getModel());

  const preset = ENGINE_PRESETS.find((p) => p.value === engine) ?? ENGINE_PRESETS[0];
  const isCloud = !!preset.cloud;

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
  const { data: store, reload: load } = useResource(() => hub.models());
  const [pull, setPull] = useState<PullStatus | null>(null);
  const [msg, setMsg] = useState('');
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => { hub.pullStatus().then(setPull).catch(() => {}); }, []);

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
  const { data: bench, setData: setBench } = useResource(() => hub.benchStatus());
  const [prompt, setPrompt] = useState('');
  const [msg, setMsg] = useState('');
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

export function AgentPanel({ onRestart }: { onRestart: () => void }) {
  const { data: st, reload: load } = useResource(() => hub.agentStatus());
  const [steps, setSteps] = useState<{ step: string; ok: boolean; detail: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState('');

  // Provision is bespoke — it renders a step list, not a one-line message.
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
