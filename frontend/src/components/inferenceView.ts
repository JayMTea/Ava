// Should the "Ava cannot answer" banner show, and what should it say?
//
// A pure function for the same reason provisionView.ts is one: the SPA has no
// component-render harness, so any decision left inside a component is a
// decision nobody can test. The rules here are small and the failure modes are
// not obvious — "we have not checked yet" must not look like "it is broken" —
// so they belong somewhere a test can reach.

export interface InferenceHealth {
  ok: boolean;
  code?: string;
  detail?: string;
  model?: string;
  engine?: string;
}

export interface BannerView {
  show: boolean;
  text: string;
  /** Passed to fixForCode(); the resolver owns the destination. */
  code?: string;
  /** `alert` only for a genuine fault — a warming engine is a status, not an
   *  emergency, and a screen reader should not interrupt for it. */
  role: 'alert' | 'status';
}

const HIDDEN: BannerView = { show: false, text: '', role: 'status' };

/**
 * @param health  the last answer from GET /api/hub/agent/inference, or null
 *                when nothing has come back yet.
 */
export function bannerView(health: InferenceHealth | null): BannerView {
  // Not checked yet, or the check itself failed. Say NOTHING: a banner that
  // appears while the app is still loading would greet every single visit with
  // an error, and "we could not reach our own API" is not a diagnosis the owner
  // can act on. Fails closed, which is the right direction for an interruption.
  if (!health) return HIDDEN;
  if (health.ok) return HIDDEN;

  // The backend writes owner-facing copy here on purpose: it is the only side
  // that knows WHICH engine and WHICH model, and a frontend template would
  // either restate that badly or drop it.
  const text = (health.detail || '').trim()
    || 'Ava cannot answer right now.';

  // A service that has not come up yet is the expected state on a gpu install,
  // where vLLM can spend 10-30 minutes pulling weights before it answers. That
  // is not a fault and must not read as one.
  const warming = health.code === 'inference_down';
  return { show: true, text, code: health.code, role: warming ? 'status' : 'alert' };
}
