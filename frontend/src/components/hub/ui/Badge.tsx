import type { ReactNode } from 'react';

// A small pill with a semantic tone (see .hub-badge in hub.css). The most-used
// status atom across every Setup panel.
export function Badge({ tone, children }: {
  tone?: 'ok' | 'warn' | 'err' | 'accent' | 'muted'; children: ReactNode;
}) {
  return <span className={'hub-badge' + (tone ? ' ' + tone : '')}><i />{children}</span>;
}
