import { useCallback, useEffect, useState } from 'react';
import { agentApi } from '../../lib/agentApi';
import { CodeArea } from '../../lib/CodeArea';
import { useGateway } from '../../hooks/useGateway';
import { EmptyState } from '../dashboard/layout';
import { FilesPanel } from './FilesPanel';
import { Badge } from '../hub/ui/Badge';

const MAX_INLINE_IMAGE = 256 * 1024;

/**
 * Review a file from this session.
 *
 * With no file chosen this renders the file list IN PLACE rather than telling
 * you to go to another tab. `#agent/s/<id>/review` is a bookmarkable address —
 * somebody can be sent it — and an address that renders "go somewhere else" is
 * an address that does not work. The panel is addressable, so it has to be
 * self-sufficient.
 */
export function ReviewPanel({ sessionId, path, onPick }: {
  sessionId: string;
  path?: string;
  onPick?: (path: string) => void;
}) {
  const client = useGateway();
  const [text, setText] = useState('');
  const [saved, setSaved] = useState('');
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!client || !path) return;
    try {
      const got = await agentApi(client).files.read(sessionId, path);
      setText(got?.content || '');
      setSaved(got?.content || '');
      setError('');
      setConflict(false);
    } catch (e) {
      setError((e as Error).message || String(e));
    }
  }, [client, sessionId, path]);

  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    if (!client || !path) return;
    setBusy(true);
    try {
      // READ-BEFORE-WRITE, because `agents.files.set` has no compare-and-swap
      // token — verified against the live gateway. Without this a save is an
      // unconditional overwrite of whatever arrived while the file was open.
      //
      // Honest about what it is: this narrows the window, it does not close
      // it. Nothing stops a write landing between this read and ours. It
      // catches the case that actually happens — the agent edited the file
      // while the owner had it open — and that is worth having.
      const fresh = await agentApi(client).files.read(sessionId, path);
      if ((fresh?.content ?? '') !== saved) {
        setConflict(true);
        return;
      }
      await agentApi(client).files.write(sessionId, path, text);
      setSaved(text);
      setError('');
      setConflict(false);
    } catch (e) {
      const err = e as { code?: string; message?: string };
      // Compare-and-swap: the file changed under us. This is NOT a generic
      // error — the owner's edits are still in the box and the only wrong move
      // is to lose them, so it gets its own state and its own wording.
      // The gateway cannot report a conflict itself (no CAS token), so this
      // only fires if some future version starts to. Kept because the branch
      // above is the one that detects it today, and both end in the same
      // state the owner needs.
      if (err.code === 'conflict' || /conflict|version/i.test(err.message || '')) {
        setConflict(true);
      } else {
        setError(err.message || String(e));
      }
    } finally {
      setBusy(false);
    }
  };

  if (!path) {
    return (
      <div className="agent-review">
        <p className="agent-list-note">Pick a file to review.</p>
        <FilesPanel sessionId={sessionId} onOpen={(p) => onPick?.(p)} />
      </div>
    );
  }

  const dirty = text !== saved;
  const isImage = /\.(png|jpe?g|gif|webp|avif)$/i.test(path);

  return (
    <div className="agent-review">
      <div className="agent-review-head">
        <span className="agent-review-path" title={path}>{path}</span>
        {conflict && <Badge tone="err">changed on disk</Badge>}
        {!conflict && dirty && <Badge tone="warn">unsaved</Badge>}
        {!conflict && !dirty && <Badge tone="ok">saved</Badge>}
        <button type="button" className="hub-btn" onClick={save}
                disabled={busy || !dirty || conflict}>
          Save
        </button>
      </div>

      {error && <p className="hub-msg err">{error}</p>}
      {conflict && (
        <p className="hub-note">
          This file changed since you opened it, so saving would overwrite
          whatever changed. Your edits are still here — copy anything you need,
          then reload.{' '}
          <button type="button" className="hub-btn ghost" onClick={load}>
            Reload from disk
          </button>
        </p>
      )}

      {isImage ? (
        text.length <= MAX_INLINE_IMAGE
          // A data URL, so no dependency and no second request. The cap is the
          // upstream one: past it an inline image is a memory problem rather
          // than a preview.
          ? <img className="agent-review-img" alt={path} src={text} />
          : <EmptyState text="This image is too large to preview inline." />
      ) : (
        <CodeArea
          value={text}
          onChange={setText}
          onSave={save}
          label={path}
        />
      )}
    </div>
  );
}
