// The chat log is modelled as an ordered list of discriminated-union items.
// Rendering is driven purely by this state (no imperative DOM), which is the
// core maintainability win of the framework migration.

import type { Artifact, Attachment, CotStep, ModelInfo, Preview } from './types';

export type ChatItem =
  | { kind: 'user'; id: string; text: string; atts: Attachment[]; failed?: boolean }
  | { kind: 'ava'; id: string; text: string; model?: ModelInfo | null; toolsUsed?: string[]; artifact?: Artifact | null; srcText: string; srcAtts: Attachment[]; audio?: string }
  | {
      kind: 'cot';
      id: string;
      label: string;
      steps: CotStep[];
      status: 'running' | 'done' | 'error';
      secs?: number;
      error?: string;
    }
  | {
      kind: 'gen';
      id: string;
      jobId: string;
      progress: number;
      status: 'running' | 'done' | 'error';
      error?: string;
      prompt?: string;
      stage?: string;
      elapsedSec?: number;
      queueHint?: string;
      cancelable?: boolean;
    }
  | { kind: 'image'; id: string; url: string; caption?: string; allowUpscale?: boolean }
  | { kind: 'preview'; id: string; preview: Preview }
  | { kind: 'sys'; id: string; text: string; icon?: string };

export function uid(): string {
  return Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}
