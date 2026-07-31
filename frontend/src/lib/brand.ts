// Brand (name / tagline / colours / logos) — one source of truth for the app.
//
// Deliberately the same shape as lib/theme.ts, which already solved this exact
// problem for light/dark: decide the value, stamp it on <html>, let React
// subscribe, and do the FIRST stamp in index.html before paint so there is no
// flash. Copying a proven module beats inventing a second convention.
//
// Resolution order: the localStorage cache (written by the last successful
// fetch) → Ava's shipped defaults. The network is a REVALIDATION, never the
// critical path, which is what makes this work offline — the SPA is a PWA whose
// shell is precached, and a design that fetched the brand before painting would
// show default blue on a branded install every time the network was slow or
// absent.
//
// The honest limitation: a browser profile that has never loaded this app has
// nothing cached, so it paints Ava's default accent for one frame before
// revalidate() lands. That is exactly the deal the theme script has always made
// with localStorage, and the alternatives (a render-blocking /brand.css, or
// server-patching index.html) each cost the offline story to buy that frame.
// See the commit message for why they were rejected.

export type Brand = {
  name: string;
  tagline: string;
  accent: string;        // '' = Ava's shipped accent
  accent_light: string;
  chrome: string;
  branded: boolean;
  assets: Record<string, string>;   // slot -> URL, '' when unset
};

const KEY = 'ava.brand';

export const DEFAULT_BRAND: Brand = {
  name: 'Ava',
  tagline: '',
  accent: '',
  accent_light: '',
  chrome: '',
  branded: false,
  assets: {},
};

export function getBrand(): Brand {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULT_BRAND, ...(JSON.parse(raw) as Partial<Brand>) };
  } catch { /* storage unavailable or corrupt — the default is a fine answer */ }
  return DEFAULT_BRAND;
}

/**
 * Brand → DOM. THE only implementation, which is the point: the panel's live
 * preview calls this, and the boot script in index.html mirrors it line for
 * line, so what you preview is what you get after a reload. A second copy of
 * this logic is how a preview and a reload start disagreeing.
 *
 * Writes only — no storage, no events. Preview needs to change the page without
 * persisting, so persistence lives in setBrand().
 */
export function applyBrand(b: Brand): void {
  if (typeof document === 'undefined') return;
  const d = document.documentElement;
  if (b.accent) {
    d.style.setProperty('--brand-accent', b.accent);
    d.style.setProperty('--brand-accent-light', b.accent_light || b.accent);
    d.setAttribute('data-brand', 'on');
  } else {
    // Removing the attribute is what restores the shipped palette — the
    // [data-brand] rules in tokens.css simply stop matching. Clearing the two
    // properties as well so a stale value cannot resurface if the attribute is
    // ever set by something else.
    d.removeAttribute('data-brand');
    d.style.removeProperty('--brand-accent');
    d.style.removeProperty('--brand-accent-light');
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', b.chrome || '#262624');
  if (b.name) document.title = b.name;
}

export function setBrand(b: Brand): void {
  applyBrand(b);
  try { localStorage.setItem(KEY, JSON.stringify(b)); } catch { /* storage unavailable */ }
  window.dispatchEvent(new CustomEvent<Brand>('ava:brand', { detail: b }));
}

/** Reset the DOM to whatever is persisted — used when a preview is abandoned. */
export function restoreBrand(): void {
  applyBrand(getBrand());
}

function same(a: Brand, b: Brand): boolean {
  return a.name === b.name && a.tagline === b.tagline && a.accent === b.accent
    && a.accent_light === b.accent_light && a.chrome === b.chrome
    && JSON.stringify(a.assets) === JSON.stringify(b.assets);
}

/**
 * Fetch the real brand and adopt it if it moved. Called once at boot, off the
 * critical path. A failure is silent on purpose: an unauthenticated or offline
 * client keeps the cached brand, which is strictly better than reverting to
 * blue because a request 401'd.
 */
export async function revalidate(fetcher: () => Promise<Partial<Brand>>): Promise<void> {
  try {
    const fresh = { ...DEFAULT_BRAND, ...(await fetcher()) };
    if (!same(fresh, getBrand())) setBrand(fresh);
  } catch { /* keep the cache */ }
}

// ---- Contrast, client-side ------------------------------------------------
// The SERVER is the authority: ava_bridge/brand.py re-checks on every save and
// refuses. This copy exists purely for feel — the readout has to move while you
// drag the picker, and a round trip per keystroke would be both laggy and
// chatty. The thresholds are duplicated deliberately and the backend owns them;
// tests/test_brand_contrast.py pins the numbers on that side.
//
// Same asymmetry as the backend, for the same reason: the accent is BOTH a
// background under white text and a mark on the canvas, so text-on-accent uses
// the 4.5 AA bar and canvas checks use WCAG 1.4.11's 3:1 non-text bar. Ava's own
// blue passes the first by 0.01 and fails 4.5 against the dark canvas — a guard
// using 4.5 for both would reject the shipped default.
const DARK_BG = '#262624';
const LIGHT_BG = '#faf9f5';
export const TEXT_ON_ACCENT_MIN = 4.5;
export const ACCENT_ON_CANVAS_MIN = 3.0;

function luminance(hex: string): number {
  const h = hex.replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(full.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: string, b: string): number {
  const [la, lb] = [luminance(a), luminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

export const isHex = (s: string) => /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(s.trim());

/** Advisory only — the save is what decides. `null` for blank or non-hex. */
export function checkAccentLocal(accent: string):
  { ok: boolean; ratios: Record<string, number>; blocking: string[] } | null {
  if (!accent.trim() || !isHex(accent)) return null;
  const ratios = {
    text_on_accent: contrastRatio(accent, '#ffffff'),
    accent_on_dark: contrastRatio(accent, DARK_BG),
    accent_on_light: contrastRatio(accent, LIGHT_BG),
  };
  const blocking: string[] = [];
  if (ratios.text_on_accent < TEXT_ON_ACCENT_MIN) {
    blocking.push(`white text on your accent would be unreadable (${ratios.text_on_accent.toFixed(2)}:1, needs ${TEXT_ON_ACCENT_MIN}:1)`);
  }
  if (ratios.accent_on_dark < ACCENT_ON_CANVAS_MIN) {
    blocking.push(`your accent disappears against the dark canvas (${ratios.accent_on_dark.toFixed(2)}:1, needs ${ACCENT_ON_CANVAS_MIN}:1)`);
  }
  if (ratios.accent_on_light < ACCENT_ON_CANVAS_MIN) {
    blocking.push(`your light-theme accent disappears against the light canvas (${ratios.accent_on_light.toFixed(2)}:1, needs ${ACCENT_ON_CANVAS_MIN}:1)`);
  }
  return { ok: blocking.length === 0, ratios, blocking };
}
