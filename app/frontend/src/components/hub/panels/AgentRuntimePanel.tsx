import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../ui/layout';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import { ResourceError } from '../ui/ResourceState';
import { HubMessage } from '../ui/HubMessage';
import { Badge } from '../ui/Badge';
import { StatRow } from '../ui/StatRow';
import { GatewayCard } from './GatewayCard';
import { DriftBoard, ProvisionRun } from './ProvisionRun';
import { openDriftView, startProvision, useProvisionState } from '../../../hooks/useProvisionState';

// Setup -> Agent -> Runtime: is the agent actually live, what has drifted, and
// the one button that applies it. This is the DEFAULT Agent sub-tab, and it is
// now the ONE place drift is computed — opening it asks the bridge, and nothing
// else does. Nothing runs on a clock: there is no banner and no tab pill to keep
// current, so a drift read happens when the owner comes here to look, and when
// they press Re-check.

export function AgentRuntimePanel() {
  const stRes = useResource(() => hub.agentStatus());
  const { data: st, reload: load } = stRes;
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState('');
  const { state: prov, job, error: provErr, jobError, loading: checking } = useProvisionState({ refreshOnMount: true });

  useEffect(() => {
    const refresh = () => { void load(); };
    window.addEventListener('ava:agent-provisioned', refresh);
    return () => window.removeEventListener('ava:agent-provisioned', refresh);
  }, [load]);

  // The run is server-side now, so it survives a page reload and a second tab —
  // and the button stops being the only feedback. It used to block for up to ten
  // minutes with nothing but a disabled label, which cannot distinguish slow from
  // hung, and install.sh has a documented hang mode.
  const provision = useCallback(async () => {
    setBusy(true); setDetail('');
    const r = await startProvision('all');
    if (!r.ok && r.error) setDetail(r.error);
    setBusy(false);
    load();
  }, [load]);

  // The owner's manual refresh, and the only one there is. With the poll gone,
  // drift created elsewhere — `ava agent provision`, a SKILL.md edited on disk,
  // another tab's Apply — shows up when this runs, not before.
  const recheck = useCallback(async () => {
    setBusy(true); setDetail('');
    await Promise.all([openDriftView(true), load()]);
    setBusy(false);
  }, [load]);

  const running = job?.status === 'running';
  const pending = prov?.pending ?? 0;
  const blocked = st?.config_error || st?.gate_error;
  const canApply = pending > 0 && !!st?.enabled && !!prov?.enabled && !st?.config_error
    && !provErr && !jobError && !stRes.error;
  const hasLocalCli = st != null && st.location !== 'remote' && 'cli' in st;

  return (
    <>
    <ResourceError r={stRes} label="the agent status" />
    <Panel
      title="Agent runtime"
      // The RUNTIME says what it is and what it gets you. Hardcoding "NemoClaw"
      // told a `remote` or `direct` install about a runtime it is not running,
      // and made a fork edit this file to stop being told about somebody
      // else's. The fallback keeps the panel sane if status is not in yet.
      subtitle={st?.blurb
        || 'Gives Ava a sandbox, tools, egress policies, and persistent memory. Without it, chat still works (tool-less).'}
      right={st ? (
        stRes.error ? <Badge tone="warn">status unavailable</Badge>
          : st.enabled === false ? <Badge tone="muted">disabled</Badge>
            : blocked ? <Badge tone="warn">needs attention</Badge>
              : st.available && st.tools ? <Badge tone="ok">ready</Badge>
                : st.available ? <Badge tone="muted">chat only</Badge>
                  : <Badge tone="warn">not ready</Badge>
      ) : null}
    >
      {st ? (
        <div className="stat-rows">
          <StatRow label="Runtime"
            value={`${st.runtime}${st.required ? ' · required' : ''}`}
            tone={st.available ? 'ok' : st.enabled === false ? 'muted' : 'warn'} />
          {/* CLI and Sandbox describe the machine the runtime runs ON. With a
              remote runtime that machine is not this container, so showing
              "not installed / none" reported a working remote agent as broken
              and pointed the owner at the wrong host. */}
          {hasLocalCli && (
            <>
              <StatRow label="CLI"
                value={st.cli || 'not installed'}
                tone={st.cli ? 'ok' : 'warn'} />
              <StatRow label="Sandbox"
                value={st.sandbox ? `${st.sandbox}${st.sandbox_exists ? '' : ' · missing'}` : 'none'}
                tone={st.sandbox_exists ? 'ok' : 'warn'} />
            </>
          )}
          {st.location === 'remote' && (
            <StatRow label="Agent host"
              value={st.url ? `${st.url}${st.available ? '' : ' · not answering'}` : 'remote'}
              tone={st.available ? 'ok' : 'warn'} />
          )}
          <StatRow label="Tools"
            value={st.tools ? 'available' : st.enabled === false ? 'disabled' : 'unavailable'}
            tone={st.tools ? 'ok' : st.enabled === false ? 'muted' : 'warn'} />
        </div>
      ) : !stRes.error ? <EmptyState text="Loading agent status…" /> : null}

      {blocked && <HubMessage message={{ ok: false, text: blocked }} />}

      {st && st.enabled === false ? (
        <div className="hub-note" style={{ marginTop: 14 }}>
          The agent is <b>turned off for this instance</b>{' '}
          {st.enabled_env_override
            ? <>— forced by the <code>{st.enabled_env_override}</code> env var in this
              instance's launch command, which shadows <code>ava.yaml</code>. Remove it
              and restart to get tools, memory, and skills.</>
            : <>(<code>agent.enabled: false</code> in ava.yaml) — so chat runs tool-less
              by design, and the CLI/sandbox rows above are just what's present on the
              host. Enable it in <a href="#hub/system">Setup → System</a> and restart to get tools, memory, and
              skills.</>}
        </div>
      ) : st && st.location === 'remote' && !st.available ? (
        <div className="hub-note" style={{ marginTop: 14 }}>
          The agent runtime is <b>remote</b> (<code>{st.url}</code>) and it isn't
          answering. Nothing to install here — this container is not the agent host.
          Check that the agent service is up and reachable from the bridge
          {st.error ? <> (<code>{String(st.error).slice(0, 120)}</code>)</> : null},
          then click Re-check below.
        </div>
      ) : st && hasLocalCli && !st.cli && (
        <div className="hub-note" style={{ marginTop: 14 }}>
          The {st.display_name || 'agent runtime'} CLI isn&rsquo;t installed.
          {st.install_hint ? ` ${st.install_hint}` : ''} Then click Re-check below.
        </div>
      )}

      <div className="agent-apply-section" aria-busy={checking}>
        <h4>Applied configuration</h4>
        <p className="agent-setup-intro">Save your changes in the sections above, then apply them here.</p>
        {checking && <p className="hub-note" role="status">Checking saved changes…</p>}
        {!checking && !prov && !provErr && <p className="hub-note">Configuration has not been checked yet.</p>}
        {prov?.enabled === false && <p className="hub-note">Applying configuration is unavailable for this runtime.</p>}
        <DriftBoard state={prov} />

      {/* Apply starts a run; re-check recovers status without starting work. */}
        <div className="hub-btn-row">
          {canApply && !running && <button type="button" className="hub-btn"
                  onClick={provision} disabled={busy || checking || stRes.loading}>
            <Icon name="check" />
            {busy ? 'Applying…' : `Apply ${pending} change${pending === 1 ? '' : 's'}`}
          </button>}
          <button type="button" className="hub-btn ghost"
                  onClick={recheck} disabled={busy || checking || stRes.loading}>
            <Icon name="refresh" />
            {checking || stRes.loading ? 'Checking…' : 'Re-check agent'}
          </button>
        </div>
      <HubMessage message={detail || provErr || jobError
        ? { ok: false, text: detail || provErr || jobError } : null} />

      <ProvisionRun job={job} />
      </div>
    </Panel>
    <GatewayCard />
    </>
  );
}
