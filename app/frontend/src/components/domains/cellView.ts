/** Every decision the cell card makes, lifted out of the component.
 *
 * The card's whole job is to keep four distinguishable facts distinguishable:
 * a reading that exists, a reading too thin to trust, a reading that could not
 * be fetched, and a thing nothing measures. Rendering is where those collapse
 * into one grey dash, so the branching lives here where it can be tested.
 */
import type {
  DomainCell, Observation, ObsState, PendingGrant, Provenance, Subtotal,
} from '../../lib/types';

export type Hero =
  | { kind: 'figure'; obs: Observation }
  | { kind: 'sentence'; glyph: string; tone: 'muted' | 'warn'; text: string; why: string };

/** The five sentences. Asserted as a set, so two branches cannot silently
 *  collapse into the same copy. */
export const HERO_SENTENCES = {
  none: 'No north star is declared for this domain.',
  dimensioned: 'Reported per dimension — no single figure for this domain.',
  insufficient: 'Not enough evidence yet.',
  unavailable: 'Could not read it.',
  no_source: 'Nothing measures this yet.',
} as const;

/** What each state MEANS, as a word plus a sentence. Never a bare colour. */
export const STATE_COPY: Record<ObsState, { label: string; why: string }> = {
  ok: { label: 'Read', why: 'a reading landed' },
  insufficient: { label: 'Too thin', why: 'the method is sound but the evidence is thin' },
  unavailable: { label: 'Unreadable', why: 'the source could not be read' },
  no_source: { label: 'Unmeasured', why: 'nothing measures this yet' },
};

/** The hero. The dimensioned branch MUST precede the ok branch: a dimensioned
 *  observation comes back `state: 'ok'` when ANY dimension read, while its
 *  cell-level `value` stays null — so an ok-first check renders a 48px em dash
 *  the day the first dimension reports. */
export function heroFor(ns: Observation | null | undefined): Hero {
  if (!ns) {
    return { kind: 'sentence', glyph: 'info', tone: 'muted',
             text: HERO_SENTENCES.none, why: '' };
  }
  if (ns.by_dim) {
    return { kind: 'sentence', glyph: 'more', tone: 'muted',
             text: HERO_SENTENCES.dimensioned, why: ns.why || '' };
  }
  if (ns.state === 'ok' && ns.value != null) return { kind: 'figure', obs: ns };
  const tone = ns.state === 'unavailable' ? 'warn' : 'muted';
  const glyph = ns.state === 'unavailable' ? 'alert'
    : ns.state === 'no_source' ? 'eyeOff' : 'more';
  const text = ns.state === 'ok'
    ? HERO_SENTENCES.dimensioned          // ok with no value and no by_dim
    : HERO_SENTENCES[ns.state];
  return { kind: 'sentence', glyph, tone, text, why: ns.why || '' };
}

export interface DimRow {
  dim: string; value: number | null; state: ObsState;
  provenance: Provenance | null; n: number | null; why: string;
}

export interface Shown {
  state: ObsState;
  label: string;
  provenance: Provenance | null;
  detail: string;
  dims: DimRow[];
}

/** A dimensioned metric's displayable state.
 *
 * The backend reports `ok` if ANY dimension read, and null provenance for the
 * parent. Both are wrong to show verbatim: the first overstates a metric a
 * third of which failed, the second is a FALSE absence where most dimensions
 * were measured. Report k-of-n and the weakest provenance actually observed.
 */
export function shownState(o: Observation): Shown {
  if (!o.by_dim) {
    return { state: o.state, label: STATE_COPY[o.state].label,
             provenance: o.provenance ?? null, detail: '', dims: [] };
  }
  const dims: DimRow[] = Object.entries(o.by_dim).map(([dim, d]) => ({
    dim, value: d.value, state: d.state, provenance: d.provenance ?? null,
    n: d.n, why: d.why || '',
  }));
  const read = dims.filter((d) => d.state === 'ok');
  const order: Record<string, number> = { assumed: 0, derived: 1, measured: 2 };
  const provs = read.map((d) => d.provenance).filter(Boolean) as Provenance[];
  const weakest = provs.length
    ? provs.reduce((a, b) => (order[a] <= order[b] ? a : b))
    : null;
  const state: ObsState = read.length === dims.length && dims.length > 0
    ? 'ok'
    : read.length > 0 ? 'insufficient' : 'no_source';
  return {
    state,
    label: STATE_COPY[state].label,
    provenance: weakest,
    detail: `${read.length} of ${dims.length} read`,
    dims,
  };
}

/** `gaps[]` plus one row per unread dimension — which the backend never adds,
 *  so a dimensioned failure would otherwise never appear on the panel the card
 *  nominates as the place absence is read. */
export function allGaps(cell: DomainCell): { metric: string; state: ObsState; why: string }[] {
  const out = (cell.gaps ?? []).map((g) => ({ ...g }));
  const seen = new Set(out.map((g) => g.metric));
  for (const o of cell.metrics ?? []) {
    if (!o.by_dim) continue;
    for (const [dim, d] of Object.entries(o.by_dim)) {
      if (d.state === 'ok') continue;
      const metric = `${o.metric} [${dim}]`;
      if (seen.has(metric)) continue;
      seen.add(metric);
      out.push({ metric, state: d.state, why: d.why || STATE_COPY[d.state].why });
    }
  }
  return out;
}

/** Non-ok first, stable within each run: what needs attention reads first. */
export function orderMetrics(obs: Observation[]): Observation[] {
  return obs
    .map((o, i) => ({ o, i, bad: shownState(o).state === 'ok' ? 1 : 0 }))
    .sort((a, b) => (a.bad - b.bad) || (a.i - b.i))
    .map((x) => x.o);
}

/** `<owner>.<id>` -> owner; '' when there is no dot. */
export function ownerOfMetric(metricId: string): string {
  const i = metricId.indexOf('.');
  return i === -1 ? '' : metricId.slice(0, i);
}

export interface NextAction { kind: 'grant' | 'open' | 'wait' | 'declare'; text: string; href?: string }

export function nextAction(o: Observation, pending: PendingGrant[]): NextAction {
  const state = shownState(o).state;
  if (state === 'ok') return { kind: 'wait', text: '' };
  const grant = (pending ?? []).find((p) => (p.metrics ?? []).includes(o.metric));
  if (grant) {
    return { kind: 'grant', href: '#hub/connectors',
             text: `Grant ${grant.connector} · ${grant.tool}` };
  }
  if (state === 'unavailable') {
    const owner = ownerOfMetric(o.metric);
    return owner ? { kind: 'open', href: `#${owner}`, text: `Open ${owner}` }
                 : { kind: 'wait', text: '' };
  }
  if (state === 'insufficient') {
    // Deliberately no button: an action that does nothing is worse than none.
    return { kind: 'wait', text: 'resolves as days accumulate' };
  }
  return { kind: 'declare', text: 'nothing measures this yet' };
}

export interface SubtotalLine {
  unit: string; value: number | null; sums: string[];
  missing: { metric: string; why: string }[];
}

/** Names the metrics a subtotal actually added.
 *
 * The backend groups by unit STRING, and unit-matching is not commensurability
 * — an intake and its own target share a unit and will be summed. The payload
 * carries no `included` list, so the card recomputes the set the same way and
 * shows it. Naming the contributors is the only honest thing available here:
 * it makes a meaningless sum self-evident to the person who declared it.
 */
export function subtotalLine(s: Subtotal, obs: Observation[]): SubtotalLine {
  const sums = (obs ?? [])
    .filter((o) => o.unit === s.unit && o.state === 'ok' && o.value != null)
    .map((o) => o.metric);
  return { unit: s.unit, value: s.value ?? null, sums, missing: s.missing ?? [] };
}

/** An absent day is an em dash, never `undefined`. A read-time ratio carries
 *  no `day` at all, so this is required rather than defensive. */
export function fmtDay(day: string | null | undefined): string {
  return day ? day : '—';
}

/** The raw unit token. Never scaled, never prettified into a claim. */
export function unitLabel(unit: string | null | undefined): string {
  return unit ?? '';
}
