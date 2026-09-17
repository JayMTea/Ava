import { useResource } from '../hooks';
import { ResourceError } from '../ui/ResourceState';
import { agentApi } from '../../../lib/agentApi';
import { useGateway, useGatewayStatus } from '../../../hooks/useGateway';
import { EmptyState, Panel } from '../../ui/layout';
import { Badge } from '../ui/Badge';
import { Legend } from '../ui/Legend';
import { StatRow } from '../ui/StatRow';

// Setup → Agent → Providers: which models the agent can reach, and what they
// have cost.
//
// A PEER of Brain rather than a section inside it. Brain answers "which model
// is Ava's" — a decision you make once. This answers "is the credential still
// good and what has it spent" — a thing you come back to. Burying a recurring
// check inside an 18 kB panel about a one-time choice makes it unfindable.

interface ModelRow {
  id: string;
  name?: string;
  provider?: string;
  auth?: string;
  plan?: string;
}

export function ProvidersPanel() {
  const client = useGateway();
  const { phase, why } = useGatewayStatus();
  const resource = useResource(async () => {
    if (!client || phase !== 'open') return null;
      const api = agentApi(client);
      const [m, u] = await Promise.all([api.models.list(), api.models.usage()]);
      return { models: (Array.isArray(m?.models) ? m.models : []) as ModelRow[], usage: u || null };
  }, [client, phase]);
  const { loading, error, reload: load } = resource;
  const models = resource.data?.models ?? [];
  const usage = resource.data?.usage;

  // The gateway being down is not an error in THIS panel — it is the reason
  // there is nothing to show, and saying so beats a red box that implies the
  // providers themselves are broken.
  if (phase !== 'open') {
    return (
      <Panel title="Providers" subtitle="What the agent can think with">
        <EmptyState text={
          `The agent gateway is not connected${why ? ` — ${why}` : ''}, so its `
          + 'providers cannot be read. This page describes the agent runtime, '
          + 'not Ava’s own inference router.'} />
        <p className="hub-note">
          Check the connection in <a href="#hub/agent">Runtime</a>, or manage
          Ava’s model in <a href="#hub/agent/brain">Brain</a>.
        </p>
      </Panel>
    );
  }

  return (
    <>
      <Panel
        title="Providers"
        subtitle="What the agent can think with"
        right={<button type="button" className="hub-btn ghost" onClick={load} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button>}
      >
        <ResourceError r={resource} label="providers and usage" />
        {loading && !models.length && <p className="agent-list-note">Loading providers…</p>}
        {!loading && !models.length && !error && (
          <EmptyState text="The gateway advertises no models." />
        )}
        {!!models.length && (
          <div className="stat-rows">
            {models.map((m) => (
              <StatRow
                key={m.id}
                label={m.name || m.id}
                tone={m.auth === 'ok' ? 'ok' : m.auth ? 'warn' : 'muted'}
                value={
                  <span className="meta-row">
                    {m.provider && <code>{m.provider}</code>}
                    {m.plan && <Badge tone="muted">{m.plan}</Badge>}
                  </span>
                }
              />
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Spend" subtitle="What the agent has cost, as the gateway reports it">
        {!usage || !Object.keys(usage).length ? (
          // NOT "$0". An absent figure and a zero figure are different claims,
          // and only one of them is true here.
          <EmptyState text="The gateway reports no usage figures." />
        ) : (
          <div className="stat-rows">
            {Object.entries(usage).slice(0, 8).map(([k, v]) => (
              <StatRow key={k} label={k} tone="muted" value={String(v)} />
            ))}
          </div>
        )}
      </Panel>

      <Legend
        title="Where each model lives"
        items={[
          { icon: 'sparkles', term: 'Brain',
            desc: 'Ava’s own model, served by her inference router.' },
          { icon: 'bot', term: 'Providers',
            desc: 'What the AGENT reaches, configured on the gateway. Changing '
              + 'one here does not change Ava’s.' },
        ]}
      />
    </>
  );
}
