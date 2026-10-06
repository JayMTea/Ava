import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import type { Artifact } from '../../lib/types';
import { ArtifactPanel } from '../artifact/ArtifactPanel';
import './chat-workspace.css';

/** The visualization and composer belong to the conversation, below the app header. */
export function ChatWorkspace({ artifact, onClose, onRefresh, refreshing, children }: {
  artifact: Artifact | null;
  onClose: () => void;
  onRefresh: () => void;
  refreshing: boolean;
  children: ReactNode;
}) {
  const workspace = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(50);
  const [expanded, setExpanded] = useState(false);
  const clamp = (value: number) => Math.max(35, Math.min(65, value));
  // biome-ignore lint/correctness/useExhaustiveDependencies: each newly selected chart starts in split view.
  useEffect(() => { setExpanded(false); }, [artifact]);

  return <div ref={workspace} className={`chat-workspace${artifact ? ' has-artifact' : ''}${expanded ? ' artifact-expanded' : ''}`}
    style={{ '--chart-width': `${width}%` } as CSSProperties}>
    <div className="chat-conversation">{children}</div>
    {artifact && <>
      <div className="chat-artifact-divider" role="separator" aria-label="Resize visualization"
        aria-orientation="vertical" aria-valuemin={35} aria-valuemax={65} aria-valuenow={Math.round(width)}
        tabIndex={0} onDoubleClick={() => setWidth(50)}
        onKeyDown={event => {
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault(); setWidth(value => clamp(value + (event.key === 'ArrowLeft' ? 5 : -5)));
          } else if (['Home', 'End', 'Enter'].includes(event.key)) {
            event.preventDefault(); setWidth(event.key === 'Home' ? 35 : event.key === 'End' ? 65 : 50);
          }
        }}
        onPointerDown={event => { event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); }}
        onPointerMove={event => {
          if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
          const rect = workspace.current?.getBoundingClientRect();
          if (rect) setWidth(clamp((rect.right - event.clientX) / rect.width * 100));
        }}
        onPointerUp={event => event.currentTarget.releasePointerCapture(event.pointerId)}><span /></div>
      <ArtifactPanel artifact={artifact} onClose={onClose} onRefresh={onRefresh} refreshing={refreshing}
        expanded={expanded} onToggleExpand={() => setExpanded(value => !value)} />
    </>}
  </div>;
}
