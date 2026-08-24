import { useCallback, useEffect, useState } from 'react';
import { agentApi, type FileEntry } from '../../lib/agentApi';
import { useGateway } from '../../hooks/useGateway';
import { EmptyState } from '../dashboard/layout';
import { Tile } from '../hub/ui/Tile';

// The files this session has touched.
//
// A flat, path-sorted list rather than a tree: a tree needs expansion state,
// which needs an address to survive a reload, which is a segment the URL
// grammar deliberately does not have. Paths are already hierarchical to read.

function iconFor(f: FileEntry): string {
  if (f.dir) return 'panel';
  const p = f.path.toLowerCase();
  if (p.endsWith('.md')) return 'file';
  if (/\.(png|jpe?g|gif|webp|avif|svg)$/.test(p)) return 'image';
  if (/\.(ya?ml|json|toml|ini|conf)$/.test(p)) return 'sliders';
  return 'code';
}

function human(bytes?: number): string {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} kB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function FilesPanel({ sessionId, onOpen }: {
  sessionId: string;
  onOpen?: (path: string) => void;
}) {
  const client = useGateway();
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!client) return;
    setLoading(true);
    try {
      const got = await agentApi(client).files.list(sessionId);
      setFiles(got?.files || []);
      setError('');
    } catch (e) {
      // Surfaced, never swallowed — an empty list and an unreachable gateway
      // must not look the same.
      setError((e as Error).message || String(e));
    } finally {
      setLoading(false);
    }
  }, [client, sessionId]);

  useEffect(() => { void load(); }, [load]);

  if (error) return <p className="hub-msg err">{error}</p>;
  if (loading && !files.length) return <p className="agent-list-note">Loading files…</p>;
  if (!files.length) return <EmptyState text="This session has not touched any files." />;

  return (
    <ul className="agent-files">
      {[...files].sort((a, b) => a.path.localeCompare(b.path)).map((f) => (
        <li key={f.path}>
          <button
            type="button"
            className="agent-file"
            onClick={() => onOpen?.(f.path)}
            disabled={f.dir}
          >
            <Tile icon={iconFor(f)} tone="muted" size={22} />
            <span className="agent-file-path">{f.path}</span>
            <span className="agent-file-size">{human(f.bytes)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
