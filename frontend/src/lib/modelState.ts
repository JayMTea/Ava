// The words for a model's observed liveness — defined ONCE.
//
// The backend reports `state` as a machine token (ava_bridge/hardware.py
// _STATES) and never as a sentence, because owner-facing copy lives in the
// frontend. Before this module the hardware monitor and Setup → Agent each
// invented their own wording for the same reading, so the floating panel could
// say "No model process detected yet" about the very model Setup was showing
// with a "brain" badge. Anything that renders model liveness imports from here.
import type { ModelState } from './types';

// Colour is NOT here. This module used to carry a `tone` hex per state, and all
// five were dark-theme values: against --panel2 in light mode they measured
// 1.54–2.44:1, so four of the six states were effectively invisible to anyone
// running Ava light. Tone is a token — `--ok/--warn/--err/--muted`, which every
// theme redefines — and stateTone() below is how a surface asks for it.
export type StateCopy = {
  label: string;   // the state, in two or three words
  hint: string;    // what it means for the user, and what to do about it
};

export const MODEL_STATE: Record<ModelState, StateCopy> = {
  resident: {
    label: 'In memory',
    hint: 'Loaded and ready to answer.',
  },
  idle: {
    label: 'Ready, not loaded',
    hint: 'The engine has it; the next message loads it.',
  },
  absent: {
    label: 'Not downloaded',
    hint: 'The engine is running but does not have this model yet.',
  },
  offline: {
    label: 'Engine offline',
    hint: 'Nothing is answering at its address.',
  },
  remote: {
    label: 'Runs elsewhere',
    hint: 'Hosted off this machine, so it uses none of its memory.',
  },
  unknown: {
    label: 'Not observable',
    hint: 'Ava cannot see inside this runtime to check.',
  },
};

export type StatefulRow = { status?: string; state?: string };

/** The row's state, tolerating a payload from before `state` existed.
 *  An unrecognised value reads as "we could not look" — never as "not loaded". */
export function stateOf(m: StatefulRow): ModelState {
  if (m.state && m.state in MODEL_STATE) return m.state as ModelState;
  return m.status === 'loaded' ? 'resident' : 'unknown';
}

export function stateCopy(m: StatefulRow): StateCopy {
  return MODEL_STATE[stateOf(m)];
}

/** The Hub `Badge` tone for a state, so a panel never picks its own. Only
 *  "not downloaded" and "offline" are problems the user has to act on. */
export function stateTone(m: StatefulRow): 'ok' | 'warn' | 'err' | 'muted' {
  const s = stateOf(m);
  if (s === 'resident') return 'ok';
  if (s === 'absent') return 'warn';
  if (s === 'offline') return 'err';
  return 'muted';
}
