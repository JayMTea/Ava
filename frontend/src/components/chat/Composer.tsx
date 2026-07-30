import { useEffect, useRef, useState } from 'react';
import type { Attachment } from '../../lib/types';
import { Icon } from '../../lib/icons';

interface Props {
  text: string;
  onText: (t: string) => void;
  pending: Attachment[];
  onRemoveAtt: (id: string) => void;
  onFiles: (files: FileList) => void;
  onSend: () => void;
  onTalk: (blob: Blob) => void;
  busy: boolean;
  codeMode: boolean;
  onToggleCode: (v: boolean) => void;
  hint: string;
  ctxTokens: number;
  ctxMax: number;
}

function fmtK(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'K';
  return String(Math.round(n));
}

function pickMime(): string {
  const opts = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/aac'];
  for (const o of opts) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(o)) return o;
  }
  return '';
}

export function Composer({
  text,
  onText,
  pending,
  onRemoveAtt,
  onFiles,
  onSend,
  onTalk,
  busy,
  codeMode,
  onToggleCode,
  hint,
  ctxTokens,
  ctxMax,
}: Props) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const [recording, setRecording] = useState(false);
  const [micError, setMicError] = useState('');

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(160, ta.scrollHeight) + 'px';
  }, [text]);

  // Release the mic stream on unmount.
  useEffect(() => {
    return () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  const startRec = async () => {
    if (busy || recording) return;
    setMicError('');
    try {
      if (!streamRef.current) {
        streamRef.current = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      }
    } catch {
      setMicError('Mic blocked — allow microphone access.');
      return;
    }
    const mime = pickMime();
    chunksRef.current = [];
    const rec = new MediaRecorder(streamRef.current, mime ? { mimeType: mime } : undefined);
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size) chunksRef.current.push(e.data);
    };
    rec.onstop = () => {
      const parts = chunksRef.current;
      const type = (parts[0] as Blob | undefined)?.type || 'audio/webm';
      const blob = new Blob(parts, { type });
      if (blob.size) onTalk(blob);
    };
    recRef.current = rec;
    rec.start();
    setRecording(true);
  };

  const stopRec = () => {
    if (!recording || !recRef.current) return;
    setRecording(false);
    try {
      recRef.current.stop();
    } catch {
      /* ignore */
    }
  };

  const toggleMic = () => (recording ? stopRec() : startRec());

  return (
    <div id="composer">
      <div id="codebar">
        <button
          type="button"
          className={'codetoggle' + (codeMode ? ' on' : '')}
          title="Code mode — Ava edits her own code with Claude"
          onClick={() => onToggleCode(!codeMode)}
        >
          <Icon name="code" />
          <span>Code mode</span>
        </button>
      </div>

      {pending.length > 0 && (
        <div id="chips">
          {pending.map((a) => (
            <div className="chip" key={a.id}>
              {a.kind === 'image' && a.url ? (
                <img src={a.url} alt={a.filename} />
              ) : (
                <div className="ic">
                  <Icon name="file" />
                </div>
              )}
              <div className="meta">
                <div className="nm">{a.filename}</div>
                <div className="sz">{a.kind === 'image' ? (a.ocr ? 'image · text read' : 'image') : `${a.chars || 0} chars`}</div>
              </div>
              <button type="button" className="x" title="Remove attachment" onClick={() => onRemoveAtt(a.id)}>
                <Icon name="close" />
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="ctxbar" title="Context-window usage for this chat">
        <div className="ctxbar-track">
          <span
            style={{
              width: Math.min(100, Math.round((ctxTokens / Math.max(1, ctxMax)) * 100)) + '%',
              background: ctxTokens / ctxMax >= 0.9 ? '#e0364d' : ctxTokens / ctxMax >= 0.7 ? '#e0a06f' : undefined,
            }}
          />
        </div>
        <span className="ctxbar-txt">
          {fmtK(ctxTokens)} / {fmtK(ctxMax)} tokens · {Math.min(100, Math.round((ctxTokens / Math.max(1, ctxMax)) * 100))}%
        </span>
      </div>

      <div className="crow">
        <textarea
          ref={taRef}
          id="text"
          placeholder={
            recording ? 'Listening… tap the mic to send' : codeMode ? 'Tell Ava what to change…' : 'Reply to Ava…'
          }
          value={text}
          autoComplete="off"
          autoCapitalize="sentences"
          onChange={(e) => onText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              if (!busy) onSend();
            }
          }}
          rows={1}
        />
        <div className="crow-tools">
          <button type="button" className="ibtn" title="Attach file" aria-label="Attach file" onClick={() => fileRef.current?.click()}>
            <Icon name="attach" />
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            accept="image/*,.pdf,.txt,.md,.markdown,.csv,.tsv,.json,.yaml,.yml,.xml,.html,.log,.doc,.docx,.odt,.rtf,.ppt,.pptx,.xls,.xlsx,.ods"
            onChange={(e) => {
              if (e.target.files?.length) onFiles(e.target.files);
              e.target.value = '';
            }}
          />
          <div className="spacer" />
          <button type="button"
            className={'ibtn mic' + (recording ? ' rec' : '')}
            title={recording ? 'Tap to send' : 'Ask Ava with your voice'}
            aria-label={recording ? 'Stop and send' : 'Voice message'}
            disabled={busy}
            onClick={toggleMic}
          >
            <Icon name="mic" />
          </button>
          <button type="button" className="ibtn send" title="Send" aria-label="Send" disabled={busy} onClick={onSend}>
            <Icon name="send" />
          </button>
        </div>
      </div>
      {/* role="status" so a mic permission failure or a transient hint is spoken
          rather than only shown — this is where "microphone blocked" surfaces. */}
      <div className="hint" id="hint" role="status">
        {micError || hint}
      </div>
    </div>
  );
}
