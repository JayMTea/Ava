import type { RecordedAnalyticsArtifactPayload } from './types';

/** Keep the original analytics captions on saved results, outside the renderer. */
export function analysisLabels(result: RecordedAnalyticsArtifactPayload['result']) {
  const legacy = !!result.filters && !result.dimension_label;
  return {
    dimension: result.dimension_label || result.filters?.geography_level || 'Category',
    header: result.dimension_label || (legacy ? 'Geography' : 'Category'),
    scope: result.scope_label || result.filters?.release || '',
    rowNoun: legacy ? 'areas' : 'rows',
    showUncertainty: legacy,
    notes: result.metric_id === 'census.weighted_count'
      ? ['Weighted PUMS estimates; not published Census summary totals.', ...result.limitations]
      : result.limitations,
  };
}
