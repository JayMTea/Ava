import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { AnalysisSources, RecordedChart } from './AnalyticsArtifact';
import type { RecordedAnalyticsArtifactPayload } from '../../lib/types';

const result: RecordedAnalyticsArtifactPayload['result'] = {
  id: 'test', title: 'Test comparison', created_at: '2026-01-01', unit: 'people', row_count: 2,
  rows: [
    { state: '06', puma: '', label: 'California', value: 120, moe_90: null, sample_records: 4 },
    { state: '41', puma: '', label: 'Oregon', value: 30, moe_90: 5, sample_records: 2 },
  ],
  filters: { release: 'ACS 2024 / 1-year', record_type: 'person', geography_level: 'state', states: ['06', '41'] },
  method: 'Weighted count', metric_id: 'census.weighted_count', metric_version: '1', limitations: [],
  citations: [{ title: 'Documentation', url: 'https://example.com/docs' }],
  sources: [{ dataset_id: 'source', url: 'https://example.com/source.csv', source_sha256: 'a', silver_sha256: 'b', published_object: 'object' }],
};

describe('recorded chart presentation', () => {
  it('inherits live Ava colors for recorded marks and uncertainty', () => {
    const html = renderToStaticMarkup(<RecordedChart result={result} />);
    expect(html).toContain('fill="var(--analysis-accent)"');
    expect(html).toContain('stroke="currentColor"');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('background:white');
  });
  it('uses a common zero baseline and preserves missing uncertainty', () => {
    const html = renderToStaticMarkup(<RecordedChart result={result} />);
    expect(html).toContain('California');
    expect(html).toContain('Oregon');
    expect(html).toContain('width="490"');
    expect(html).toContain('width="122.5"');
    expect(html.match(/<g /g)).toHaveLength(1);
    expect(html).not.toContain('tablist');
  });
  it('shows documentation and original data citations directly, excluding unsafe links', () => {
    const html = renderToStaticMarkup(<AnalysisSources result={{ ...result, citations: [...result.citations, { title: 'Unsafe', url: 'javascript:alert(1)' }] }} />);
    expect(html).toContain('https://example.com/docs');
    expect(html).toContain('https://example.com/source.csv');
    expect(html).not.toContain('javascript:');
  });
  it('keeps every selected area in the expanded chart and labels a shortened preview', () => {
    const many = { ...result, rows: Array.from({ length: 22 }, (_, i) => ({ ...result.rows[0], label: `Area ${i}` })) };
    expect(renderToStaticMarkup(<RecordedChart result={many} />)).toContain('Area 21');
    expect(renderToStaticMarkup(<RecordedChart result={many} compact />)).not.toContain('Area 21');
    expect(renderToStaticMarkup(<RecordedChart result={many} compact />)).toContain('22 areas');
  });
});
