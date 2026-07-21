import type { CSSProperties } from 'react';
import type { ActionMsg } from '../hooks';

// Tone-aware panel message. The single render site for a `useAction` result, so
// success reads green and errors red — no more hardcoded `hub-msg err` that
// showed "Saved." in red. Renders nothing when there's no message.
export function HubMessage({ message, className, style }: {
  message: ActionMsg; className?: string; style?: CSSProperties;
}) {
  if (!message) return null;
  return (
    <div className={'hub-msg ' + (message.ok ? 'ok' : 'err') + (className ? ' ' + className : '')} style={style}>
      {message.text}
    </div>
  );
}
