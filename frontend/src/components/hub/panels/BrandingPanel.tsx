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


/** One image slot: a two-way toggle between Ava's shipped mark and the owner's
 *  own, with both previews visible so the choice is a picture rather than a
 *  filename. Kept dumb — every decision (which slots exist, what is in force,
 *  what is parked, what the default looks like) comes from the server.
 *
 *  "Ava default" is never disabled: it is always somewhere to go back to. The
 *  "Yours" side is disabled only when there is nothing to go back TO — no image
 *  in force and none parked — and in that state the file picker is the control,
 *  because a toggle with an empty destination would be a dead switch. */
function AssetRow({ slot, label, hint, url, stashed, defaultUrl, busy,
                   onPick, onClear, onSource }: {
  slot: string; label: string; hint: string; url: string | null;
  stashed: boolean; defaultUrl: string; busy: boolean;
  onPick: (slot: string, f: File) => void;
  onClear: (slot: string) => void;
  onSource: (slot: string, source: 'default' | 'custom') => void;
}) {
  const inputId = `brand-asset-${slot}`;
  const usingCustom = Boolean(url);
  const hasOwn = usingCustom || stashed;

  const Preview = ({ src, on }: { src: string; on: boolean }) => (
    <img
      // The cache-buster matters on the OWNER's slot URL only: it is stable
      // across re-uploads, so without it a replacement shows the previous image
      // until a hard reload. Ava's default is a static file that never changes,
      // and busting it would defeat its cache headers on every render.
      src={src.startsWith('/brand/') ? `${src}?v=${Date.now()}` : src}
      alt=""
      width={40}
      height={40}
      style={{
        objectFit: 'contain', borderRadius: 6, background: 'var(--panel2)',
        opacity: on ? 1 : 0.4, transition: 'opacity .12s',
      }}
    />
  );

  return (
    <div className="hub-field">
      <label htmlFor={hasOwn ? undefined : inputId}>
        {label}{' '}
        {!usingCustom && <Badge tone="muted">Ava's default</Badge>}
      </label>

      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        {defaultUrl && <Preview src={defaultUrl} on={!usingCustom} />}

        <div className="hub-opts" role="group" aria-label={`${label} source`}>
          <button
            type="button"
            className={'hub-opt' + (usingCustom ? '' : ' sel')}
            aria-pressed={!usingCustom}
            disabled={busy || !usingCustom}
            onClick={() => onSource(slot, 'default')}
          >
            Ava default
          </button>
          <button
            type="button"
            className={'hub-opt' + (usingCustom ? ' sel' : '')}
            aria-pressed={usingCustom}
            disabled={busy || !hasOwn || usingCustom}
            onClick={() => onSource(slot, 'custom')}
          >
            Yours
          </button>
        </div>

        {url && <Preview src={url} on />}
      </div>

      <div style={{ display: 'flex', gap: 10, alignItems: 'center',
                    flexWrap: 'wrap', marginTop: 8 }}>
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
        {hasOwn && (
          // Distinct from the toggle on purpose: the toggle parks an image,
          // this destroys it. Same word the server uses for the same verb.
          <button type="button" className="hub-btn ghost" disabled={busy}
                  onClick={() => onClear(slot)}>
            Delete yours
          </button>
        )}
      </div>

      <div className="bud-field-hint">
        {hint}
        {stashed && !usingCustom && (
          <> Your own image is kept — switch to <b>Yours</b> to put it back.</>
        )}
      </div>
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
    }, 'Deleted.');

  // The toggle. Same cached-brand bookkeeping as upload/clear, because the
  // header and the sign-in card read that cache — without it the sidebar keeps
  // rendering the old source until the next reload.
  const setSource = (slot: string, source: 'default' | 'custom') =>
    run(async () => {
      const r = await hub.setBrandAssetSource(slot, source);
      reload();
      const assets = { ...getBrand().assets };
      if (r.url) assets[slot] = r.url;
      else delete assets[slot];
      setBrand({ ...getBrand(), assets });
      return undefined;
    }, source === 'default' ? "Using Ava's default." : 'Using your image.');

  // No 'icon' row. The favicon, the home-screen tile and the maskable icon are
  // Ava's on every install and are not configurable — see ava_bridge/brand.py
  // SLOTS. The Logo row lost its "used for the favicon…" clause with them: it
  // was the sentence that made a logo upload silently re-brand the browser tab.
  const ASSETS: { slot: string; label: string; hint: string }[] = [
    { slot: 'logo', label: 'Logo',
      hint: 'A square mark, shown inside the app once you are signed in. The browser tab and home-screen icon always stay Ava.' },
    { slot: 'wordmark', label: 'Wordmark',
      hint: "A wide lockup for the sidebar. Ava's default is the AVA wordmark; upload your own, or delete it to fall back to your name as text." },
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
    if (!window.confirm(
      "Reset every branding value to Ava's defaults?\n\n"
      + 'Your uploaded images are kept — each one can be switched back on individually.',
    )) return;
    // The images are switched to Ava's default rather than deleted, which is
    // what makes the confirm's promise true. Before the toggle existed this
    // button left every uploaded image in force, so an install that pressed it
    // still wore its old logo and the label was simply wrong.
    save({ name: '', tagline: '', accent: '', accent_light: '', chrome: '', public: true },
         "Reset to Ava's defaults.",
         () => { void Promise.all(
           (lim.slots ?? []).map((s) => hub.setBrandAssetSource(s, 'default').catch(() => null)),
         ).then(reload); });
  };

  return (
    <>
      <ResourceError r={bRes} label="your branding settings" />

      <Panel
        title="Make it yours"
        subtitle="Ava ships one look. Change the name it answers to, the colour it uses, and its marks. Every image starts as Ava's own and stays one click away, so trying yours is never a one-way door. Nothing here costs anything: re-branding your own install is the point of running it yourself."
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
            {/* Deliberately says LABEL, not icon. The name still titles the tab
                and names the home-screen shortcut; the picture on both is Ava's
                and is not configurable. Saying "the home-screen icon" here is
                what set the expectation that the artwork followed too. */}
            Reaches the header, the sign-in page, the browser tab's title, the
            home-screen label and the assistant's own sense of what it is called.
            Clear it to go back to “{d.name}”.
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
            Anyone who can reach this address sees the sign-in page, so this covers the
            name, the tagline and the colour on it. Off means it renders Ava's own
            default instead — not a blank card, which would only advertise that
            something is being hidden. The mark on that card is always Ava's either way.
          </div>
        </div>

        {ASSETS.map((a) => (
          <AssetRow
            key={a.slot}
            slot={a.slot}
            label={a.label}
            hint={a.hint}
            url={b.assets?.[a.slot]?.url ?? null}
            stashed={b.assets?.[a.slot]?.stashed ?? false}
            defaultUrl={b.assets?.[a.slot]?.default_url ?? ''}
            busy={busy}
            onPick={pickAsset}
            onClear={clearAsset}
            onSource={setSource}
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
