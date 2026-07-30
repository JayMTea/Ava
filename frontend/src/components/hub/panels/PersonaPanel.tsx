import { useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/primitives';
import { ResourceError } from '../ui/ResourceState';
import { useAction, useResource } from '../hooks';
import { hub } from '../hubApi';
import { Badge } from '../ui/Badge';
import { HubMessage } from '../ui/HubMessage';
import { Legend } from '../ui/Legend';

// Persona — how Ava talks. Blank on a fresh install, on purpose: the shipped
// prompt template carries only operational directives, so nothing about this
// assistant's voice comes from whoever wrote the repo. Presets below are STARTING
// POINTS: picking one drops its text into the editable box, and what gets saved is
// the text, never the preset name. That way editing a preset upstream can never
// retroactively change how an existing install's assistant talks.
export function PersonaPanel({ onRestart }: { onRestart: () => void }) {
  const pRes = useResource(() => hub.persona());
  const { data: p, reload } = pRes;
  const [style, setStyle] = useState('');
  const [format, setFormat] = useState('chat');
  const { busy, message, run } = useAction();

  // Seed from the server, and re-seed after a save. Nothing is sent until the
  // owner presses the button — no onChange writes through.
  useEffect(() => {
    if (!p) return;
    setStyle(p.style ?? '');
    setFormat(p.format ?? 'chat');
  }, [p]);

  const save = () => run(async () => {
    const r = await hub.savePersona({ style, format });
    if (r.error) return r.error;
    reload();
    onRestart();
  }, 'Saved — re-provision the agent in Setup → Agent for it to take effect.');

  const styleMax = p?.style_max ?? 4000;
  const overrides = p?.env_overrides ?? {};
  const overridden = Object.keys(overrides);
  const isSet = (p?.style ?? '').trim() !== '';

  return (
    <>
      <ResourceError r={pRes} label="your persona settings" />

      <Panel
        title="How Ava talks"
        subtitle="Ava ships with no personality. The prompt it starts from covers only what it must do — call its tools, never claim it rendered an image it didn't. The voice is yours to write."
      >
        {p ? (
          <>
            {overridden.length > 0 && (
              <div className="bud-monthly">
                <Icon name="alert" />
                <span>
                  Set in the environment{overridden.map((k) => <span key={k}> · <code>AVA_PERSONA_{k.toUpperCase()}</code></span>)}.
                  Environment variables outrank <code>ava.yaml</code>, so saving here will not change anything until you unset them.
                </span>
              </div>
            )}

            <div className="hub-field">
              <label>
                Start from a preset <span style={{ color: 'var(--muted)', fontWeight: 400 }}>— optional, and fully editable afterwards</span>
              </label>
              <div className="hub-opts">
                {p.presets.map((preset) => (
                  <button
                    key={preset.id}
                    type="button"
                    className={'hub-opt' + (style.trim() === preset.text.trim() ? ' sel' : '')}
                    aria-pressed={style.trim() === preset.text.trim()}
                    onClick={() => setStyle(preset.text)}
                  >
                    <b>{preset.label}</b>
                    <small style={{ color: 'var(--muted)' }}>{preset.text}</small>
                  </button>
                ))}
              </div>
            </div>

            <div className="hub-field">
              <label>
                Ava's voice, in your words
                {!isSet && <> <Badge tone="muted">not set</Badge></>}
              </label>
              <textarea
                className="hub-input"
                value={style}
                spellCheck
                maxLength={styleMax}
                placeholder="Blank = Ava speaks in the underlying model's own voice, unshaped by anyone else's taste."
                onChange={(e) => setStyle(e.target.value)}
                style={{ width: '100%', minHeight: 120, resize: 'vertical' }}
              />
              <div className="bud-field-hint">
                {style.length}/{styleMax} characters. Written straight into your prompt, so
                write it as an instruction — "dry, understated, never gushes" rather than
                "be nicer". Clear the box to go back to no styling at all.
              </div>
            </div>

            <div className="hub-field">
              <label>Answer format</label>
              <div className="hub-opts">
                {p.format_choices.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className={'hub-opt' + (format === f.id ? ' sel' : '')}
                    aria-pressed={format === f.id}
                    onClick={() => setFormat(f.id)}
                  >
                    <b>{f.label}</b>
                    <small style={{ color: 'var(--muted)' }}>{f.hint}</small>
                  </button>
                ))}
              </div>
            </div>

            <div className="hub-btn-row">
              <button type="button" className="hub-btn" onClick={save} disabled={busy}>
                <Icon name="check" />{busy ? 'Saving…' : 'Save persona'}
              </button>
            </div>
            <HubMessage message={message} />
          </>
        ) : <EmptyState text="Loading…" />}
      </Panel>

      <div className="hub-section" />
      <Panel title="What Ava keeps regardless" subtitle="These are correctness, not character — they stay in place whatever voice you choose.">
        <Legend
          title="Always in the prompt"
          items={[
            { icon: 'activity', term: 'Use the tools', desc: 'Live questions get a real tool call — weather comes from get_weather, never from memory.' },
            { icon: 'image', term: 'Never fake a render', desc: 'Ava may only say an image was created if it actually called run_gpu_job and got a confirmation.' },
            { icon: 'shield', term: 'Know its own reach', desc: <>The sandbox warns that outbound network is deny-by-default; Ava is told that this does <b>not</b> apply to its own tools, so it stops claiming it has no web access.</> },
            { icon: 'info', term: 'Takes effect on provision', desc: <>The prompt is built when the agent runtime is provisioned, so re-provision in <b>Setup → Agent</b> after saving.</> },
          ]}
        />
      </Panel>
    </>
  );
}
