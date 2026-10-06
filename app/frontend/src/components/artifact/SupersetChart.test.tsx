import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SupersetChart } from './AnalyticsArtifact';
import type { AnalyticsArtifactReference, SupersetArtifactPayload } from '../../lib/types';

vi.mock('../AppFrame', () => ({ AppFrame: ({ path }: { path: string }) => <iframe title="Native chart" src={path} /> }));

describe('native Superset presentation', () => {
  it.each(['world_map', 'country_map', 'pie', 'heatmap_v2', 'echarts_timeseries_line', 'bubble_v2', 'custom_plugin'])('keeps %s in its native frame with citations', chartType => {
    const artifact = { id: 'test', title: 'Population', connector_id: 'analytics', chart_type: chartType, mode: 'live' } as AnalyticsArtifactReference;
    const data = { chart: { citations: [{ title: 'Source', url: 'https://example.com/data' }, { title: 'Unsafe', url: 'javascript:alert(1)' }] }, visualization: { format: 'superset', path: '/superset/superset/explore/?slice_id=7&standalone=1' } } as SupersetArtifactPayload;
    const html = renderToStaticMarkup(<SupersetChart data={data} artifact={artifact} onOpen={() => {}} />);
    expect(html).toContain('<iframe');
    expect(html).toContain('slice_id=7');
    expect(html).toContain('https://example.com/data');
    expect(html).toContain('Live chart');
    expect(html).toContain('Open chart: Population');
    expect(html).not.toContain('analysis-chart-row');
    expect(html).not.toContain('javascript:');
    expect(html).not.toContain('tablist');
  });

  it('leaves sources to a view that already shows them', () => {
    const artifact = { id: 'test', title: 'Population', connector_id: 'analytics', chart_type: 'custom_map', mode: 'live' } as AnalyticsArtifactReference;
    const chart = { citations: [{ title: 'Source', url: 'https://example.com/data' }] };
    const visualization = { format: 'superset', path: '/superset/superset/explore/?slice_id=7&standalone=1' };
    const shown = renderToStaticMarkup(<SupersetChart data={{ chart: { ...chart, sources_in_view: true }, visualization } as SupersetArtifactPayload} artifact={artifact} />);
    expect(shown).not.toContain('Chart sources');
    expect(shown).not.toContain('https://example.com/data');
    expect(shown).toContain('Live chart');
    // An older payload, or a view without its own notes, keeps the list here.
    const listed = renderToStaticMarkup(<SupersetChart data={{ chart, visualization } as SupersetArtifactPayload} artifact={artifact} />);
    expect(listed).toContain('https://example.com/data');
  });
});
