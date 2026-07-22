import { useCallback, useRef, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/primitives';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import type { EnrollResult } from '../hubApi';
import { Badge } from '../ui/Badge';
import { StatRow } from '../ui/StatRow';

// Voice — enrollment recorder + similarity test.
function useRecorder() {
  const [recording, setRecording] = useState(false);
  const mrRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);

  const start = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mr = new MediaRecorder(stream);
    chunksRef.current = [];
    mr.ondataavailable = (e) => chunksRef.current.push(e.data);
    mr.start();
    mrRef.current = mr;
    setRecording(true);
  }, []);

  const stop = useCallback((): Promise<Blob> => new Promise((resolve) => {
    const mr = mrRef.current;
    if (!mr) return resolve(new Blob());
    mr.onstop = () => {
      mr.stream.getTracks().forEach((t) => t.stop());
      resolve(new Blob(chunksRef.current, { type: mr.mimeType || 'audio/webm' }));
    };
    mr.stop();
    setRecording(false);
  }), []);

  return { recording, start, stop };
}

const ENROLL_PHRASES = [
  'Read a few sentences naturally, like you are talking to a friend.',
  'Describe what you did today, or read a paragraph from any article.',
  'Aim for 10–15 seconds per clip. Three clips give a solid voiceprint.',
];

export function VoicePanel({ onRestart }: { onRestart: () => void }) {
  // Status loads via the shared hook; the recording flow below keeps its own
  // busy/msg state — it's a bespoke state machine (clip capture, mic errors),
  // not a one-shot action, and all its messages are errors.
  const { data: st, reload: load } = useResource(() => hub.voiceStatus());
  const [clips, setClips] = useState<Blob[]>([]);
  const [result, setResult] = useState<EnrollResult | null>(null);
  const [testSim, setTestSim] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const rec = useRecorder();
  const [mode, setMode] = useState<'enroll' | 'test' | null>(null);

  const toggleRecord = useCallback(async (m: 'enroll' | 'test') => {
    setMsg('');
    if (typeof MediaRecorder === 'undefined') {
      setMsg('This browser cannot record audio in-page — enroll from a file with enroll_from_file.py instead.');
      return;
    }
    if (rec.recording) {
      const blob = await rec.stop();
      setMode(null);
      if (blob.size < 1000) { setMsg('Recording too short — try again.'); return; }
      if (m === 'enroll') setClips((c) => [...c, blob]);
      else {
        setBusy(true);
        try {
          const r = await hub.voiceTest(blob);
          if (r.ok && r.similarity != null) setTestSim(r.similarity);
          else setMsg(r.error || 'test failed');
        } catch (e) { setMsg((e as Error).message); }
        setBusy(false);
      }
    } else {
      setTestSim(null);
      try { setMode(m); await rec.start(); }
      catch (e) {
        setMode(null);
        setMsg((e as Error)?.name === 'NotAllowedError'
          ? 'Microphone access denied — allow the mic for this site and retry.'
          : `Could not start recording: ${(e as Error).message}`);
      }
    }
  }, [rec]);

  const applyThreshold = useCallback(async (v: number) => {
    setBusy(true); setMsg('');
    try {
      const r = await hub.voiceThreshold(v);
      if (r.error) setMsg(r.error);
      else { onRestart(); load(); }
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [onRestart, load]);

  const enroll = useCallback(async () => {
    setBusy(true); setMsg(''); setResult(null);
    try {
      const r = await hub.voiceEnroll(clips);
      if (r.ok) { setResult(r); setClips([]); load(); }
      else setMsg(r.error || 'enrollment failed');
    } catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [clips, load]);

  const enableVoice = useCallback(async () => {
    setBusy(true);
    try { await hub.save({ features: { voice: true } }); load(); onRestart(); }
    catch (e) { setMsg((e as Error).message); }
    setBusy(false);
  }, [load, onRestart]);

  return (
    <>
      <Panel
        title="Voice & biometric gate"
        subtitle="Everything runs on your machine: local speech-to-text, local TTS, and a speaker-verification gate so Ava answers your voice only."
        right={st ? (
          !st.enabled ? <Badge tone="muted">voice off</Badge>
            : st.enrolled ? <Badge tone="ok">gate closed</Badge>
              : <Badge tone="err">gate open</Badge>
        ) : null}
      >
        {st == null ? <EmptyState text="Loading voice status…" /> : (
          <div className="stat-rows">
            <StatRow label="Voice feature" tone={st.enabled ? 'ok' : 'muted'}
              value={st.enabled ? 'on' : (
                <>off<button className="hub-btn ghost sm" onClick={enableVoice} disabled={busy}>Enable</button></>
              )} />
            <StatRow label="Voiceprint" tone={st.enrolled ? 'ok' : 'warn'}
              value={st.enrolled ? 'enrolled' : 'not enrolled — record clips below'} />
            <StatRow label="Dependencies" tone={st.deps_ok ? 'ok' : 'warn'}
              value={st.deps_ok ? 'installed' : (st.deps_error || 'missing')} />
            <StatRow label="Gate threshold" tone="muted"
              value={<>{st.threshold} <span style={{ color: 'var(--muted)' }}>cosine similarity · set from enrollment, or voice.threshold in ava.yaml</span></>} />
          </div>
        )}
        {st?.enabled && !st.enrolled && (
          <div className="hub-restart" style={{ marginTop: 14, marginBottom: 0 }}>
            <Icon name="alert" />
            <span><b>The gate is open:</b> voice is on but no voiceprint is enrolled, so
            {' '}<b>anyone</b> can talk to Ava. Enroll below to close it.</span>
          </div>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Enroll your voice" subtitle="Record a few clips of natural speech; Ava builds an averaged voiceprint (nothing is uploaded anywhere — it stays on this machine).">
        <ul className="voice-tips">
          {ENROLL_PHRASES.map((p, i) => <li key={i}>{p}</li>)}
        </ul>

        <div className="hub-btn-row">
          <button
            className={'hub-btn' + (rec.recording && mode === 'enroll' ? '' : ' ghost')}
            onClick={() => toggleRecord('enroll')}
            disabled={busy || !st?.deps_ok || (rec.recording && mode !== 'enroll')}
          >
            <Icon name="mic" />{rec.recording && mode === 'enroll' ? 'Stop recording' : `Record clip ${clips.length + 1}`}
          </button>
          {clips.length > 0 && (
            <button className="hub-btn" onClick={enroll} disabled={busy || rec.recording}>
              <Icon name="check" />{busy ? 'Building voiceprint…' : `Build voiceprint from ${clips.length} clip${clips.length === 1 ? '' : 's'}`}
            </button>
          )}
          {clips.length > 0 && !rec.recording && (
            <button className="hub-btn ghost" onClick={() => setClips([])} disabled={busy}>
              <Icon name="trash" />Discard clips
            </button>
          )}
        </div>

        {rec.recording && mode === 'enroll' && (
          <div className="hub-msg" style={{ color: 'var(--err)' }}>● Recording — speak naturally, then Stop.</div>
        )}
        {clips.length > 0 && !rec.recording && (
          <div className="hub-msg" style={{ color: 'var(--muted)' }}>
            {clips.length} clip{clips.length === 1 ? '' : 's'} ready{clips.length < 3 ? ' — 3+ recommended' : ''}.
          </div>
        )}

        {result && (
          <div className="hub-note" style={{ marginTop: 12 }}>
            <b>Voiceprint saved.</b> {result.seconds}s of audio → {result.windows} voice windows
            {result.dropped ? ` (${result.dropped} outliers dropped)` : ''}.
            Consistency {result.consistency?.mean}.{' '}
            Suggested threshold: <b>{result.suggested_threshold}</b>
            {result.suggested_threshold != null && (
              <button className="hub-btn sm" style={{ marginLeft: 10 }} disabled={busy}
                onClick={() => applyThreshold(result.suggested_threshold!)}>
                <Icon name="check" />Apply threshold
              </button>
            )}
            {result.low_consistency && <div style={{ color: 'var(--warn)', marginTop: 4 }}>Consistency is a bit low — re-record in a quieter room for a stronger gate.</div>}
          </div>
        )}
      </Panel>

      <div className="hub-section" />
      <Panel title="Test the gate" subtitle="Record a short clip and see how it scores against the enrolled voiceprint.">
        <div className="hub-btn-row" style={{ marginTop: 0 }}>
          <button
            className={'hub-btn' + (rec.recording && mode === 'test' ? '' : ' ghost')}
            onClick={() => toggleRecord('test')}
            disabled={busy || !st?.deps_ok || !st?.enrolled || (rec.recording && mode !== 'test')}
          >
            <Icon name="mic" />{rec.recording && mode === 'test' ? 'Stop & score' : 'Record test clip'}
          </button>
        </div>
        {testSim != null && st && (
          <div className="hub-msg" style={{ fontSize: 'var(--fs-md)' }}>
            Similarity <b style={{ color: testSim >= st.threshold ? 'var(--ok)' : 'var(--err)' }}>{testSim}</b>
            {' '}vs threshold {st.threshold} — {testSim >= st.threshold
              ? <span style={{ color: 'var(--ok)' }}>Ava would answer this voice.</span>
              : <span style={{ color: 'var(--err)' }}>Ava would ignore this voice.</span>}
          </div>
        )}
        {!st?.enrolled && <div className="hub-msg" style={{ color: 'var(--muted)' }}>Enroll a voiceprint first.</div>}
      </Panel>
      {msg && <div className="hub-msg err">{msg}</div>}
    </>
  );
}
