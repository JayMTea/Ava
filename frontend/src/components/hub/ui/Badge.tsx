import type { ReactNode } from 'react';
import type { Tone } from './Tile';

// A small pill with a semantic tone (see .hub-badge in hub.css). The most-used
// status atom across every Setup panel.
//
// The union is `Tone` from ui/Tile so the two atoms cannot disagree about what
// tones exist. They did: Tile accepted `info` and Badge did not, so a caller
// holding a `Tone` could not pass it here — and passing it anyway would have
// rendered an unstyled pill that silently lost its meaning. One vocabulary, and
// every value in it has a rule in hub.css.
export function Badge({ tone, children }: {
  tone?: Tone; children: ReactNode;
}) {
  return <span className={'hub-badge' + (tone ? ' ' + tone : '')}><i />{children}</span>;
}
