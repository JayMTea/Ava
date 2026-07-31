import { DRIFT_LABEL, DRIFT_TONE } from '../provisionView';
import type { DriftState } from '../hubApi';
import { Badge } from './Badge';

/**
 * One vocabulary for "is this thing live in the agent sandbox?" — used by skills,
 * the persona, egress policies and tool servers alike. Four states, four
 * phrasings; a panel that invents a fifth is inventing a second product.
 *
 * The badge states a FACT. The instruction ("apply it") lives in exactly one
 * place — PendingChangesBar — because six rows each telling you to re-provision
 * is six copies of one call to action, and none of them is where the button is.
 *
 * This replaced a local `skillDeployBadge` in AgentPanel whose copy read
 * "edited · re-provision" / "not deployed · re-provision" / "provision to load".
 * The last of those over-claimed: `unknown` means we could not determine the
 * state, not that provisioning will definitely load it.
 */
export function DriftBadge({ state }: { state: DriftState }) {
  return <Badge tone={DRIFT_TONE[state]}>{DRIFT_LABEL[state]}</Badge>;
}
