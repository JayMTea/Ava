import type { ReactNode } from 'react';
import { Icon } from '../../../lib/icons';
import { BRIDGE_OUTDATED } from '../../../lib/api';

/** The shape `useResource` returns. Taken WHOLE, deliberately — see below. */
export type Resource<T> = {
  data: T | null;
  error: string;
  /** The failure's machine-readable code, '' when it has none. */
  code?: string;
  loading: boolean;
  reload: () => void;
};

/**
 * The one failure that is NOT a fault in the thing being loaded.
 *
 * `lib/api.req` marks a 404 with no error body as `bridge_outdated`: the page
 * asked for a route the running bridge does not have. That happens because the
 * SPA is re-read from disk on every request while the Python process keeps the
 * routes it booted with, so an update reaches the browser before it reaches the
 * server. Rendering it as a red "Couldn't load X" sends the owner hunting for a
 * broken X. It is one restart away, so it gets the restart banner's own
 * language and colour (`.hub-restart`, as in HubView) — it is the same
 * instruction, arrived at from a different direction.
 *
 * "Try again" stays: after the restart it is exactly the right button.
 */
function OutdatedBridge({ label, reload }: { label: string; reload: () => void }) {
  return (
    <div className="hub-restart" role="status">
      <Icon name="refresh" />
      <span>
        Restart Ava to load {label} — this page is newer than the bridge that is
        running, so the route it asks for does not exist yet.{' '}
        <button type="button" className="hub-btn ghost sm" onClick={reload}>Try again</button>
      </span>
    </div>
  );
}

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
  r: { error: string; code?: string; reload: () => void };
  label: string;
}) {
  if (!r.error) return null;
  // No wrapper: `.hub-restart` carries its own bottom margin.
  if (r.code === BRIDGE_OUTDATED) return <OutdatedBridge label={label} reload={r.reload} />;
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
  if (r.error && r.code === BRIDGE_OUTDATED) {
    return <OutdatedBridge label={label} reload={r.reload} />;
  }
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
