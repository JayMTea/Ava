import { useEffect, useState, type CSSProperties } from 'react';
import type { AnalyticsArtifactPayload, AnalyticsArtifactReference, RecordedAnalyticsArtifactPayload, SupersetArtifactPayload } from '../../lib/types';
import { appAccent, appById } from '../../lib/appColor';
import { AppFrame } from '../AppFrame';
import './analytics-artifact.css';

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const format = (value: number | null) => value == null ? 'Unavailable' : number.format(value);
const sourceUrl = (url: string) => /^https:\/\//i.test(url) ? url : undefined;

/** Old saved results keep a faithful preview when their connector predates chart delivery. */
export function RecordedChart({ result, compact = false }: { result: RecordedAnalyticsArtifactPayload['result']; compact?: boolean }) {
  const shown = compact ? result.rows.slice(0, 6) : result.rows;
  const maximum = Math.max(1, ...result.rows.map(row => (row.value ?? 0) + (row.moe_90 ?? 0)));
  return <div className="analysis-chart" role="img" aria-label={`${result.unit} by ${result.filters.geography_level}. ${shown.map(row => `${row.label}: ${format(row.value)}; ${row.moe_90 == null ? 'margin of error unavailable' : `90% margin of error ${format(row.moe_90)}`}`).join('. ')}`}>
    {shown.map(row => <div className="analysis-chart-row" key={`${row.state}-${row.puma}-${row.label}`}>
      <div className="analysis-row-label"><span>{row.label}</span><strong>{format(row.value)}</strong></div>
      <svg viewBox="0 0 500 28" preserveAspectRatio="none" aria-hidden="true">
        <rect x="0" y="5" width={Math.max(0, (row.value ?? 0) / maximum * 490)} height="18" rx="3" fill="var(--analysis-accent)" />
        {row.moe_90 != null && row.value != null && <g stroke="currentColor" strokeWidth="2">
          <line x1={Math.max(0, row.value - row.moe_90) / maximum * 490} x2={(row.value + row.moe_90) / maximum * 490} y1="14" y2="14" />
          <line x1={Math.max(0, row.value - row.moe_90) / maximum * 490} x2={Math.max(0, row.value - row.moe_90) / maximum * 490} y1="8" y2="20" />
          <line x1={(row.value + row.moe_90) / maximum * 490} x2={(row.value + row.moe_90) / maximum * 490} y1="8" y2="20" />
        </g>}
      </svg>
    </div>)}
    {shown.length < result.rows.length && <p className="analysis-note">{result.rows.length} areas. Open the chart to see all.</p>}
  </div>;
}

export function AnalysisSources({ result }: { result: RecordedAnalyticsArtifactPayload['result'] }) {
  const citations = [...result.citations, ...result.sources.map(source => ({
    title: source.url.split('/').pop() || 'Source dataset', url: source.url,
  }))].filter((source, index, all) => sourceUrl(source.url) && all.findIndex(item => item.url === source.url) === index);
  if (!citations.length) return <p className="analysis-note">Source citations unavailable.</p>;
  return <div className="analysis-sources" aria-label="Chart sources"><span>Sources</span>
    {citations.slice(0, 3).map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title}</a>)}
    {citations.length > 3 && <details><summary>{citations.length - 3} more sources</summary>
      {citations.slice(3).map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title}</a>)}
    </details>}
  </div>;
}

export function SupersetChart({ data, artifact, onOpen }: {
  data: SupersetArtifactPayload; artifact: AnalyticsArtifactReference; onOpen?: () => void;
}) {
  const citations = data.chart.citations.filter(source => sourceUrl(source.url));
  return <section className={`analysis-artifact analysis-native${onOpen ? ' analysis-preview' : ''}`} style={{ '--analysis-accent': appAccent(artifact.connector_id) } as CSSProperties} aria-label={onOpen ? 'Chart preview' : 'Superset chart'}>
    {onOpen && <button type="button" className="analysis-preview-open" onClick={onOpen} aria-label={`Open chart: ${artifact.title}`}>
      <span className="analysis-preview-title">{artifact.title}</span><span className="analysis-preview-hint">View chart ↗</span>
    </button>}
    <div className="analysis-native-frame">
      <AppFrame id={artifact.connector_id} label={artifact.title} path={data.visualization.path} />
    </div>
    <p className="analysis-note">Live chart from {appById(artifact.connector_id)?.label ?? 'Analytics'}</p>
    {citations.length > 0 && <div className="analysis-sources" aria-label="Chart sources"><span>Sources</span>
      {citations.map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title}</a>)}
    </div>}
  </section>;
}

export function AnalyticsArtifact({ artifact, onOpen }: {
  artifact: AnalyticsArtifactReference;
  onOpen?: () => void;
}) {
  const [data, setData] = useState<AnalyticsArtifactPayload | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [imageFailed, setImageFailed] = useState(false);
  const compact = !!onOpen;
  // biome-ignore lint/correctness/useExhaustiveDependencies: retry intentionally repeats the same request.
  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError(''); setImageFailed(false);
    fetch(`/api/artifact/analytics/${encodeURIComponent(artifact.id)}`, { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error(response.status === 404 ? 'This chart is unavailable or its app is disconnected.' : response.status === 401 ? 'Sign in to Ava to open this chart.' : 'The chart could not be loaded.');
        return response.json() as Promise<AnalyticsArtifactPayload>;
      }).then(setData).catch((reason: Error) => {
        if (reason.name !== 'AbortError') setError(reason.message);
      });
    return () => controller.abort();
  }, [artifact.id, attempt]);
  const app = appById(artifact.connector_id);
  if (error) return <div role="alert" className="analysis-notice">{error} <button type="button" onClick={() => setAttempt(value => value + 1)}>Try again</button></div>;
  if (!data) return <div className="analysis-loading" role="status">Loading chart…</div>;
  if (data.schema_version === 'ava-artifact/2') return <SupersetChart data={data} artifact={artifact} onOpen={onOpen} />;
  const result = data.result;
  const chartImage = data.visualization?.format === 'svg' && data.visualization.result_id === artifact.result_id && (!compact || result.rows.length <= 6);
  const chart = chartImage && !imageFailed
    ? <img className="analysis-chart-image" src={`/api/artifact/analytics/${encodeURIComponent(artifact.id)}/chart`} alt={`${result.title}. ${result.rows.slice(0, 6).map(row => `${row.label}: ${format(row.value)} ${result.unit}`).join('; ')}`} onError={() => setImageFailed(true)} />
    : artifact.chart_type === 'table' ? <div className="analysis-table-scroll"><table><caption>{result.unit} by {result.filters.geography_level}</caption><thead><tr><th>Geography</th><th>{result.unit}</th></tr></thead><tbody>
      {(compact ? result.rows.slice(0, 6) : result.rows).map(row => <tr key={`${row.state}-${row.puma}-${row.label}`}><th scope="row">{row.label}</th><td>{format(row.value)}</td></tr>)}
    </tbody></table>{compact && result.rows.length > 6 && <p className="analysis-note">{result.rows.length} areas. Open to see all.</p>}</div>
    : <RecordedChart result={result} compact={compact} />;
  const appUrl = data.app_url?.startsWith(`/apps/${encodeURIComponent(artifact.connector_id)}/`) ? data.app_url : undefined;
  return <section className={`analysis-artifact${compact ? ' analysis-preview' : ''}`} style={{ '--analysis-accent': appAccent(artifact.connector_id) } as CSSProperties} aria-label={compact ? 'Chart preview' : 'Recorded chart'}>
    {compact ? <button type="button" className="analysis-preview-open" onClick={onOpen} aria-label={`Open chart: ${artifact.title}`}>
      <span className="analysis-preview-title">{artifact.title}</span>
      {chart}
      <span className="analysis-preview-hint">View chart ↗</span>
    </button> : chart}
    <p className="analysis-note">{result.filters.release} · {result.unit} by {result.filters.geography_level}</p>
    {result.metric_id === 'census.weighted_count' && <p className="analysis-note">Weighted PUMS estimates; not published Census summary totals.</p>}
    {!compact && <>
      {result.rows.some(row => row.moe_90 != null) && <p className="analysis-note">Whiskers show the 90% margin of error where available.</p>}
      {result.rows.some(row => row.moe_90 == null) && <p className="analysis-note">Margin of error unavailable for {result.rows.every(row => row.moe_90 == null) ? 'these estimates' : 'some estimates'}.</p>}
    </>}
    <AnalysisSources result={result} />
    {!compact && appUrl && <a className="analysis-open-app" href={appUrl} target="_blank" rel="noreferrer">Open in {app?.label ?? 'Analytics'} ↗</a>}
  </section>;
}
