// What the "free this model's memory" control offers, and what it says.
//
// Pure functions, because this is where every judgement about a destructive button
// lives and the SPA has no component-render harness — same reason `inferenceView.ts`
// and `provisionView.ts` exist. A test can drive every row shape here with no DOM.
//
// Two rules run through all of it, and both are about not overclaiming:
//
//   * **A number only when it was measured.** `freed_gib` absent means the amount is
//     unknown, and "freed 0 GB" is a different claim from "we could not tell" — the
//     first says nothing happened. Every receipt below drops one fact at a time and
//     says which it dropped, rather than filling the gap with the declared weight.
//   * **`resident: null` is "we could not look", never "no."** So a model whose
//     residency is unknowable still gets an enabled button: freeing it is safe to
//     try, and the result reports what actually came back. Refusing to offer the
//     action would be inferring absence from ignorance, which is the bug the whole
//     residency vocabulary exists to prevent.
import type { AllocJob, AllocModel, AllocReport } from './hub/hubApi';

// Where each field renders matters as much as what it says, because the row lives in
// a ~209px column inside a floating panel with a finite height. The split:
//
//   VISIBLE   — `state` (as a coloured dot), the name, `heldShort`, and `blocked`.
//               Between them they answer "what is this, is it holding anything, and
//               can I do something about it" at a glance.
//   ON HOVER  — `state` in words and `hint`. Reference material: true, worth having,
//               and not worth 75-120px of column each time. `hint` is up to four
//               joined sentences, which is what made a row cost 134px.
//
// Nothing is dropped and nothing is behind a click — the explanation is one hover
// away, and the dot plus the size carry the reading on their own.
export interface RowView {
  /** The button, or null when there is nothing honest to offer. */
  action: { verb: 'release' | 'restore'; label: string; busyLabel: string } | null;
  /** VISIBLE. Why there is no button — so it is never a mystery. */
  blocked: string;
  /** TOOLTIP. The full explanation, joined. Always true, never reassuring. */
  hint: string;
  /** A confirm step is required before acting. Rendered inline, never hidden. */
  confirm: string;
  tone: 'ok' | 'warn' | 'err' | 'accent' | 'muted';
  /** Status in words. Rendered as the dot's colour; the words go in the tooltip. */
  state: string;
}

/** How much this row is holding, in a sentence — for the tooltip and for wide space. */
export function heldLabel(m: AllocModel): string {
  if (m.resident === false) return 'nothing';
  // An unmeasured size is a declared hint, not a reading. Rendering it as a figure
  // is how a panel starts quoting config back as telemetry.
  if (m.resident_gib == null || !m.measured) return 'an unknown amount';
  return `${m.resident_gib.toFixed(1)} GB`;
}

/** The same reading, compact enough to sit beside a name in a narrow column.
 *
 * `?` rather than a number for an unmeasured size, and `—` rather than `0 GB` for
 * one observed empty: both are the same refusal `heldLabel` makes at length, which
 * is why the long form travels in the row's tooltip beside it.
 */
export function heldShort(m: AllocModel): string {
  if (m.resident === false) return '—';
  if (m.resident_gib == null || !m.measured) return '?';
  return `${m.resident_gib.toFixed(1)} GB`;
}

export function rowView(m: AllocModel, breaker?: AllocReport['breaker']): RowView {
  const quiesced = !!breaker?.quiesced;
  const gaveUp = !!breaker?.models?.[m.id]?.given_up;

  // Already freed by the owner: the only state that offers a way back, and only
  // for a driver that has something to start. A self-restoring engine reloads on
  // its own, so a button would imply Ava was doing something it is not.
  if (m.released_by_owner) {
    const back = m.restore_kind === 'start';
    return {
      action: back
        ? { verb: 'restore', label: 'Load it back', busyLabel: 'Loading it back…' }
        : null,
      // The one state that is NOT legible from the size beside it. "—" says the
      // model is holding nothing; it does not say the owner is the reason, and
      // that difference is the whole point of tracking who released it. A
      // self-restoring row has no button, so without this it would show a dot and
      // a dash and nothing else.
      blocked: back ? '' : 'You freed this — reloads on next use',
      hint: back
        ? 'You freed this. Ava will not start it again on its own.'
        : 'You freed this. It reloads itself the next time something needs it.',
      confirm: '',
      tone: 'accent',
      state: 'You freed this',
    };
  }

  const no = (blocked: string, hint = ''): RowView =>
    ({ action: null, blocked, hint, confirm: '', tone: 'muted', state: 'Watching only' });

  if (!m.local) return no('Runs on another machine', 'Its memory is not this box\'s to free.');
  if (m.pinned) {
    return { ...no('Protected'), tone: 'muted', state: 'Protected',
             hint: 'You marked this pinned, so Ava will never release it.' };
  }
  if (m.observe_only || m.modes.length === 0) {
    return no('No release lever',
              m.release_blocked === 'not resident'
                ? 'It is not holding anything right now.'
                : 'Ava can see this but has no way to free it. Give it one in ava.yaml.');
  }
  if (m.held_by > 0) {
    return { ...no('In use right now'), tone: 'warn', state: 'In use',
             hint: 'Something is using it. Ava does not take memory from work in progress.' };
  }
  if (quiesced) {
    return { ...no('Paused'), tone: 'warn', state: 'Paused',
             hint: breaker?.quiesce_reason
               || 'Ava paused its own memory management after too many actions.' };
  }

  // `stop` is a materially bigger promise than `unload` and the verb must say so —
  // one leaves the engine running, the other takes the process down.
  const stop = !m.modes.includes('unload');
  const action = {
    verb: 'release' as const,
    label: stop ? 'Stop it and free the memory' : 'Free its memory',
    busyLabel: stop ? 'Stopping…' : 'Freeing…',
  };

  const hints: string[] = [];
  if (m.resident === null) {
    hints.push('Ava cannot see inside this one, so it cannot confirm afterwards — '
               + 'but freeing it is safe to try.');
  }
  if (m.self_restoring) hints.push('It reloads itself the next time something needs it.');
  if (!m.self_restoring && m.restore_kind === 'none') {
    hints.push('Ava cannot start this again — you would bring it back yourself.');
  }
  if (gaveUp) hints.push('Ava stopped trying to manage this one on its own.');
  const unverified = m.notes.some((n) => n.includes('NOT VERIFIED'));
  if (unverified) {
    hints.push('This lever has not been tested against a live '
               + `${m.driver === 'http-unload' ? 'engine' : 'service'} yet — `
               + 'it may turn out to do nothing.');
  }

  // A confirm on every click trains dismissal, so it is spent only where the cost is
  // real: losing the ability to answer, or taking a process down.
  let confirm = '';
  if (m.is_brain) {
    confirm = m.self_restoring
      ? 'Free the memory Ava thinks with? Your next message loads it again, '
        + 'so this costs one slow reply — not a broken Ava.'
      : 'Free the memory Ava thinks with? Ava cannot answer chat until you load it '
        + 'back. Nothing is lost — your chats, memory and settings are untouched, '
        + 'and the model is not deleted.';
  } else if (stop) {
    confirm = 'Stop this and free its memory? It stays down until you bring it back.';
  }

  return {
    action, blocked: '', confirm,
    hint: hints.join(' '),
    tone: m.resident === null ? 'muted' : m.resident ? 'ok' : 'muted',
    state: m.resident === null ? 'Not observable'
      : m.resident ? 'Holding memory' : 'Not loaded',
  };
}

/** The sentence for a finished job. Degrades the claim, never invents a number.
 *
 * `label` is the name the row shows. Without it the receipt falls back to the model
 * ID, so a row headed "Ollama" reported "Freed 12.4 GB from local" — two names for
 * one thing, in adjacent lines.
 */
export function receipt(job: AllocJob, label?: string): { text: string; ok: boolean } | null {
  if (!job || job.status === 'idle' || job.status === 'running') return null;
  const r = job.result;
  const name = label || job.model || 'it';
  if (!r) return { text: `${name}: no result was recorded.`, ok: false };

  if (!r.ok) {
    // "Nothing was done" is a warning, not an error: declining is a correct
    // outcome for a lever that is not there, and painting it red teaches the owner
    // that a working system is broken.
    const why = r.detail || (r.log || job.log || []).slice(-1)[0] || '';
    if (r.code === 'held_live') {
      return { text: `${name} is in use right now, so Ava left it alone.`, ok: false };
    }
    if (r.code === 'turn_in_flight') {
      return { text: 'Ava is answering right now. Try again when it finishes.', ok: false };
    }
    if (r.code === 'job_running') {
      return { text: 'Something else is already being freed.', ok: false };
    }
    return { text: `Nothing was done${why ? `: ${why}` : '.'}`, ok: false };
  }

  if (job.verb === 'restore') return { text: `${name} is back.`, ok: true };

  // Four rungs, most-known first. Each drops exactly one fact and names it.
  const freed = r.freed_gib;
  if (freed != null && freed > 0.05) {
    const span = (r.free_before_gib != null && r.free_after_gib != null)
      ? ` This machine: ${r.free_before_gib.toFixed(1)} GB free → ${r.free_after_gib.toFixed(1)} GB free.`
      : '';
    return { text: `Freed ${freed.toFixed(1)} GB from ${name}.${span}`, ok: true };
  }
  if (r.measured) {
    // It let go, and the pool did not move. Honest, and not a failure: the kernel
    // reclaims asynchronously, and other programs use this pool too.
    return {
      text: `${name} released its memory, but this machine's free-memory reading has `
            + 'not moved yet. Other programs use this pool too, and the kernel can '
            + 'take a moment to hand memory back.',
      ok: true,
    };
  }
  return {
    text: `${name} released its memory. This machine reports no free-memory figure `
          + 'Ava trusts, so there is no number to show you — what Ava can confirm is '
          + 'that it was told to let go and did not refuse.',
    ok: true,
  };
}

/** Rows worth showing, and the order to show them.
 *
 * Everything declared is shown, including rows with no lever: "Ava can see this and
 * cannot free it" is the honest answer on most stock installs, and hiding it would
 * make the panel look empty rather than truthful. The brain sorts first because it
 * is the one whose release the owner most needs to think about.
 */
export function rowOrder(models: AllocModel[]): AllocModel[] {
  const rank = (m: AllocModel) =>
    (m.is_brain ? 0 : 2) + (m.released_by_owner ? -1 : 0) + (m.resident ? 0 : 1);
  return [...models].sort((a, b) => rank(a) - rank(b) || a.id.localeCompare(b.id));
}
