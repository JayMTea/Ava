import { Badge } from '../hub/ui/Badge';
import { useGatewayStatus } from '../../hooks/useGateway';

// The glance, not the page.
//
// This is what replaces a fourth "Health" section: a fresh install needs to be
// told what is wrong, but a route you visit once is dead weight forever after.
// The chip sits in the section bar and links to Setup → Agent → Runtime, which
// stays the authoritative page — one home, and the link says where it goes.

const TONE = {
  open: 'ok', connecting: 'warn', down: 'err', closed: 'muted',
  unconfigured: 'muted',
} as const;

const LABEL = {
  open: 'gateway live', connecting: 'connecting…', down: 'gateway down',
  closed: 'gateway off',
  // A Direct-floor install has no gateway to be up or down — this runtime just
  // is not one that has a control plane. Not an error, and the link explains.
  unconfigured: 'not set up',
} as const;

export function GatewayStatusChip() {
  const { phase, why } = useGatewayStatus();
  return (
    <a className="agent-chip" href="#hub/agent/runtime"
       title={why || 'Agent runtime settings'}>
      <Badge tone={TONE[phase]}>{LABEL[phase]}</Badge>
    </a>
  );
}
