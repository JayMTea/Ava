import type { ReactNode } from 'react';
import { Icon } from '../../../lib/icons';

/** The shape `useResource` returns. Taken WHOLE, deliberately — see below. */
export type Resource<T> = {
  data: T | null;
  error: string;
  loading: boolean;
  reload: () => void;
};

/**
 * Renders one `useResource` result: its error, its pending state, or its data.
 *
 * It takes the whole hook result rather than destructured fields, and that is
 * the entire point. 17 of 21 call sites destructured only `{ data }`, so a
 * backend failure rendered Setup as tabs of permanent "Detecting hardware…"
 * ellipses — every panel looked like it was still loading, forever, with the
 * real error sitting unread in a variable nobody had named. Passing `r` means a
 * panel cannot silently drop the error: there is no field to omit.
 *
 * `empty` keeps each panel's own skeleton/placeholder, so this adds an error
 * path without flattening the loading states that were already good.
 */
/**
 * The error half of `ResourceState`, on its own.
 *
 * For a panel that renders SEVERAL sections from ONE resource, wrapping each
 * section would show the same failure three times. Surface it once at the top
 * and let the existing per-section placeholders stand.
 *
 * `tests/test_hub_uniformity.py` accepts either shape — what it rejects is a
 * `useResource` result whose `error` reaches neither.
 */
// Takes only what it needs — `error` + `reload` — so an array of DIFFERENT
// Resource<T> can be searched for the first failure without TypeScript
// unifying their data types.
export function ResourceError({ r, label }: {
  r: { error: string; reload: () => void };
  label: string;
}) {
  if (!r.error) return null;
  return (
    <div className="hub-msg err" role="alert" style={{ marginBottom: 12 }}>
      <div>Couldn’t load {label}. {r.error}</div>
      <button type="button" className="hub-btn ghost sm" style={{ marginTop: 8 }} onClick={r.reload}>
        <Icon name="refresh" />Try again
      </button>
    </div>
  );
}

export function ResourceState<T>({ r, label, empty, children }: {
  r: Resource<T>;
  /** What failed, in the owner's words: "your hardware", "the agent status". */
  label: string;
  /** Rendered while loading with nothing to show yet. Defaults to a quiet line. */
  empty?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  if (r.error) {
    return (
      <div className="hub-msg err" role="alert">
        <div>Couldn’t load {label}. {r.error}</div>
        <button type="button" className="hub-btn ghost sm" style={{ marginTop: 8 }} onClick={r.reload}>
          <Icon name="refresh" />Try again
        </button>
      </div>
    );
  }
  if (r.data == null) {
    return <>{empty ?? <div className="hub-note">Loading {label}…</div>}</>;
  }
  return <>{children(r.data)}</>;
}
