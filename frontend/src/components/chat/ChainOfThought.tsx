import { useState } from 'react';
import type { CotStep } from '../../lib/types';
import { Icon } from '../../lib/icons';

interface Props {
  label: string;
  steps: CotStep[];
  status: 'running' | 'done' | 'error';
  secs?: number;
  error?: string;
}

export function ChainOfThought({ label, steps, status, secs, error }: Props) {
  const [open, setOpen] = useState(true);
  const cls =
    'cot' + (open && status === 'running' ? ' open' : '') + (status === 'done' ? ' cot-done' : '') + (status === 'error' ? ' cot-fail' : '');

  let headText: string;
  if (status === 'done') {
    // Live turns carry an elapsed time; replayed/persisted chains don't, so fall
    // back to a plain label instead of rendering "Thought for undefineds".
    const lead = typeof secs === 'number' ? `Thought for ${secs}s` : 'Reasoning';
    headText = lead + (steps.length ? ` · ${steps.length} steps` : '');
  } else if (status === 'error') headText = `Failed: ${error || 'failed'}`;
  else headText = `${label}…`;

  return (
    <div className={cls}>
      {/* A button, not a div: this is the only affordance for opening Ava's
          reasoning on the primary surface, and as a div it could not be reached
          or activated by keyboard at all. .cot-head carries the UA reset so the
          rendering is unchanged. */}
      <button
        type="button"
        className="cot-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="cot-spin" />
        <span className="cot-lab">{headText}</span>
      </button>
      <div className="cot-body">
        {steps.map((s, i) =>
          s.kind === 'tool' ? (
            <div className="cstep tool" key={i}>
              <Icon name="image" />
              <span>{'Using ' + (s.name || 'tool').replace(/^.*__/, '').replace(/_/g, ' ')}</span>
            </div>
          ) : s.kind === 'thinking' ? (
            <div className="cstep think" key={i}>
              {s.text}
            </div>
          ) : (
            <div className="cstep say" key={i}>
              {s.text}
            </div>
          ),
        )}
      </div>
    </div>
  );
}
