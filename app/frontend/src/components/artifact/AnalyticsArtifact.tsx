import { useCallback, useEffect, useState, type CSSProperties } from 'react';
import type { AnalyticsArtifactPayload, AnalyticsArtifactReference, RecordedAnalyticsArtifactPayload, SupersetArtifactPayload, AppArtifactPayload } from '../../lib/types';
import { appAccent, appById } from '../../lib/appColor';
import { analysisLabels } from '../../lib/artifactCompat';
import { AppFrame } from '../AppFrame';
import './analytics-artifact.css';

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const preciseNumber = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });
const format = (value: number | null, precise = false) => value == null ? 'Unavailable' : (precise ? preciseNumber : number).format(value);
const sourceUrl = (url: string) => /^https:\/\//i.test(url) ? url : undefined;

/** Old saved results keep a faithful preview when their connector predates chart delivery. */
export function RecordedChart({ result, compact = false }: { result: RecordedAnalyticsArtifactPayload['result']; compact?: boolean }) {
  const labels = analysisLabels(result);
  const shown = compact ? result.rows.slice(0, 6) : result.rows;
  const minimum = Math.min(0, ...result.rows.map(row => (row.value ?? 0) - (row.moe_90 ?? 0)));
  const maximum = Math.max(1, ...result.rows.map(row => (row.value ?? 0) + (row.moe_90 ?? 0)));
  const position = (value: number) => (value - minimum) / (maximum - minimum) * 490;
  return <div className="analysis-chart" role="img" aria-label={`${result.unit} by ${labels.dimension}. ${shown.map(row => `${row.label}: ${format(row.value, !labels.showUncertainty)}${labels.showUncertainty ? `; ${row.moe_90 == null ? 'margin of error unavailable' : `90% margin of error ${format(row.moe_90)}`}` : ''}`).join('. ')}`}>
    {shown.map(row => <div className="analysis-chart-row" key={`${row.state}-${row.puma}-${row.label}`}>
      <div className="analysis-row-label"><span>{row.label}</span><strong>{format(row.value, !labels.showUncertainty)}</strong></div>
      <svg viewBox="0 0 500 28" preserveAspectRatio="none" aria-hidden="true">
        <rect x={Math.min(position(0), position(row.value ?? 0))} y="5" width={Math.abs(position(row.value ?? 0) - position(0))} height="18" rx="3" fill="var(--analysis-accent)" />
        {row.moe_90 != null && row.value != null && <g stroke="currentColor" strokeWidth="2">
          <line x1={position(Math.max(minimum, row.value - row.moe_90))} x2={position(row.value + row.moe_90)} y1="14" y2="14" />
          <line x1={position(Math.max(minimum, row.value - row.moe_90))} x2={position(Math.max(minimum, row.value - row.moe_90))} y1="8" y2="20" />
          <line x1={position(row.value + row.moe_90)} x2={position(row.value + row.moe_90)} y1="8" y2="20" />
        </g>}
      </svg>
    </div>)}
    {shown.length < result.rows.length && <p className="analysis-note">{result.rows.length} {labels.rowNoun}. Open the chart to see all.</p>}
  </div>;
}

export function AnalysisSources({ result }: { result: RecordedAnalyticsArtifactPayload['result'] }) {
  const citations = [...result.citations, ...result.sources.map(source => ({
    title: source.title || source.url.split('/').pop() || 'Source dataset', url: source.url,
  }))].filter((source, index, all) => sourceUrl(source.url) && all.findIndex(item => item.url === source.url) === index);
  if (!citations.length) return <p className="analysis-note">Source citations unavailable.</p>;
  return <div className="analysis-sources" aria-label="Chart sources"><span>Sources</span>
    {citations.slice(0, 3).map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>)}
    {citations.length > 3 && <details><summary>{citations.length - 3} more sources</summary>
      {citations.slice(3).map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>)}
    </details>}
  </div>;
}

export function SupersetChart({ data, artifact, onOpen }: {
  data: SupersetArtifactPayload | AppArtifactPayload; artifact: AnalyticsArtifactReference; onOpen?: () => void;
}) {
  // A view that shows its own sources owns them: listing them here as well gives
  // one fact two homes, and only the app's copy follows what the chart now shows.
  // The saved chart may say so; the frame says so as it renders, which also
  // covers a chart saved before its connector learned to.
  const [viewListsSources, setViewListsSources] = useState(false);
  const onSourcesInView = useCallback(() => setViewListsSources(true), []);
  const citations = data.chart.sources_in_view || viewListsSources ? [] : data.chart.citations.filter(source => sourceUrl(source.url));
  return <section className={`analysis-artifact analysis-native${onOpen ? ' analysis-preview' : ''}`} style={{ '--analysis-accent': appAccent(artifact.connector_id) } as CSSProperties} aria-label={onOpen ? 'Chart preview' : data.schema_version === 'ava-artifact/2' ? 'Superset chart' : 'Live chart'}>
    {onOpen && <button type="button" className="analysis-preview-open" onClick={onOpen} aria-label={`Open chart: ${artifact.title}`}>
      <span className="analysis-preview-title">{artifact.title}</span><span className="analysis-preview-hint">View chart ↗</span>
    </button>}
    <div className="analysis-native-frame">
      <AppFrame id={artifact.connector_id} label={artifact.title} path={data.visualization.path} onSourcesInView={onSourcesInView} />
    </div>
    <p className="analysis-note">Live chart from {appById(artifact.connector_id)?.label ?? 'Analytics'}</p>
    {citations.length > 0 && <div className="analysis-sources" aria-label="Chart sources"><span>Sources</span>
      {citations.map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>)}
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
  const compact = !!onOpen;
  // biome-ignore lint/correctness/useExhaustiveDependencies: retry intentionally repeats the same request.
  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError('');
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
  if (data.mode === 'live') return <SupersetChart data={data} artifact={artifact} onOpen={onOpen} />;
  const result = data.result;
  const labels = analysisLabels(result);
  // Render recorded values with Ava tokens; immutable export images cannot follow
  // a live theme change. Native Superset artifacts keep their original chart type.
  const chart = artifact.chart_type === 'table' ? <div className="analysis-table-scroll"><table><caption>{result.unit} by {labels.dimension}</caption><thead><tr><th>{labels.header}</th><th>{result.unit}</th></tr></thead><tbody>
      {(compact ? result.rows.slice(0, 6) : result.rows).map(row => <tr key={`${row.state}-${row.puma}-${row.label}`}><th scope="row">{row.label}</th><td>{format(row.value, !labels.showUncertainty)}</td></tr>)}
    </tbody></table>{compact && result.rows.length > 6 && <p className="analysis-note">{result.rows.length} {labels.rowNoun}. Open to see all.</p>}</div>
    : <RecordedChart result={result} compact={compact} />;
  const appUrl = data.app_url?.startsWith(`/apps/${encodeURIComponent(artifact.connector_id)}/`) ? data.app_url : undefined;
  return <section className={`analysis-artifact${compact ? ' analysis-preview' : ''}`} style={{ '--analysis-accent': appAccent(artifact.connector_id) } as CSSProperties} aria-label={compact ? 'Chart preview' : 'Recorded chart'}>
    {compact ? <button type="button" className="analysis-preview-open" onClick={onOpen} aria-label={`Open chart: ${artifact.title}`}>
      <span className="analysis-preview-title">{artifact.title}</span>
      {chart}
      <span className="analysis-preview-hint">View chart ↗</span>
    </button> : chart}
    <p className="analysis-note">{labels.scope} · {result.unit} by {labels.dimension}</p>
    {labels.notes.map(note => <p key={note} className="analysis-note">{note}</p>)}
    {!compact && labels.showUncertainty && <>
      {result.rows.some(row => row.moe_90 != null) && <p className="analysis-note">Whiskers show the 90% margin of error where available.</p>}
      {result.rows.some(row => row.moe_90 == null) && <p className="analysis-note">Margin of error unavailable for {result.rows.every(row => row.moe_90 == null) ? 'these estimates' : 'some estimates'}.</p>}
    </>}
    <AnalysisSources result={result} />
    {!compact && appUrl && <a className="analysis-open-app" href={appUrl} target="_blank" rel="noreferrer">Open in {app?.label ?? 'Analytics'} ↗</a>}
  </section>;
}
