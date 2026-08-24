// The chat log is modelled as an ordered list of discriminated-union items.
// Rendering is driven purely by this state (no imperative DOM), which is the
// core maintainability win of the framework migration.

import type { Artifact, Attachment, CotStep, ModelInfo, Preview } from './types';

export type ChatItem =
  | { kind: 'user'; id: string; text: string; atts: Attachment[]; failed?: boolean; runId?: string; idem?: string }
  | { kind: 'ava'; id: string; text: string; model?: ModelInfo | null; toolsUsed?: string[]; artifact?: Artifact | null; srcText: string; srcAtts: Attachment[]; audio?: string; runId?: string; streaming?: boolean }
  | {
      kind: 'cot';
      id: string;
      label: string;
      steps: CotStep[];
      status: 'running' | 'done' | 'error';
      /**
       * Written EXACTLY ONCE, when the run finishes. Never while it is in
       * flight.
       *
       * `ChainOfThought.tsx` uses `typeof secs === 'number'` as its live-vs-
       * replay discriminator: a number means "Thought for Ns", its absence
       * means "Reasoning" (a replayed chain). Setting it during a streamed run
       * would make the two indistinguishable and leave the replay wording
       * unreachable. The running label derives from `startedAt` instead.
       */
      secs?: number;
      /** Epoch ms the run began — the live elapsed label's only input. */
      startedAt?: number;
      runId?: string;
      error?: string;
      code?: string;
    }
  // `runId` is the dedupe key `applyTurnRecord` needs: the streamed path
  // applies the finished record more than once (terminal + safety net), and an
  // untagged preview would be appended again on every pass.
  | { kind: 'preview'; id: string; preview: Preview; runId?: string }
  | { kind: 'sys'; id: string; text: string; icon?: string; code?: string }
  /**
   * The shape of the thread changed — rewound, forked, or switched branch.
   *
   * ONE kind, not three. All three mean "what you are reading is no longer the
   * only history", they render identically (a centred hairline with a label),
   * and three kinds would be three renderers for one visual.
   */
  | {
      kind: 'marker';
      id: string;
      marker: 'rewound' | 'forked' | 'branch-switch';
      text: string;
      at: number;
      branchId?: string;
    };

export function uid(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}
