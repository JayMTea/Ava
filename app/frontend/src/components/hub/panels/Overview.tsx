import type { ReactNode } from 'react';
import { Icon } from '../../../lib/icons';
import { Panel } from '../../ui/layout';
import { useResource } from '../hooks';
import { isExternalApp, type TabId } from '../shared';
import { hub } from '../hubApi';
import { ResourceError } from '../ui/ResourceState';
import { Badge } from '../ui/Badge';
import { Tile } from '../ui/Tile';

// Overview — a welcome blurb plus a card per configurable piece (hardware,
// agent, connectors, governance) that navigates to its tab.
export function Overview({ onGo }: { onGo: (t: TabId) => void }) {
  // Five resources, one screen. Named so their errors can be surfaced together
  // below rather than each rendering its own alert — five stacked "couldn't
  // load" boxes for one dead backend is worse than one.
  const sysRes = useResource(() => hub.system());
  const agentRes = useResource(() => hub.agentStatus());
  const connsRes = useResource(() => hub.connectors());
  const backendsRes = useResource(() => hub.backends());
  const hwRes = useResource(() => hub.hardware());
  const { data: sys } = sysRes;
  const { data: agent } = agentRes;
  const { data: connsData } = connsRes;
  const { data: backends } = backendsRes;
  const { data: hw } = hwRes;
  const firstErr = [sysRes, agentRes, connsRes, backendsRes, hwRes].find((r) => r.error);

  const conns = connsData?.connectors ?? [];
  const engineUp = backends?.any_up ?? false;
  const enabledConns = conns.filter((c) => c.enabled && isExternalApp(c)).length;

  const card = (t: TabId, icon: string, title: string, value: ReactNode, sub: string) => (
    <button type="button" className="ov-card" data-tour={`hub-${t}`} onClick={() => onGo(t)}>
      <Tile icon={icon} tone="accent" size={40} />
      <span className="ov-card-body">
        <span className="ov-card-title">{title}</span>
        <span className="ov-card-val">{value}</span>
        <span className="ov-card-sub">{sub}</span>
      </span>
      <span className="ov-card-go" aria-hidden="true"><Icon name="arrowRight" /></span>
    </button>
  );

  return (
    <>
      {firstErr && <ResourceError r={firstErr} label="your setup status" />}
      <Panel title="Welcome" subtitle="Set up and control everything from here — no terminal required.">
        <p className="hub-note" style={{ border: 0, padding: 0, background: 'none' }}>
          Each tab configures one piece: your <b>hardware</b> and its spend caps, the <b>agent</b>
          — its brain, persona, skills, memory and voice — the <b>connectors</b> that wire in your
          apps, and <b>system</b> settings like data retention. Changes are written to
          <b> ava.yaml</b> — never to source.
        </p>
      </Panel>

      <div className="hub-section" />
      <div className="ov-cards">
        {card('hardware', 'chart', 'Hardware',
          hw ? <Badge tone="accent">{hw.tier} tier</Badge> : <Badge>detecting…</Badge>,
          hw?.gpu ? hw.gpu : 'GPU · memory · tier · budgets')}
        {card('agent', 'bot', 'Agent',
          agent?.available ? <Badge tone="ok">{agent.name} ready</Badge>
            : agent?.enabled === false ? <Badge tone="muted">disabled</Badge>
              : <Badge tone="warn">not provisioned</Badge>,
          engineUp ? 'brain (engine up) · skills · memory' : 'brain · persona · skills · memory · voice')}
        {card('connectors', 'panel', 'Connectors',
          <Badge tone="accent">{enabledConns} enabled</Badge>,
          'apps Ava monitors & drives')}
        {card('system', 'sliders', 'System',
          sys ? <Badge tone="accent">{sys.features.filter((f) => f.enabled).length} on</Badge> : <Badge>…</Badge>,
          'optional capabilities · data retention · walkthrough')}
      </div>
    </>
  );
}
