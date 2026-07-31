import { useEffect, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { EmptyState, Panel } from '../../dashboard/layout';
import { ResourceError } from '../ui/ResourceState';
import { useAction, useResource } from '../hooks';
import { hub } from '../hubApi';
import { Badge } from '../ui/Badge';
import { DriftBadge } from '../ui/DriftBadge';
import { HubMessage } from '../ui/HubMessage';
import { Legend } from '../ui/Legend';
import { markProvisionDirty, startProvision, useProvisionState } from '../../../hooks/useProvisionState';

// Persona — how Ava talks. Blank on a fresh install, on purpose: the shipped
// prompt template carries only operational directives, so nothing about this
// assistant's voice comes from whoever wrote the repo. Presets below are STARTING
// POINTS: picking one drops its text into the editable box, and what gets saved is
// the text, never the preset name. That way editing a preset upstream can never
// retroactively change how an existing install's assistant talks.
// `onRestart` is deliberately gone. A persona save writes persona.style/format to
// ava.yaml and the prompt is rebuilt at PROVISION time — nothing about it needs a
// bridge restart. Calling it painted a warn-toned banner naming a
// `docker compose restart ava` the owner did not need to run, while the panel's
// own message told them to do something else entirely. Two apply verbs, one of
// them wrong. See the convention in CLAUDE.md.
export function PersonaPanel() {
  const pRes = useResource(() => hub.persona());
  const { data: p, reload } = pRes;
  const { state: prov } = useProvisionState();
  const [style, setStyle] = useState('');
  const [format, setFormat] = useState('chat');
  const [applying, setApplying] = useState(false);
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
    // The bar picks it up from here — no navigation instruction, no homework.
    markProvisionDirty('persona');
  }, 'Saved.');

  const styleMax = p?.style_max ?? 4000;
  const overrides = p?.env_overrides ?? {};
  const overridden = Object.keys(overrides);
  const isSet = (p?.style ?? '').trim() !== '';
  const personaState = prov?.scopes?.persona?.state;
  const personaStale = personaState === 'stale' || personaState === 'undeployed';

  return (
    <>
      <ResourceError r={pRes} label="your persona settings" />

      <Panel
        title="How Ava talks"
        subtitle="Ava ships with no personality. The prompt it starts from covers only what it must do — call its tools, never claim it rendered an image it didn't. The voice is yours to write."
      >
        {p ? (
          <>
            {/* Same trap as Setup → System: a broken ava.yaml still returns 200,
                so the fields below would show defaults and every save would 409.
                The route already reports config_error — render it. */}
            {p.config_error && (
              <div className="hub-msg err" style={{ marginBottom: 12 }}>
                <b>Your config file does not parse, so a persona cannot be saved.</b>
                <br />
                Fix <code>ava.yaml</code> and restart, then reload this page.
                <br />
                <small style={{ opacity: 0.85 }}>{p.config_error}</small>
              </div>
            )}
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
                {/* Two badges answering two different questions: is there a
                    value at all, and is the value the one Ava is running. */}
                {!isSet && <> <Badge tone="muted">not set</Badge></>}
                {personaState && <> <DriftBadge state={personaState} /></>}
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
            {/* The actual fix for "I saved it and nothing changed": the answer is
                here, next to the button that caused it, instead of a navigation
                instruction in a message the next action wipes. */}
            {personaStale && (
              <div className="hub-note">
                <Icon name="info" />
                <span>
                  Saved, but Ava is still using the previous version.{' '}
                  <button
                    type="button" className="hub-btn ghost sm" disabled={applying}
                    onClick={async () => {
                      setApplying(true);
                      await startProvision('persona');
                      setApplying(false);
                    }}
                  >
                    {applying ? 'Applying…' : 'Apply now'}
                  </button>
                </span>
              </div>
            )}
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
            { icon: 'info', term: 'Takes effect when you apply', desc: <>Your voice is compiled into Ava’s prompt when your changes reach the agent. Saving stages it; the bar at the top of Setup applies it in one click.</> },
          ]}
        />
      </Panel>
    </>
  );
}
