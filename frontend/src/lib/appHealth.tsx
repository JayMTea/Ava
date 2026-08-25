import type { AppHealth } from './types';

// The sidebar's readiness dot: is this app ready to use RIGHT NOW?
//
// WHY THIS IS NOT THE IDENTITY DOT
// --------------------------------
// `appColor.tsx`'s <AppDot> answers "whose is this?" and is the right marker on
// a chat tool chip or a preview card, where the question is attribution. In the
// sidebar the owner is not asking whose an app is — the name and the icon are
// right there — they are asking whether clicking it will work. So the sidebar
// spends its dot on health and keeps identity on the row's ICON, which is
// tinted with appAccent(). CLAUDE.md's rule ("a UI element representing a
// connected app must carry the app's accent") is satisfied by the tint; what
// changed is which glyph carries it, not whether the row does.
//
// The backend hands over facts and one rolled-up code (see
// `dashboard.apps_health`). Every sentence below is written HERE, because
// owner-facing copy is the frontend's job.

export type Health = AppHealth['health'];

/** One word for a badge, and the state word in each row's screen-reader label. */
export const HEALTH_LABEL: Record<Health, string> = {
  ready: 'Ready',
  partial: 'Partial',
  down: 'Down',
  off: 'Off',
};

/** Everything that is missing, as owner-facing phrases. Empty when nothing is.
 *
 *  Only ever reports what the app ACTUALLY declares: an app with no credential
 *  slot cannot be missing a credential, and saying so would send the owner
 *  looking for a setting that does not exist. */
export function healthReasons(h: AppHealth): string[] {
  const out: string[] = [];
  if (h.service === 'down') out.push('its health check is not answering');
  if (h.service === 'unknown') out.push('its health check could not be read');
  if (h.auth_env && !h.auth_set) out.push(`no credential saved (${h.auth_env})`);
  if (h.tools_expected && !h.tools_deployed) out.push('its tools are not deployed to the agent');
  if (h.policy_expected && !h.policy_present) out.push('its egress policy has not been generated');
  return out;
}

/** The dot's tooltip: the verdict, then why — so a colour is never a riddle.
 *  `label` is the app's name, because a tooltip that opens with "Partial" and
 *  never says whose is one the owner has to guess at. */
export function healthTitle(label: string, h: AppHealth | undefined): string {
  if (!h) return `${label} — checking…`;
  if (h.health === 'off') return `${label} — switched off in Setup.`;
  const why = healthReasons(h);
  if (h.health === 'ready') {
    // An app with no probe is reported ready on its wiring alone, and the
    // tooltip says so rather than implying Ava watched it answer.
    return h.service === null
      ? `${label} — ready. Everything it declares is in place (it declares no health check).`
      : `${label} — ready. Answering, and everything it needs is in place.`;
  }
  const head = h.health === 'down' ? `${label} — not answering` : `${label} — partly ready`;
  return why.length ? `${head}: ${why.join('; ')}.` : `${head}.`;
}

/** The readiness dot. Decorative on its own — every call site pairs it with the
 *  app's name, and carries the explanation in `title`. */
export function HealthDot({ health, title, className }: {
  health: Health | undefined; title?: string; className?: string;
}) {
  // `unknown` while the first poll is in flight: a grey ring, never a green dot
  // we have not earned.
  const state = health ?? 'unknown';
  return (
    <span
      className={`health-dot is-${state}` + (className ? ` ${className}` : '')}
      title={title}
      aria-hidden="true"
    />
  );
}

/** Index the health list by app id for O(1) lookup from a render loop. */
export function byId(list: AppHealth[]): Record<string, AppHealth> {
  const out: Record<string, AppHealth> = {};
  for (const h of list) out[h.id] = h;
  return out;
}
