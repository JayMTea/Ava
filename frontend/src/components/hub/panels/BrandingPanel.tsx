import { useEffect, useState } from 'react';
import { markProvisionDirty } from '../../../hooks/useProvisionState';
import { applyBrand, checkAccentLocal, getBrand, restoreBrand, setBrand } from '../../../lib/brand';
import { Icon } from '../../../lib/icons';
import { Panel } from '../../dashboard/layout';
import { useAction, useResource } from '../hooks';
import { hub } from '../hubApi';
import { Badge } from '../ui/Badge';
import { ResourceError } from '../ui/ResourceState';

// Branding — how Ava LOOKS. Persona is how it talks; this is the other half,
// which is why it sits directly after that tab.
//
// Every field is blank by default and blank means Ava's shipped look, so
// "Reset to Ava's defaults" is an ordinary save of empty strings rather than a
// special code path that can rot.
//
// The preview writes the DOM through the SAME applyBrand() the pre-paint boot
// script mirrors, so what you see while dragging is what you get after a
// reload. It deliberately does NOT persist: only a successful save calls
// setBrand(), so a colour the server refuses cannot survive a refresh.

// Starting points, not a palette. Ava's own accent is NOT listed here — it comes
// from the server's `defaults.accent`, so this file never becomes a second place
// the shipped colour is written down (tests/test_brand_tokens.py enforces that).
const SWATCHES = [
  '#2f7d4f', '#8b1a3d', '#6b46c1', '#b35c1e', '#0b7285', '#b03572', '#3b5bdb',
];


/** One image slot. Kept dumb — every decision (which slots exist, what is set)
 *  comes from the server's `limits.slots` and `assets`. */
function AssetRow({ slot, label, hint, url, busy, onPick, onClear }: {
  slot: string; label: string; hint: string; url: string | null;
  busy: boolean; onPick: (slot: string, f: File) => void; onClear: (slot: string) => void;
}) {
  const inputId = `brand-asset-${slot}`;
  return (
    <div className="hub-field">
      <label htmlFor={inputId}>
        {label} {!url && <Badge tone="muted">not set</Badge>}
      </label>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        {url && (
          // The cache-buster matters: the slot URL is stable, so a re-upload
          // would otherwise show the previous image until a hard reload.
          <img
            src={`${url}?v=${Date.now()}`}
            alt=""
            width={40}
            height={40}
            style={{ objectFit: 'contain', borderRadius: 6, background: 'var(--panel2)' }}
          />
        )}
        <input
          id={inputId}
          type="file"
          accept="image/png,image/webp,image/jpeg"
          disabled={busy}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onPick(slot, f);
            e.target.value = '';   // let the same file be re-picked after an error
          }}
        />
        {url && (
          <button type="button" className="hub-btn ghost" disabled={busy}
                  onClick={() => onClear(slot)}>
            Remove
          </button>
        )}
      </div>
      <div className="bud-field-hint">{hint}</div>
    </div>
  );
}

export function BrandingPanel() {
  const bRes = useResource(() => hub.branding());
  const { data: b, reload } = bRes;
  const { busy, message, run } = useAction();

  const [name, setName] = useState('');
  const [tagline, setTagline] = useState('');
  const [accent, setAccent] = useState('');
  const [chrome, setChrome] = useState('');
  const [isPublic, setIsPublic] = useState(true);
  const [dirty, setDirty] = useState(false);
  // The server's verdict on the LAST attempted save. It carries the suggestion,
  // which the client-side check deliberately does not compute — walking OKLab
  // belongs in one place, and that place is the authority.
  const [rejected, setRejected] = useState<{ blocking: string[]; suggest: string } | null>(null);

  // Seed from the server, and re-seed after a save. Nothing is sent until the
  // owner presses the button — the preview below is DOM-only.
  useEffect(() => {
    if (!b) return;
    setName(b.name ?? '');
    setTagline(b.tagline ?? '');
    setAccent(b.accent ?? '');
    setChrome(b.chrome ?? '');
    setIsPublic(b.public ?? true);
    setDirty(false);
  }, [b]);

  // Live preview, debounced. Writes only the DOM.
  useEffect(() => {
    if (!dirty) return;
    const t = setTimeout(() => {
      applyBrand({ ...getBrand(), accent, accent_light: accent, chrome, name: name || 'Ava' });
    }, 80);
    return () => clearTimeout(t);
  }, [accent, chrome, name, dirty]);

  // Abandoning the panel must not leave a phantom accent behind. Without this,
  // navigating away mid-edit keeps the previewed colour until a reload.
  useEffect(() => () => restoreBrand(), []);

  if (!b) return <ResourceError r={bRes} label="your branding settings" />;

  const d = b.defaults;
  const lim = b.limits;
  const overridden = Object.keys(b.env_overrides ?? {});
  // What is TYPED, not what is stored. Reading b.contrast here meant the numbers
  // described the SAVED colour, so typing an unreadable one showed the old
  // colour's verdict until you pressed Save and got a refusal out of nowhere.
  const local = checkAccentLocal(accent);
  const contrast = local
    ? { ...local, suggest: rejected?.suggest ?? '' }
    : { ok: b.contrast.ok, ratios: b.contrast.ratios,
        blocking: b.contrast.blocking, suggest: b.contrast.suggest };

  const save = (patch: Record<string, unknown>, ok: string,
                after?: () => void) =>
    run(async () => {
      // req() THROWS on a non-2xx — it does not return {error}. The refusal
      // carries structured detail (ratios + a suggested colour) on err.detail,
      // which is captured here and then re-thrown so useAction still shows the
      // message the way every other panel does.
      try {
        await hub.saveBranding(patch);
      } catch (e) {
        const detail = (e as { detail?: { contrast?: { blocking: string[]; suggest: string } } }).detail;
        if (detail?.contrast) {
          setRejected({ blocking: detail.contrast.blocking, suggest: detail.contrast.suggest });
        }
        throw e;
      }
      setRejected(null);
      // Only now does it persist and reach the other tabs.
      setBrand({
        ...getBrand(),
        name: (patch.name as string) ?? name ?? 'Ava',
        accent: (patch.accent as string) ?? accent,
        accent_light: (patch.accent_light as string) ?? accent,
        chrome: (patch.chrome as string) ?? chrome,
        branded: Boolean((patch.accent as string) ?? accent),
      });
      reload();
      after?.();
      return undefined;
    }, ok);

  const saveAll = () => {
    // The NAME is the only field the sandbox holds: render_persona.py bakes it
    // into the agent's system prompt at provision time. Colours and images never
    // reach the agent, so they are done the moment the save returns.
    //
    // Two apply verbs, never conflated (CLAUDE.md): this is "apply to the agent",
    // so it marks the persona scope dirty and lets PendingChangesBar say so —
    // it does NOT call onRestart(), because the backend really does return
    // restart_required: false and there is nothing to restart.
    const nameChanged = b.name !== name;
    return save(
      { name, tagline, accent, accent_light: accent, chrome, public: isPublic },
      'Saved.',
      nameChanged ? () => markProvisionDirty('persona') : undefined,
    );
  };

  const pickAsset = (slot: string, f: File) =>
    run(async () => {
      await hub.uploadBrandAsset(slot, f);
      reload();
      // The icon set and the manifest derive from these, so the cached brand has
      // to learn the slot is filled or the header keeps rendering the wordmark
      // as text until the next reload.
      setBrand({ ...getBrand(), assets: { ...getBrand().assets, [slot]: `/brand/asset/${slot}` } });
      return undefined;
    }, 'Uploaded.');

  const clearAsset = (slot: string) =>
    run(async () => {
      await hub.clearBrandAsset(slot);
      reload();
      const assets = { ...getBrand().assets };
      delete assets[slot];
      setBrand({ ...getBrand(), assets });
      return undefined;
    }, 'Removed.');

  const ASSETS: { slot: string; label: string; hint: string }[] = [
    { slot: 'logo', label: 'Logo',
      hint: 'A square mark. Shown on the sign-in card, and used for the favicon and home-screen icon unless you set one below.' },
    { slot: 'wordmark', label: 'Wordmark',
      hint: 'A wide lockup for the sidebar. Blank renders the name as text, which is how Ava ships.' },
    { slot: 'icon', label: 'App icon',
      hint: 'At least 512x512, square. Every icon size is rendered from this one — favicon, home-screen tile, and the maskable icon phones crop to a circle.' },
  ];

  const importPack = (f: File) =>
    run(async () => {
      const r = await hub.importBrandPack(f);
      reload();
      // The pack may have changed colours AND images, so re-read rather than
      // patching the cache field by field.
      const fresh = await hub.branding();
      setBrand({
        ...getBrand(),
        name: fresh.name, accent: fresh.accent,
        accent_light: fresh.accent_light || fresh.accent,
        chrome: fresh.chrome, branded: fresh.branded,
      });
      return r.ok ? undefined : 'import failed';
    }, 'Brand pack applied.');

  const resetAll = () => {
    if (!window.confirm("Reset every branding value to Ava's defaults?")) return;
    save({ name: '', tagline: '', accent: '', accent_light: '', chrome: '', public: true },
         "Reset to Ava's defaults.");
  };

  return (
    <>
      <ResourceError r={bRes} label="your branding settings" />

      <Panel
        title="Make it yours"
        subtitle="Ava ships one look. Change the name it answers to, the colour it uses, and — soon — its logo. Nothing here costs anything: re-branding your own install is the point of running it yourself."
      >
        {b.config_error && (
          <div className="hub-msg err" style={{ marginBottom: 12 }}>
            <b>Your config file does not parse, so branding cannot be saved.</b>
            <br />
            Fix <code>ava.yaml</code> and restart, then reload this page.
            <br />
            <small style={{ opacity: 0.85 }}>{b.config_error}</small>
          </div>
        )}

        {overridden.length > 0 && (
          <div className="bud-monthly">
            <Icon name="alert" />
            <span>
              Set in the environment
              {overridden.map((k) => (
                <span key={k}> · <code>{b.env_overrides[k]}</code></span>
              ))}
              . Environment variables outrank <code>ava.yaml</code>, so saving here will not
              change them until they are unset — if this instance is managed for you, that is
              your administrator's doing.
            </span>
          </div>
        )}

        <div className="hub-field">
          <label>
            Name {name === '' && <Badge tone="muted">using “{d.name}”</Badge>}
          </label>
          <input
            className="hub-input"
            value={name}
            maxLength={lim.name_max}
            placeholder={d.name}
            onChange={(e) => { setName(e.target.value); setDirty(true); }}
          />
          <div className="bud-field-hint">
            Reaches the header, the sign-in page, the browser tab, the home-screen icon and
            the assistant's own sense of what it is called. Clear it to go back to “{d.name}”.
          </div>
        </div>

        <div className="hub-field">
          <label>Tagline</label>
          <input
            className="hub-input"
            value={tagline}
            maxLength={lim.tagline_max}
            placeholder={d.tagline}
            onChange={(e) => { setTagline(e.target.value); setDirty(true); }}
          />
          <div className="bud-field-hint">
            {tagline.length}/{lim.tagline_max}. Shown on the sign-in card and as the
            installed app's description.
          </div>
        </div>

        <div className="hub-field">
          <label>
            Accent colour {accent === '' && <Badge tone="muted">using Ava's blue</Badge>}
          </label>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <input
              type="color"
              className="hub-input"
              aria-label="Accent colour"
              value={accent || d.accent}
              onChange={(e) => { setAccent(e.target.value); setDirty(true); }}
              style={{ width: 52, height: 34, padding: 2, cursor: 'pointer' }}
            />
            <input
              className="hub-input"
              value={accent}
              placeholder={d.accent}
              spellCheck={false}
              onChange={(e) => { setAccent(e.target.value.trim()); setDirty(true); }}
              style={{ width: 120, fontFamily: 'var(--mono, monospace)' }}
            />
            {[d.accent, ...SWATCHES].map((c) => (
              <button
                key={c}
                type="button"
                aria-label={`Use ${c}`}
                className={'hub-opt' + (accent.toLowerCase() === c ? ' sel' : '')}
                onClick={() => { setAccent(c); setDirty(true); }}
                style={{ background: c, width: 26, height: 26, padding: 0, borderRadius: 6 }}
              />
            ))}
          </div>
          <div className="bud-field-hint">
            Hover, focus rings, chart series and every faint tint derive from this one value,
            in both light and dark. Clear the box to go back to Ava's blue.
          </div>
        </div>

        {/* Contrast is reported as NUMBERS, not a verdict, because the thresholds
            are asymmetric for a real reason and a bare "invalid" would look like
            a bug. Ava's own blue passes text-on-accent by 0.01. */}
        {accent && (
          <div className={'hub-msg ' + (contrast.ok ? '' : 'err')} style={{ marginBottom: 12 }}>
            <b>{contrast.ok ? 'Readable' : 'Not readable enough to save'}</b>
            <div style={{ marginTop: 4, fontSize: 12, opacity: 0.9 }}>
              white text on it {contrast.ratios.text_on_accent?.toFixed(2)}:1
              {' · '}on the dark canvas {contrast.ratios.accent_on_dark?.toFixed(2)}:1
              {' · '}on the light canvas {contrast.ratios.accent_on_light?.toFixed(2)}:1
            </div>
            {(rejected?.blocking.length ? rejected.blocking : contrast.blocking)
              .map((x) => <div key={x} style={{ marginTop: 4 }}>· {x}</div>)}
            {(rejected?.suggest || contrast.suggest) && (
              <button
                type="button"
                className="hub-btn"
                style={{ marginTop: 8 }}
                onClick={() => { setAccent(rejected?.suggest || contrast.suggest); setDirty(true); }}
              >
                Use {rejected?.suggest || contrast.suggest} instead
              </button>
            )}
          </div>
        )}

        <div className="hub-field">
          <label>
            Chrome colour {chrome === '' && <Badge tone="muted">matching the theme</Badge>}
          </label>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              type="color"
              className="hub-input"
              aria-label="Chrome colour"
              value={chrome || d.chrome_dark}
              onChange={(e) => { setChrome(e.target.value); setDirty(true); }}
              style={{ width: 52, height: 34, padding: 2, cursor: 'pointer' }}
            />
            <input
              className="hub-input"
              value={chrome}
              placeholder={d.chrome_dark}
              spellCheck={false}
              onChange={(e) => { setChrome(e.target.value.trim()); setDirty(true); }}
              style={{ width: 120, fontFamily: 'var(--mono, monospace)' }}
            />
          </div>
          <div className="bud-field-hint">
            The browser or phone status bar around the app, and the splash screen when it is
            installed. Blank follows the theme's own canvas.
          </div>
        </div>

        <div className="hub-field">
          <label>
            <input
              type="checkbox"
              checked={isPublic}
              onChange={(e) => { setIsPublic(e.target.checked); setDirty(true); }}
              style={{ marginRight: 8 }}
            />
            Show the brand on the sign-in page
          </label>
          <div className="bud-field-hint">
            Anyone who can reach this address sees the sign-in page. Off means it renders
            Ava's own default instead — not a blank card, which would only advertise that
            something is being hidden.
          </div>
        </div>

        {ASSETS.map((a) => (
          <AssetRow
            key={a.slot}
            slot={a.slot}
            label={a.label}
            hint={a.hint}
            url={b.assets?.[a.slot]?.url ?? null}
            busy={busy}
            onPick={pickAsset}
            onClear={clearAsset}
          />
        ))}

        <div className="hub-field">
          <label htmlFor="brand-pack">Brand pack</label>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
            <a className="hub-btn ghost" href="/api/hub/branding/export" download="brand.zip">
              Export
            </a>
            <input
              id="brand-pack"
              type="file"
              accept=".zip,application/zip"
              disabled={busy}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) importPack(f);
                e.target.value = '';
              }}
            />
          </div>
          <div className="bud-field-hint">
            One file holding the colours and images above, so a brand can be moved between
            installs or produced somewhere else. Imported packs are validated exactly like a
            manual upload — a pack can set only branding, never anything else about your
            server.
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <button type="button" className="hub-btn primary" disabled={busy} onClick={saveAll}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button type="button" className="hub-btn ghost" disabled={busy} onClick={resetAll}>
            Reset to Ava's defaults
          </button>
          {message && (
            <span className={'hub-msg ' + (message.ok ? 'ok' : 'err')}>{message.text}</span>
          )}
        </div>
      </Panel>
    </>
  );
}
