import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/layout';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import { ResourceError } from '../ui/ResourceState';
import type { BenchResult, PullStatus } from '../hubApi';
import { Badge } from '../ui/Badge';
import { fitLine } from '../../../lib/modelFit';

// The model store (download models sized to your hardware) and the head-to-head
// benchmark nested inside it. Rendered by BrainPanel, under the brain manager.

export function ModelStorePanel() {
  const storeRes = useResource(() => hub.models());
  const { data: store, reload: load } = storeRes;
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
    <>
    <ResourceError r={storeRes} label="the model store" />
    <Panel
      title="Model store"
      subtitle={store ? `Downloads land in ${store.store} · detected tier: ${store.detected_tier}${store.available_gb ? ` · ${store.available_gb} GB` : ''}` : 'Download models sized to your hardware.'}
      right={
        <button type="button" className="hub-btn sm" onClick={() => start('auto')} disabled={running}>
          <Icon name="sparkles" />{running ? 'Pulling…' : 'Pull recommended'}
        </button>
      }
    >
      {store == null ? <EmptyState text="Loading model store…" />
        : store.roles.length === 0 ? <EmptyState text="No models declared in ava.yaml (models: …) — 'Pull recommended' picks one for your tier." />
          : store.roles.map((m) => {
            // Silence is the default. `fitLine` returns null for should_fit and
            // for unknown, so only the two readings an owner would act on ever
            // add a line — see lib/modelFit.ts for why that is the whole point.
            const fit = fitLine(m.fit);
            return (
            <div className="hub-row" key={m.role}>
              <div className="hub-row-main">
                <div className="hub-row-title">{m.role} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>· {m.id}</span></div>
                <div className="hub-row-sub">{m.engine}{m.tier ? ` · tier ${m.tier}` : ''}</div>
                {fit && (
                  <div className={`hub-row-sub hub-row-fit tone-${fit.tone}`}>{fit.text}</div>
                )}
              </div>
              <div className="hub-row-actions">
                {m.present ? <Badge tone="ok">downloaded</Badge> : (
                  <button type="button" className="hub-btn ghost sm" onClick={() => start(m.role)} disabled={running}>
                    <Icon name="cloud" />Pull
                  </button>
                )}
              </div>
            </div>
            );
          })}

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
    </>
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
  const benchRes = useResource(() => hub.benchStatus());
  const { data: bench, setData: setBench } = benchRes;
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
    <>
    <ResourceError r={benchRes} label="the benchmark status" />
    <div style={{ borderTop: '1px solid var(--line)', paddingTop: 16 }}>
      <div className="hub-row" style={{ border: 0, padding: 0 }}>
        <div className="hub-row-main">
          <div className="hub-row-title">Compare models</div>
          <div className="hub-row-sub">Run the same prompt on every backend — throughput and time-to-first-token, side by side.</div>
        </div>
        <button type="button" className="hub-btn sm" onClick={run} disabled={running}>
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
    </>
  );
}
