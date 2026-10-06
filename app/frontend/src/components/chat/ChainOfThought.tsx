import { useState } from 'react';
import type { CotStep } from '../../lib/types';
import { Icon } from '../../lib/icons';
import { fixForCode } from '../../lib/fixes';
import { FixLink } from '../../lib/FixLink';
import { MediaCard } from './Media';

interface Props {
  label: string;
  steps: CotStep[];
  status: 'running' | 'done' | 'error';
  secs?: number;
  error?: string;
  /** Machine-readable failure code, if the backend knew one. Drives the same
   *  fix-it link that system messages already get — this was the last failure
   *  surface without one, and it is the one a first message lands on. */
  code?: string;
  /** Opens an image attachment in the lightbox. */
  onOpen?: (url: string) => void;
}

function toolLabel(name?: string): string {
  return (name || 'tool').replace(/^.*__/, '').replace(/_/g, ' ');
}

function fmtArgs(args: unknown): string {
  if (typeof args === 'string') return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

// A tool call. A bare call is one "Using <tool>" line; once its result folds in
// (chatEvents.foldStep) it grows a collapsible "Tool output" card carrying the
// arguments, the output text and any media the tool produced — the shape the
// OpenClaw Control UI shows and the reason this pipeline exists.
function ToolStep({ step, onOpen }: { step: CotStep; onOpen?: (url: string) => void }) {
  const label = 'Using ' + toolLabel(step.name);
  const hasMedia = !!(step.attachments && step.attachments.length);
  const hasBody = !!(step.output || step.args != null || hasMedia);
  if (!hasBody) {
    return (
      <div className="cstep tool">
        <Icon name="image" />
        <span>{label}</span>
      </div>
    );
  }
  return (
    <details className={'cstep tool tool-card' + (step.is_error ? ' tool-err' : '')}>
      <summary className="tool-sum">
        <Icon name="image" />
        <span className="tool-lab">{step.is_error ? 'Tool error · ' : 'Tool output · '}{toolLabel(step.name)}</span>
      </summary>
      <div className="tool-body">
        {step.args != null && (
          <pre className="tool-args"><code>{fmtArgs(step.args)}</code></pre>
        )}
        {step.output && (
          <pre className="tool-out"><code>{step.output}</code></pre>
        )}
        {hasMedia && (
          <div className="tool-media">
            {step.attachments!.map((m, i) => (
              <MediaCard key={`${m.url}:${i}`} media={m} onOpen={onOpen} />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

export function ChainOfThought({ label, steps, status, secs, error, code, onOpen }: Props) {
  const fix = status === 'error' ? fixForCode(code) : undefined;
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
      {/* Outside the button: a link inside a button is not activatable and the
          nesting is invalid. */}
      {fix && <div className="gen-fixrow"><FixLink fix={fix} /></div>}
      <div className="cot-body">
        {steps.map((s, i) =>
          s.kind === 'tool' || s.kind === 'tool_result' ? (
            <ToolStep key={i} step={s} onOpen={onOpen} />
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
