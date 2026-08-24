import { useCallback, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/layout';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import { ResourceError } from '../ui/ResourceState';
import { Badge } from '../ui/Badge';
import { StatRow } from '../ui/StatRow';
import { GatewayCard } from './GatewayCard';
import { DriftBoard, ProvisionRun } from './ProvisionRun';
import { startProvision, useProvisionState } from '../../../hooks/useProvisionState';

// Setup -> Agent -> Runtime: is the agent actually live, what has drifted, and
// the one button that applies it. This is the DEFAULT Agent sub-tab, because
// `#hub/agent` already means "go apply my changes" to PendingChangesBar.

export function AgentRuntimePanel() {
  const stRes = useResource(() => hub.agentStatus());
  const { data: st, reload: load } = stRes;
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState('');
  const { state: prov, job } = useProvisionState();

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

  const running = job?.status === 'running';
  const pending = prov?.pending ?? 0;

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
        st.available ? <Badge tone="ok">active</Badge>
          : st.enabled === false ? <Badge tone="muted">disabled</Badge>
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
          {st.location !== 'remote' && (
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
      ) : <EmptyState text="Loading agent status…" />}

      {st && st.enabled === false ? (
        <div className="hub-note" style={{ marginTop: 14 }}>
          The agent is <b>turned off for this instance</b>{' '}
          {st.enabled_env_override
            ? <>— forced by the <code>{st.enabled_env_override}</code> env var in this
              instance's launch command, which shadows <code>ava.yaml</code>. Remove it
              and restart to get tools, memory, and skills.</>
            : <>(<code>agent.enabled: false</code> in ava.yaml) — so chat runs tool-less
              by design, and the CLI/sandbox rows above are just what's present on the
              host. Enable it in <b>System</b> and restart to get tools, memory, and
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
      ) : st && st.location !== 'remote' && !st.cli && (
        <div className="hub-note" style={{ marginTop: 14 }}>
          The {st.display_name || 'agent runtime'} CLI isn&rsquo;t installed.
          {st.install_hint ? ` ${st.install_hint}` : ''} Then click Re-check below.
        </div>
      )}

      <DriftBoard state={prov} />

      {/* While a run is live the button is REPLACED by the run view, not
          disabled: a disabled button reads as broken, a moving step reads as
          working. `Provision / re-check` was a slash-compound precisely because
          one button was doing two jobs. */}
      {!running && (
        <div className="hub-btn-row">
          <button type="button" className="hub-btn" onClick={provision} disabled={busy}>
            <Icon name={pending ? 'check' : 'refresh'} />
            {busy ? 'Applying…'
              : pending ? `Apply ${pending} change${pending === 1 ? '' : 's'}`
              : 'Re-check agent'}
          </button>
        </div>
      )}
      {detail && <div className="hub-msg" style={{ color: 'var(--muted)' }}>{detail}</div>}

      <ProvisionRun job={job} />
    </Panel>
    <GatewayCard />
    </>
  );
}
