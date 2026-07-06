import type { Artifact } from '../../lib/types';
import { Icon } from '../../lib/icons';
import { WeatherArtifact } from './WeatherArtifact';

interface Props {
  artifact: Artifact | null;
  onClose: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}

export function ArtifactPanel({ artifact, onClose, onRefresh, refreshing }: Props) {
  return (
    <aside id="artifactPanel" aria-hidden={artifact ? 'false' : 'true'}>
      <div className="art-head">
        <div className="art-title">
          <span id="artIcon" className="art-ic">
            <Icon name={artifact?.type === 'weather' ? 'cloud' : 'panel'} />
          </span>
          <span>{artifact?.title || 'Artifact'}</span>
        </div>
        <div className="art-tools">
          <button
            className="art-ibtn"
            title="Refresh"
            aria-label="Refresh"
            onClick={onRefresh}
            disabled={refreshing}
            style={refreshing ? { opacity: 0.5 } : undefined}
          >
            <Icon name="refresh" />
          </button>
          <button className="art-ibtn" title="Close panel" aria-label="Close panel" onClick={onClose}>
            <Icon name="close" />
          </button>
        </div>
      </div>
      <div id="artBody" className="art-body">
        {artifact?.type === 'weather' && <WeatherArtifact art={artifact} />}
      </div>
      <div className="art-foot">
        <span>{artifact?.location || ''}</span>
      </div>
    </aside>
  );
}
