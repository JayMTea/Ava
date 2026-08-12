import { useCallback, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { api } from '../../../lib/api';
import { EmptyState, Panel } from '../../dashboard/layout';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import { ResourceError } from '../ui/ResourceState';
import type { Backend, BackendTestResult } from '../hubApi';
import { Badge } from '../ui/Badge';
import { stateCopy, stateTone } from '../../../lib/modelState';
import type { StatefulRow } from '../../../lib/modelState';
import { ModelStorePanel } from './ModelStorePanel';
import { ModelStoreList } from './ModelStoreList';

// Setup -> Agent -> Brain: what Ava thinks with. The multi-model brain manager
// plus the model store and the head-to-head benchmark, which are the two things
// you reach for while deciding what the brain should be.
//
// Split out of the old 1174-line AgentPanel.tsx, which stacked five unrelated
// sections in one ~3000px scroll. AgentPanel.tsx is now just the sub-tab router.

// Whether the brain is actually up, in the SAME words the floating hardware
// monitor uses (lib/modelState). This panel described a model as the brain
// while the monitor reported none, because neither read the other's source.
function BrainState({ row }: { row: (StatefulRow & { memory_gb?: number | null }) | null }) {
  if (!row) return null;
  return (
    <small style={{ color: 'var(--muted)', display: 'block', marginTop: 3 }}>
      <Badge tone={stateTone(row)}>{stateCopy(row).label}</Badge>{' '}
      {stateCopy(row).hint}
      {row.memory_gb != null && ` · ${row.memory_gb.toFixed(1)} GB in memory`}
    </small>
  );
}
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

  const listRes = useResource(() => hub.backendList());
  const beRes = useResource(() => hub.backends());
  const { data: list, reload: load } = listRes;
  const { data: be } = beRes;
  // The agent sandbox's own model: while the agent runtime is active, THAT is
  // what chat turns think with — the backends below only serve the tool-less
  // fallback and router roles. Without pinning it here, this panel reads
  // "no model linked" on a machine where Ava is plainly answering.
  const agentRes = useResource(() => hub.agentStatus());
  const { data: agent } = agentRes;
  // The router's live route: when nothing is configured it serves a built-in
  // default (`implicit`) — with the agent off, THAT is the operative brain.
  const routeRes = useResource(() => api.getModel());
  const { data: route } = routeRes;
  // The SAME reading the floating hardware monitor shows, from the same
  // endpoint and the same vocabulary (lib/modelState). This panel could say
  // "brain" while the monitor said no model existed, because the two never
  // consulted each other; now they render one fact.
  //
  // Deliberately NOT in `brainErr` below: this is liveness ON TOP of the
  // configuration this section exists to edit. If /api/hardware hiccups the
  // state chip simply does not render, which is right — turning a telemetry
  // blip into a red error over a correctly configured brain would report a
  // problem the user does not have, and the floating monitor already owns
  // that failure.
  const hwRes = useResource(() => api.hardware());
  const brainRow = (hwRes.data?.models || []).find((m) => m.role_key === 'brain') || null;
  // One surface for this section's configuration resources — see Overview.
  const brainErr = [listRes, beRes, agentRes, routeRes].find((r) => r.error);

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
  //
  // The DECISION comes from the backend's resolver (`agent.brain.source`), not
  // from re-deriving it here out of `available && sandbox_model`. That local
  // derivation was a fourth independent answer to "which model is live", across
  // three endpoints, and nothing kept it in step with the other three. The
  // `sandbox_*` fields are still what gets RENDERED — they carry the detail —
  // and the old expression stays as the fallback for a payload without `brain`.
  const ownsBrain = agent?.brain
    ? agent.brain.source === 'agent' && !!agent.brain.model
    : !!(agent?.available && agent.sandbox_model);
  const agentBrain = ownsBrain && agent?.sandbox_model ? agent : null;
  // Agent off + nothing configured: the router still serves its built-in
  // default, so chat works — show that instead of "no model linked".
  const routerDefault = !agentBrain && backends.length === 0
    ? (route?.backends || []).find((b) => b.implicit) || null : null;

  return (
    <>
    {brainErr && <ResourceError r={brainErr} label="your model settings" />}
    <Panel
      title="Ava's brain"
      subtitle="Link any model — a local engine (Ollama, MLX, LM Studio, llama.cpp, vLLM) or any OpenAI-compatible cloud provider — and pick which one Ava thinks with."
      right={<button type="button" className="hub-btn sm" onClick={openAdd}><Icon name="sparkles" />Add a model</button>}
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
              <BrainState row={brainRow} />
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
                <Badge tone="muted">from environment</Badge>
              </b>
              <small style={{ color: 'var(--muted)', wordBreak: 'break-all' }}>
                {routerDefault.model} — declared by AVA_BACKEND_URL rather than in
                Ava's config, which is how Docker installs are wired. Add a model
                below to manage it here instead.
              </small>
              <BrainState row={brainRow} />
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
                      {b.is_brain && brainRow && (
                        <Badge tone={stateTone(brainRow)}>{stateCopy(brainRow).label}</Badge>
                      )}
                    </b>
                    <small style={{ color: 'var(--muted)', wordBreak: 'break-all' }}>
                      {b.engine} · {b.model || 'no model set'} · {b.base_url}
                      {!b.local && (b.has_key ? ' · key ✓' : ' · no key')}
                    </small>
                  </div>
                  {!b.is_brain && (
                    <button type="button" className="hub-btn sm ghost" onClick={() => setBrain(b.id)}>Use as brain</button>
                  )}
                  <button type="button" className="hub-btn sm ghost" onClick={() => openEdit(b)}>Edit</button>
                  <button type="button" className="hub-btn sm ghost" onClick={() => remove(b.id)} aria-label={`Remove ${b.id}`}>
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
            <button type="button" className="hub-btn ghost" onClick={runTest} disabled={testing || !baseUrl.trim() || !model.trim()}>
              {testing ? 'Testing…' : 'Test connection'}
            </button>
            <button type="button" className="hub-btn" onClick={save} disabled={busy || !(editing || id).trim() || !baseUrl.trim() || !model.trim()}>
              <Icon name="check" />{busy ? 'Saving…' : 'Save model'}
            </button>
            <button type="button" className="hub-btn ghost" onClick={() => { setShowForm(false); resetForm(); }} disabled={busy}>Cancel</button>
          </div>
          {msg && <div className="hub-msg err">{msg}</div>}
          {be && !be.any_up && !isCloud && (
            <div className="hub-msg" style={{ color: 'var(--muted)' }}>
              No local engine detected. Start one first (e.g. install Ollama and run <code>ollama serve</code>), or link a cloud provider.
            </div>
          )}
          {be && be.any_up && !isCloud && (
            <div className="hub-msg" style={{ color: 'var(--muted)' }}>
              Detected: {be.backends.filter((b) => b.up).map((b) => `${b.engine} at ${b.base_url}`).join(', ')}
            </div>
          )}
        </div>
      )}
    </Panel>
    </>
  );
}

// The Brain sub-tab. The store and the benchmark sit under the manager because
// they exist to answer "which model should this be?" — separating them would
// mean leaving the page mid-decision.
export function BrainPanel({ onRestart }: { onRestart: () => void }) {
  return (
    <>
      <BrainManager onRestart={onRestart} />
      <div className="hub-section" />
      <ModelStoreList />
      <ModelStorePanel />
    </>
  );
}
