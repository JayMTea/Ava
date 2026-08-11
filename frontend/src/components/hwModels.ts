// How the hardware monitor's model list decides what to say — as plain
// functions, because the SPA has no component-render harness and a decision
// left inside a component is a decision nobody can test (see inferenceView.ts).
//
// The bug this exists for: the panel listed every model holding memory on the
// box in ONE flat dropdown, under the heading "Models Ava can see", with one
// vocabulary and equal weight. On a real box that was Ava's brain, a third-party
// ComfyUI holding 65 GB, another app's vLLM, and a backend Ava had invented
// because nothing was configured. Three of the four were not Ava's, and the
// owner reasonably read the list as stale junk. Nothing was stale; the list just
// could not say whose anything was.
//
// State words are NOT redefined here — they are imported from lib/modelState.ts,
// which owns them for every surface.
import type { HardwareStats, ModelRelation } from '../lib/types';
import { stateOf, stateCopy } from '../lib/modelState';

export type Row = NonNullable<HardwareStats['models']>[number];

export type RelationCopy = {
  /** Section heading. */
  group: string;
  /** One line under the heading saying what the section IS, or '' for none. */
  note: string;
};

export const MODEL_RELATION: Record<ModelRelation, RelationCopy> = {
  brain: {
    group: "Ava's brain",
    note: '',
  },
  configured: {
    group: "Ava's other engines",
    note: '',
  },
  app: {
    group: 'Connected apps',
    note: 'Part of an app you connected to Ava. Ava can see it; it is not what Ava thinks with.',
  },
  foreign: {
    group: 'Other programs holding memory',
    note: 'Not Ava’s — other software on this machine. The memory it holds is not available to Ava.',
  },
};

/** The order sections are shown in: Ava's own first, then everything else. */
export const RELATION_ORDER: ModelRelation[] = ['brain', 'configured', 'app', 'foreign'];

/** The row's relation, tolerating a payload from before `relation` existed.
 *
 *  Never claims `brain` without `role_key` and never `app` without an app id:
 *  a fallback that guessed either would put someone else's model under Ava's
 *  name, which is the exact failure this module exists to end. */
export function relationOf(m: Row): ModelRelation {
  const r = m.relation;
  if (r && r in MODEL_RELATION) return r;
  if (m.role_key === 'brain') return 'brain';
  if (m.backend) return 'configured';
  if (m.app) return 'app';
  return 'foreign';
}

/** Did anything actually READ an identity for this row?
 *
 *  `model_id` stays null when nothing did — the backend names such a row from
 *  its command line and mapped files instead, and never invents an id. */
export function identified(m: Row): boolean {
  return Boolean(m.model_id || m.backend || m.role_key === 'brain');
}

/** The row's headline. */
export function rowTitle(m: Row): string {
  return (m.model || '').trim() || m.name || 'Unidentified process';
}

/** What an unidentified row has loaded, from its mapped weight files.
 *
 *  Only for rows nothing could name: for the rest the title already says it. */
export function holdsLine(m: Row): string {
  if (identified(m)) return '';
  const names = (m.components || []).map((c) => c.name).filter(Boolean);
  if (!names.length) return 'Ava cannot tell what this program is holding.';
  const shown = names.slice(0, 2).join(', ');
  return names.length > 2
    ? `Holds ${shown} +${names.length - 2}`
    : `Holds ${shown}`;
}

/** How Ava came to know about this row, in words.
 *
 *  The panel printed the raw `source` token ("nvidia-smi") as owner-facing copy,
 *  against the rule that the backend returns facts and the frontend words them.
 *  An unrecognised token is returned verbatim rather than invented. */
export function foundVia(source: string | undefined): string {
  switch ((source || '').toLowerCase()) {
    case 'nvidia-smi': return 'GPU process telemetry';
    case 'docker': return 'Docker';
    case 'api': return "the engine's own API";
    case 'agent': return "Ava's agent sandbox";
    default: return source || 'unknown';
  }
}

/** What this row's state MEANS for the owner, or '' for nothing worth saying.
 *
 *  Two overrides, both presentation-only — `state` itself is never touched, so
 *  "liveness is observed, never inferred" still holds:
 *
 *  - A foreign row gets no hint. MODEL_STATE.resident.hint is "Loaded and ready
 *    to answer.", which is false of another program's image model: it will never
 *    answer anything of Ava's. The section note carries the meaning instead.
 *  - An implicit backend that is offline is not a fault. Nothing was configured,
 *    so "Nothing is answering at its address" describes an address nobody chose. */
export function rowHint(m: Row): string {
  if (relationOf(m) === 'foreign') return '';
  // "In memory" that was concluded rather than read. Correct — this engine holds
  // one model from boot — but MODEL_STATE.resident.hint reads as a measurement,
  // and the whole point of the state vocabulary is that liveness is observed.
  // Saying how we know costs one clause and keeps the claim honest.
  if (stateOf(m) === 'resident' && m.state_measured === false) {
    return 'This engine loads one model at boot and holds it, so serving it means '
      + 'holding it. Ava cannot read its memory directly to confirm.';
  }
  if (m.implicit && stateOf(m) === 'offline') {
    return 'Nothing is configured here — this address came from AVA_BACKEND_URL, '
      + 'and nothing is listening on it.';
  }
  return stateCopy(m).hint;
}

export type RowGroup = {
  relation: ModelRelation;
  copy: RelationCopy;
  rows: Row[];
};

/** The rows, split into sections in RELATION_ORDER. Empty sections are dropped —
 *  a properly set-up box should never see a "Connected apps" heading with
 *  nothing under it. Order WITHIN a section is the backend's (brain first, then
 *  by weight), which is already the order these sections want. */
export function groupRows(rows: Row[]): RowGroup[] {
  const out: RowGroup[] = [];
  for (const relation of RELATION_ORDER) {
    const inGroup = rows.filter((m) => relationOf(m) === relation);
    if (inGroup.length) {
      out.push({ relation, copy: MODEL_RELATION[relation], rows: inGroup });
    }
  }
  return out;
}
