import { hub } from '../hubApi';
import { useResource } from '../hooks';
import { useGatewayStatus } from '../../../hooks/useGateway';
import { Panel } from '../../dashboard/layout';
import { Badge } from '../ui/Badge';
import { ResourceError } from '../ui/ResourceState';
import { StatRow } from '../ui/StatRow';

// The agent gateway, on the page that already owns "is my agent live".
//
// One home: the Agent console's status chip links HERE rather than duplicating
// any of it, and this is the page `fixes.ts` routes an `agent_*` code to.

export function GatewayCard() {
  const r = useResource(() => hub.agentGateway());
  const live = useGatewayStatus();
  const g = r.data;

  return (
    <Panel title="Agent gateway" subtitle="OpenClaw's own control plane">
      <ResourceError r={r} label="the gateway settings" />

      <div className="stat-rows">
        <StatRow
          label="Connection"
          tone={live.phase === 'open' ? 'ok'
            : live.phase === 'connecting' ? 'warn'
            : live.phase === 'unconfigured' ? 'muted' : 'err'}
          value={live.phase === 'open' ? 'connected'
            : live.phase === 'unconfigured' ? 'not the configured runtime'
            : (live.why || live.phase)}
        />
        <StatRow
          label="Operator token"
          // Never the value, and never generated — it has to match something
          // the gateway will accept, so an invented one fails every handshake
          // while reporting the gateway's fault.
          tone={g?.configured ? 'ok' : 'warn'}
          value={g?.configured ? `configured (${g.source})` : 'not set'}
        />
        <StatRow
          label="Address"
          tone="muted"
          value={<code>{g?.url || 'derived from the sandbox registry'}</code>}
        />
        <StatRow
          label="Off-loopback"
          tone={g?.allow_remote ? 'warn' : 'ok'}
          value={g?.allow_remote ? 'allowed' : 'refused'}
        />
        {/* Identity rows appear only when the runtime HAS an identity to
            report — a runtime with none answers null rather than the panel
            rendering empty rows that read as "unknown" when the truth is
            "not applicable". */}
        {g?.identity?.agent_name && (
          <StatRow
            label="Agent"
            tone="muted"
            value={`${g.identity.agent_name}${g.identity.agent_id ? ` (${g.identity.agent_id})` : ''}`}
          />
        )}
        {g?.identity?.device_id && (
          <StatRow
            label="This install"
            tone="muted"
            // A fingerprint the operator COMPARES, not transcribes — the
            // gateway shows the same value to its own operators.
            value={<code>{g.identity.device_id}</code>}
          />
        )}
        {typeof g?.identity?.paired === 'number' && (
          <StatRow
            label="Paired devices"
            tone={g.identity.pending ? 'warn' : 'muted'}
            value={g.identity.pending
              ? `${g.identity.paired} paired · ${g.identity.pending} waiting`
              : `${g.identity.paired}`}
          />
        )}
      </div>

      {/* Read-only, and deliberately visible.
          `POST /api/gateway/rpc` refuses to WRITE the gateway's device-auth
          keys — a UI bug that flipped one would turn a transient mistake into a
          permanent posture change surviving every restart. Without this row the
          capability would be silently missing rather than visibly held
          elsewhere, which is the difference between a decision and a gap. */}
      {g?.device_auth && (
        <p className="hub-note with-icon">
          Whether the gateway authenticates browsers is not editable from Ava.
          Change it deliberately with{' '}
          <code>{g.device_auth.change_with}</code>.
          {!g.device_auth.known && (
            <> The sandbox registry could not be read, so its current setting is
            unknown here.</>
          )}
        </p>
      )}

      {!g?.configured && (
        <p className="hub-note">
          Without a token the gateway runtime cannot connect, and chat falls back
          to whichever runtime <code>agent.runtime</code> names.{' '}
          <code>ava agent provision</code> writes one where the CLI lives.
        </p>
      )}

      <span className="hub-badge-row">
        <Badge tone={live.phase === 'open' ? 'ok' : 'muted'}>
          {live.phase === 'open' ? 'gateway live' : 'gateway offline'}
        </Badge>
      </span>
    </Panel>
  );
}
