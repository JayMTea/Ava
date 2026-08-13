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
import { stateOf, stateCopy, stateTone } from '../lib/modelState';

export type Row = NonNullable<HardwareStats['models']>[number];

export type RelationCopy = {
  /** Section heading. */
  group: string;
  /** What the section IS, shown under the heading while it is open, or ''.
   *
   *  The only place a relation is explained. The detail card deliberately says
   *  nothing about whose a row is, because it sits directly under the row it
   *  describes, inside the section that already said so — the panel used to
   *  print a near-identical sentence in both places, ~200px apart, in a column
   *  ~183px wide. And it is content rather than heading, so a section folded
   *  away costs its explanation nothing. */
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
    note: 'Owned by an app you connected — not what Ava thinks with.',
  },
  foreign: {
    // Short, and the same length as its sibling "Connected apps", because the
    // two are read against each other: at the narrowest the panel gets, the
    // label track is ~105px, or about eighteen characters. "Not Ava's — other
    // software on this machine." was the label for a while and wrapped to three
    // lines there.
    //
    // It does not need to carry "not Ava's" either — the section above it is
    // titled "Model use outside Ava". And it only appears when there is a
    // second group to tell it apart FROM; see `needsGroupHeads`, because on its
    // own it is that section title again, one line lower, with the same total.
    group: 'Other software',
    // One sentence. It carried a second — "The memory it holds is not available
    // to Ava." — which is the first sentence's consequence spelled out, and
    // beside a row already reporting that memory in Ava's own memory column it
    // was the same fact a third time.
    note: 'Not Ava’s — other software on this machine.',
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
  const rel = relationOf(m);
  if (rel === 'foreign') return '';
  // "Loaded and ready to answer." is as false of a CONNECTED APP's engine as of
  // a stranger's — it answers that app's requests, not Ava's, and types.ts
  // defines the relation as "Ava can see it; it is not what Ava thinks with".
  // Only this one reading is suppressed: "Engine offline" and "Not downloaded"
  // are true and useful of an app's engine too. This never surfaced while the
  // detail block defaulted to the brain; opening an app row would have shown it.
  if (rel === 'app' && stateOf(m) === 'resident') return '';
  // "In memory" that was concluded rather than read. Correct — this engine holds
  // one model from boot — but MODEL_STATE.resident.hint reads as a measurement,
  // and the whole point of the state vocabulary is that liveness is observed.
  // Saying how we know costs one clause and keeps the claim honest.
  if (stateOf(m) === 'resident' && m.state_measured === false) {
    return 'This engine loads one model at boot and holds it, so serving it means '
      + 'holding it. Ava cannot read its memory directly to confirm.';
  }
  // The agent sandbox is a container, not an address, so the generic offline
  // hint ("Nothing is answering at its address") describes something that does
  // not exist here. The backend's probe knows which of stopped / unreachable /
  // wrong-token it is; say that instead of inventing a cause.
  if (m.source === 'agent' && stateOf(m) === 'offline') {
    const why = (m.state_detail || '').trim();
    return why
      ? `Ava's agent sandbox is not running: ${why}`
      : "Ava's agent sandbox is not running, so it cannot answer.";
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
  /** What this section's rows hold together, or null when there is no honest
   *  sum (see `groupMemoryGb`). Set by `groupRows`. */
  memoryGb: number | null;
};

/** The rows, split into sections in RELATION_ORDER. Empty sections are dropped —
 *  a properly set-up box should never see a "Connected apps" heading with
 *  nothing under it. Order WITHIN a section is the backend's (brain first, then
 *  by weight), which is already the order these sections want.
 *
 *  The ORDER OF SECTIONS is RELATION_ORDER and not "biggest first". Ranking by
 *  memory would put a stranger's 65 GB process above the app the owner
 *  connected, and this module exists because four different relationships once
 *  rendered as one undifferentiated list. Memory is answered by the totals and
 *  the aligned column; whose is answered by the order. */
export function groupRows(rows: Row[], pool?: MemPool): RowGroup[] {
  const out: RowGroup[] = [];
  for (const relation of RELATION_ORDER) {
    const inGroup = rows.filter((m) => relationOf(m) === relation);
    if (inGroup.length) {
      out.push({
        relation,
        copy: MODEL_RELATION[relation],
        rows: inGroup,
        memoryGb: pool ? groupMemoryGb(inGroup, pool) : null,
      });
    }
  }
  return out;
}

/** Does a section's groups need their own headings?
 *
 *  Only when there is more than one, because a group heading exists to tell one
 *  relationship apart from another. A single group under "Model use outside
 *  Ava" was the section title said twice — "Model use outside Ava · 12.0 GB"
 *  and, one line below it, "Other software · 12.0 GB": same claim, same number,
 *  a heading and a fold spent on a distinction nobody was drawing. The group's
 *  NOTE still renders, because that is the line that says something new. */
export function needsGroupHeads(groups: RowGroup[]): boolean {
  return groups.length > 1;
}


// ---------------------------------------------------------------------------
// Memory: what a row holds, and what it holds a share OF.
//
// `memory_gb` is NOT one currency, which is the whole reason this section is
// careful. In ava_bridge/hardware.py an nvidia-smi row reports GPU memory
// (_gpu_model_processes), a docker row reports the container's system RSS
// (_docker_model_containers), and an api row reports Ollama's `size_bytes`,
// which spans RAM and VRAM together. On a unified box those are one pool and
// adding them is honest. On a discrete box they are two, and a percentage of
// the wrong one is a lie that looks exactly like a measurement.
// ---------------------------------------------------------------------------

export type MemPool = {
  /** The denominator, or null when this machine reports none we can divide. */
  totalGb: number | null;
  /** True when every row's figure describes the SAME pool. Only then can
   *  shares and section totals be drawn. */
  unified: boolean;
  /** How to name the denominator to the owner. */
  noun: string;
};

/** How close the GPU total has to be to the system total before we call it one
 *  pool. The two arrive in different units from different probes (MiB from
 *  nvidia-smi, GB from psutil) and one is the other minus whatever the firmware
 *  reserves, so they never match to the digit even on a DGX Spark, where they
 *  genuinely are the same silicon. */
const UNIFIED_RATIO = 0.85;

/** The pool this panel is apportioning.
 *
 *  Deliberately derived from what the payload already carries rather than from
 *  a new backend field: `gpu.mem_total_mb` and `mem.total_gb` are both on the
 *  wire today and neither is rendered anywhere. */
export function poolOf(s: Pick<HardwareStats, 'gpu' | 'mem'> | null): MemPool {
  const sysGb = s?.mem?.total_gb ?? null;
  const gpuMb = s?.gpu?.mem_total_mb ?? null;
  if (sysGb == null || sysGb <= 0) return { totalGb: null, unified: false, noun: '' };
  // No GPU reading at all: one system pool. This is the CPU-only and the
  // invisible-GPU case (a WSL2 box where nvidia-smi is not reachable), where
  // every row's figure is RAM and the sum is honest.
  if (gpuMb == null || gpuMb <= 0) {
    return { totalGb: sysGb, unified: true, noun: "this machine's" };
  }
  const gpuGb = gpuMb / 1024;
  if (gpuGb >= sysGb * UNIFIED_RATIO) {
    return { totalGb: sysGb, unified: true, noun: "this machine's" };
  }
  // Discrete GPU: rows are drawn from two pools. We still know how big the
  // graphics pool is and can name it per row, but nothing may be summed or
  // drawn as a share, because half the rows are counted in the other one.
  return { totalGb: gpuGb, unified: false, noun: "the GPU's" };
}

/** GB this row holds in the pool right now, or null for "nothing countable".
 *
 *  null is NOT zero. A `remote` row uses none of this box's memory; `idle`,
 *  `absent` and `offline` rows hold nothing at all. Rendering any of them as a
 *  zero-width bar would silently agree that "runs elsewhere" and "holds 0 GB"
 *  are the same reading, and this panel's whole vocabulary exists to keep those
 *  apart. */
export function heldGb(m: Row): number | null {
  if (stateOf(m) !== 'resident') return null;
  const gb = m.memory_gb;
  return gb != null && gb > 0 ? gb : null;
}

/** This row's share of the pool, 0-100, or null when there is no honest one.
 *
 *  NOT renormalised to the largest row. A 0.4 GB row on a 121 GB box is
 *  supposed to look like nothing; scaling it up to fill the width would be the
 *  comfortable lie. */
export function shareOf(m: Row, p: MemPool): number | null {
  if (!p.unified || p.totalGb == null) return null;
  const held = heldGb(m);
  if (held == null) return null;
  return Math.max(0, Math.min(100, (held / p.totalGb) * 100));
}

/** The denominator in words, or '' when there is none to give. */
export function memPhrase(m: Row, p: MemPool): string {
  const held = heldGb(m);
  if (held == null || p.totalGb == null) return '';
  // A discrete box can still say what a GPU row is a share of — it is only the
  // SUM across rows that mixes pools, not the individual reading.
  if (!p.unified && m.source !== 'nvidia-smi') return '';
  const pct = Math.round((held / p.totalGb) * 100);
  // Over 100% means the denominator is wrong for this row, whatever poolOf
  // concluded. Say nothing rather than print "137% of the GPU's 48.0 GB" — and
  // do NOT clamp, which would render the same broken reading as a confident
  // "100%" instead of admitting there is nothing to report.
  if (pct > 100) return '';
  return `${pct}% of ${p.noun} ${p.totalGb.toFixed(1)} GB`;
}

/** What a section's rows hold together, or null when no honest sum exists.
 *
 *  null rather than 0 when nothing reported: a "0.0 GB" heading claims a
 *  measurement nobody made. */
export function groupMemoryGb(rows: Row[], p: MemPool): number | null {
  if (!p.unified) return null;
  let total = 0;
  let any = false;
  for (const m of rows) {
    const held = heldGb(m);
    if (held != null) { total += held; any = true; }
  }
  return any ? total : null;
}

// ---------------------------------------------------------------------------
// Row and card wording.
// ---------------------------------------------------------------------------

/** The dot's tone, as a token name for hub.css's `.tone-*` system.
 *
 *  The dot follows the STATE, and only shades it by GPU busyness when that is
 *  actually known. Keying it off gpu_util first painted a grey "idle" dot
 *  beside a green "In memory" — two contradictory readings of one row, and grey
 *  on every CPU-only box, where per-process GPU util is never available. */
export function activityTone(m: Row): 'ok' | 'warn' | 'err' | 'muted' {
  const u = m.gpu_util;
  if (stateOf(m) === 'resident' && u != null && u >= 30) return 'ok';
  if (stateOf(m) === 'resident' && u != null && u > 0) return 'warn';
  return stateTone(m);
}

/** The row's ONE second line, or '' — and short enough to survive ellipsis at
 *  ~183px.
 *
 *  `resident` says NOTHING. The dot is already green, and "In memory" under a
 *  green dot is the same reading twice on the row that is working fine. Every
 *  other state is a diagnosis — "Not downloaded", "Engine offline" — and has to
 *  be legible without anyone clicking anything, which is the whole reason this
 *  panel stopped hiding its list behind a dropdown.
 *
 *  `holdsLine`'s "Ava cannot tell what this program is holding." is the right
 *  sentence in the card and the wrong one in a row: at this width it ellipses
 *  to "Ava cannot tell what this progr…", which reads as a truncated error. */
export function rowSub(m: Row): string {
  if (!identified(m)) {
    const names = (m.components || []).map((c) => c.name).filter(Boolean);
    return names.length ? holdsLine(m) : 'Contents unknown';
  }
  return stateOf(m) === 'resident' ? '' : stateCopy(m).label;
}

/** The explanation a row carries in the LIST, or ''.
 *
 *  Same rule as `rowSub`, one level down: a working model spends no words.
 *  "Loaded and ready to answer." sat under "In memory" under a green dot —
 *  three renderings of one fact, on the row that needed no attention at all. */
export function listHint(m: Row): string {
  if (stateOf(m) === 'resident') return '';
  return [rowHint(m), servedLine(m)].filter(Boolean).join(' ');
}

/** Is this row Ava's own?
 *
 *  The panel's top-level cut. `brain` is what Ava thinks with and `configured`
 *  is an engine the owner registered with Ava and that Ava can route to —
 *  both Ava's, however unreachable either happens to be right now. `app` and
 *  `foreign` are somebody else's.
 *
 *  This exists because the cut used to be "is it the brain", while the heading
 *  over everything below the brain said "Model use outside Ava". A configured
 *  backend is not outside Ava, so a row the owner had registered themselves
 *  was filed under a heading calling it foreign — and then refused to fold,
 *  because the fold rule knew it was Ava's even though the heading did not.
 *
 *  It is also the panel's ONE fold rule now. There is no per-relation
 *  collapsibility any more: what folds is the "Model use outside Ava" section,
 *  and this predicate is what decides which rows are inside it — so the
 *  heading, the membership and the control cannot drift apart again. */
export function isAvas(relation: ModelRelation): boolean {
  return relation === 'brain' || relation === 'configured';
}

/** What an engine DOES have, when the model it was pointed at was never
 *  downloaded. "It does not have this model" is only half an answer — the other
 *  half is usually the whole diagnosis (a tag typo, or a pull that never ran). */
export function servedLine(m: Row): string {
  if (stateOf(m) !== 'absent') return '';
  const served = m.served || [];
  if (!served.length) return '';
  const shown = served.slice(0, 3).join(', ');
  return served.length > 3
    ? `It has: ${shown} +${served.length - 3} more.`
    : `It has: ${shown}.`;
}

/** A component's kind and residency, in the owner's words.
 *
 *  "(configured — not in memory)" said `configured` about every entry in a list
 *  where being listed IS being configured. */
export function componentMeta(
  c: { kind: string; kind_label?: string; in_memory?: boolean | null },
): string {
  const kind = (c.kind_label || c.kind || 'Model').trim();
  if (c.in_memory === false) return `${kind} · not in memory`;
  if (c.in_memory == null) return `${kind} · residency unknown`;
  return `${kind} · in memory`;
}

/** The GPU temperature's tone.
 *
 *  Here rather than in the component because it is the same kind of decision as
 *  everything else in this file, and because the three hexes it replaces
 *  (#e0364d/#e0a06f/#7fd0a0) existed in neither theme's palette. */
export function tempTone(t: number | null | undefined): 'ok' | 'warn' | 'err' | null {
  if (t == null) return null;
  if (t >= 85) return 'err';
  if (t >= 70) return 'warn';
  return 'ok';
}
