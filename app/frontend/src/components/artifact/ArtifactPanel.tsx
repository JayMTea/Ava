import { useEffect, useRef } from 'react';
import type { Artifact } from '../../lib/types';
import { Icon } from '../../lib/icons';
import { WeatherArtifact } from './WeatherArtifact';
import { AnalyticsArtifact } from './AnalyticsArtifact';
import { appIcon } from '../../lib/appColor';

interface Props {
  artifact: Artifact | null;
  onClose: () => void;
  onRefresh: () => void;
  refreshing: boolean;
  expanded: boolean;
  onToggleExpand: () => void;
}

export function ArtifactPanel({ artifact, onClose, onRefresh, refreshing, expanded, onToggleExpand }: Props) {
  const closeButton = useRef<HTMLButtonElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeButton.current?.focus({ preventScroll: true });
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); close.current(); }
    };
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('keydown', key);
      if (previous?.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  return (
    <aside className="chat-artifact-panel" aria-label="Conversation visualization">
      <div className="art-head">
        <div className="art-title">
          <span id="artIcon" className="art-ic">
            <Icon name={artifact?.type === 'analytics' ? appIcon(artifact.connector_id) : artifact?.type === 'weather' ? 'cloud' : 'panel'} />
          </span>
          <span>{artifact?.title || 'Artifact'}</span>
        </div>
        <div className="art-tools">
          <button type="button" className="art-ibtn chat-artifact-expand" title={expanded ? 'Restore split view' : 'Expand visualization'}
            aria-label={expanded ? 'Restore split view' : 'Expand visualization'} onClick={onToggleExpand}>
            <Icon name={expanded ? 'panel' : 'expand'} />
          </button>
          {artifact?.type === 'weather' && <button type="button"
            className="art-ibtn"
            title="Refresh"
            aria-label="Refresh"
            onClick={onRefresh}
            disabled={refreshing}
            style={refreshing ? { opacity: 0.5 } : undefined}
          >
            <Icon name="refresh" />
          </button>}
          <button ref={closeButton} type="button" className="art-ibtn chat-artifact-close" title="Back to chat" aria-label="Close visualization and return to chat" onClick={onClose}>
            <span className="chat-artifact-back">Back to chat</span>
            <Icon name="close" />
          </button>
        </div>
      </div>
      <div id="artBody" className="art-body">
        {artifact?.type === 'weather' && <WeatherArtifact art={artifact} />}
        {artifact?.type === 'analytics' && <AnalyticsArtifact key={artifact.id} artifact={artifact} />}
      </div>
    </aside>
  );
}
