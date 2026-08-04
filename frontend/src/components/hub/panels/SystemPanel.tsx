import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/layout';
import { ResourceError } from '../ui/ResourceState';
import { useAction, useResource } from '../hooks';
import { hub } from '../hubApi';
import { Badge } from '../ui/Badge';
import { HubMessage } from '../ui/HubMessage';
import { Tile } from '../ui/Tile';

// System — about, self-editing governance, data retention, optional features,
// learning status.
// Human label for a retention window in days (0 == forever).
const RETENTION_LABELS: Record<number, string> = {
  0: 'Forever', 30: '1 month', 90: '3 months', 183: '6 months', 365: '1 year', 730: '2 years',
};
function retentionLabel(days: number): string {
  return RETENTION_LABELS[days] || (days > 0 ? `${days} days` : 'Forever');
}

// Optional-feature capability key → a typed glyph, so each feature row carries
// an identity like the connector/memory rows. Unknown keys fall back to sliders.
const FEATURE_ICONS: Record<string, string> = {
  web_search: 'search', voice: 'mic', memory: 'db', code: 'code',
};
const featureIcon = (key: string) => FEATURE_ICONS[key] || 'sliders';

// Self-editing modes, safest → most permissive, each with a glyph that reads its
// posture at a glance (locked / selective / hands-off).
const APPROVALS: { id: string; title: string; sub: string; icon: string }[] = [
  { id: 'all', title: 'All changes need approval', icon: 'lock', sub: 'Safest. Every edit Ava makes to its own code waits for you.' },
  { id: 'policy', title: 'Only sensitive paths', icon: 'sliders', sub: 'Auth/config/deploy edits are gated; routine edits auto-commit to git.' },
  { id: 'none', title: 'Auto-apply', icon: 'bot', sub: 'Trusted box — all non-secret edits commit automatically.' },
];

export function SystemPanel({ onRestart }: { onRestart: () => void }) {
  const sysRes = useResource(() => hub.system());
  const { data: sys, reload, setData: setSys } = sysRes;
  const { busy, message, setMessage, run } = useAction();

  const setApproval = (mode: string) => run(async () => {
    const r = await hub.setApproval(mode);
    if (r.error) return r.error;
    setSys((s) => (s ? { ...s, code_approval: mode } : s));
    // Approval now applies live (no restart); only nudge a restart if the
    // backend still asks for one — and only claim "in effect now" when it is.
    if (r.restart_required) onRestart();
    else setMessage({ text: 'Saved — in effect now.', ok: true });
  });

  const saveFeatures = (patch: Record<string, boolean>) => run(async () => {
    const r = await hub.save({ features: patch });
    if (r.error) return r.error;
    reload(); onRestart();
  });

  const setRetention = (days: number) => run(async () => {
    const r = await hub.setRetention(days);
    if (r.error) return r.error;
    setSys((s) => (s ? { ...s, retention_days: days } : s)); onRestart();
  });

  const overrides = Object.entries(sys?.env_overrides || {});

  return (
    <>
      <ResourceError r={sysRes} label="your system settings" />
      {/* A broken ava.yaml is not a load failure, so ResourceError above never
          fires for it — the request succeeds and every value on this page is
          silently a DEFAULT rather than the owner's setting, while each save is
          refused with a 409. hub/system.py has returned config_error since it was
          written; nothing rendered it, so the panel invited people to toggle
          things that could not persist. */}
      {sys?.config_error && (
        <div className="hub-msg err" style={{ marginBottom: 14 }}>
          <b>Your config file does not parse, so nothing on this page can be saved.</b>
          <br />
          Every value below is Ava's built-in default, not your setting. Fix
          {sys.config_path ? <> <code>{sys.config_path}</code></> : ' ava.yaml'} and
          restart, then reload this page.
          <br />
          <small style={{ opacity: 0.85 }}>{sys.config_error}</small>
        </div>
      )}
      <Panel title="About" subtitle="Your instance">
        {sys ? (
          <dl className="hub-kv">
            <dt>Name</dt><dd>{sys.brand}</dd>
            <dt>Version</dt><dd>{sys.version}</dd>
            <dt>Runtime</dt><dd>{sys.docker ? 'Docker container' : 'Native process'}</dd>
          </dl>
        ) : <EmptyState text="Loading…" />}
        {overrides.length > 0 && (
          <div className="hub-note" style={{ marginTop: 14 }}>
            <b>Environment overrides active:</b> {overrides.map(([k, v]) => (
              <span key={k} style={{ marginRight: 8 }}><code>{v}</code> ({k.replace(/_/g, ' ')})</span>
            ))}
            — these env vars shadow ava.yaml, so edits to the matching settings
            below apply live but revert to the env value on restart. Unset the
            variable in your launch command/unit to make yaml wins stick.
          </div>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Self-editing governance" subtitle="How Ava's code-change agent applies edits to its own repo (secrets, models/ and .git are always denied).">
        <div className="sys-gov">
          {APPROVALS.map((a) => {
            const on = sys?.code_approval === a.id;
            return (
              <button type="button" key={a.id} className={'sys-gov-opt' + (on ? ' sel' : '')}
                disabled={busy} aria-pressed={on} onClick={() => setApproval(a.id)}>
                <Tile icon={a.icon} tone={on ? 'accent' : 'muted'} size={34} />
                <span className="sys-gov-txt"><b>{a.title}</b><small>{a.sub}</small></span>
                {on ? <span className="sys-gov-check"><Icon name="check" />Current</span> : <span className="sys-gov-pick">Use</span>}
              </button>
            );
          })}
        </div>
      </Panel>

      <div className="hub-section" />
      <Panel title="Data retention" subtitle="How long Ava keeps performance metrics and hardware history. Older data is pruned automatically; the dashboard's time-range filters can only reach back as far as this.">
        {sys ? (
          <>
            <div className="hub-field" style={{ maxWidth: 340 }}>
              <label>Keep data for</label>
              <select className="hub-select" value={sys.retention_days} disabled={busy}
                onChange={(e) => setRetention(Number(e.target.value))}>
                {(sys.retention_choices || [30, 90, 183, 365, 730, 0]).map((d) => (
                  <option key={d} value={d}>{retentionLabel(d)}{d === 183 ? ' (default)' : ''}</option>
                ))}
              </select>
            </div>
            <div className="hub-note" style={{ marginTop: 12 }}>
              Currently keeping <b>{retentionLabel(sys.retention_days)}</b> of history.
              Changing this takes effect after a restart and prunes anything older on the next cleanup.
            </div>
          </>
        ) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      {/* Not "all off by default": features.REGISTRY ships learning and memory
          ON. Saying otherwise made a fresh install look misconfigured. */}
      <Panel title="Optional features" subtitle="Ava's defaults — turn on what you need, turn off what you don't.">
        {sys ? (sys.features || []).map((f) => (
          // Rendered straight from the backend capability registry
          // (ava_bridge/features.py) — a newly registered capability gets its
          // row here with no UI change. Per-key extras (like the voice
          // enrollment badge) hang off the key below.
          <label className={'sys-feat' + (f.enabled ? ' on' : '')} key={f.key}>
            <Tile icon={featureIcon(f.key)} tone={f.enabled ? 'accent' : 'muted'} size={32} />
            <span className="sys-feat-main">
              <span className="sys-feat-title">{f.label}</span>
              <span className="sys-feat-sub">
                {f.sub}
                {f.key === 'voice' && f.enabled && (
                  <>{' '}{sys.voiceprint
                    ? <Badge tone="ok">voiceprint enrolled</Badge>
                    : <Badge tone="warn">no voiceprint — enroll on the Voice tab</Badge>}</>
                )}
              </span>
            </span>
            <input type="checkbox" className="sys-feat-check" checked={f.enabled} disabled={busy}
              onChange={(e) => saveFeatures({ [f.key]: e.target.checked })} />
          </label>
        )) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="Learning" subtitle="Periodic local-first self-analysis that parks improvement proposals for your approval.">
        {sys && (
          <dl className="hub-kv">
            <dt>Status</dt><dd>{sys.learning_enabled ? <Badge tone="ok">on · every {sys.learning_interval_h}h</Badge> : <Badge tone="muted">off</Badge>}</dd>
            <dt>Proposals</dt><dd><span style={{ color: 'var(--muted)' }}>Review &amp; run cycles on the Operations → Control Center page.</span></dd>
          </dl>
        )}
      </Panel>
      <div className="hub-section" />
      <Panel title="Walkthrough" subtitle="The short guided tour shown on a fresh install.">
        <p className="hub-note">
          Replays the walkthrough on every page. Useful after connecting new apps,
          or if you skipped it the first time.
        </p>
        <button type="button" className="hub-btn ghost" disabled={busy}
          onClick={() => run(async () => {
            const r = await hub.tourReset();
            if (!r.ok) return 'could not reset the walkthrough';
            window.dispatchEvent(new Event('ava:tour-reset'));
            setMessage({ text: 'Walkthrough reset — it starts again on this page.', ok: true });
          })}>
          Show the walkthrough again
        </button>
      </Panel>
      <HubMessage message={message} />
    </>
  );
}
