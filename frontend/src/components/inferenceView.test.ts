// The banner's rules. Pure, because the SPA has no component-render harness —
// same reason provisionView.test.ts exists.
import { describe, expect, it } from 'vitest';

import { bannerView } from './inferenceView';

describe('bannerView', () => {
  it('says nothing before the first answer', () => {
    // The load-bearing case. A banner that appears while the app is still
    // fetching would greet EVERY visit with an error before the check returns.
    expect(bannerView(null).show).toBe(false);
  });

  it('says nothing when Ava can answer', () => {
    expect(bannerView({ ok: true }).show).toBe(false);
  });

  it('shows the backend\'s own diagnosis rather than a template', () => {
    const v = bannerView({
      ok: false, code: 'model_unknown',
      detail: "ollama is running but does not have 'llama3.2'.",
    });
    expect(v.show).toBe(true);
    expect(v.text).toContain('llama3.2');
    expect(v.code).toBe('model_unknown');
  });

  it('treats a missing model as a fault and a warming engine as a status', () => {
    // A gpu install can spend half an hour pulling weights before vLLM answers.
    // Interrupting a screen reader for that would be wrong; a missing model is
    // a real fault that will not fix itself.
    expect(bannerView({ ok: false, code: 'model_unknown', detail: 'x' }).role).toBe('alert');
    expect(bannerView({ ok: false, code: 'inference_down', detail: 'x' }).role).toBe('status');
  });

  it('still says something useful when the backend sent no detail', () => {
    const v = bannerView({ ok: false, code: 'model_unknown' });
    expect(v.text.length).toBeGreaterThan(0);
  });

  it('passes the code through untouched for fixForCode to resolve', () => {
    // The banner owns no mapping table: fixes.ts resolves the destination from
    // the code pattern, so a future code routes correctly with no change here.
    expect(bannerView({ ok: false, code: 'whatever_down', detail: 'x' }).code)
      .toBe('whatever_down');
  });
});
