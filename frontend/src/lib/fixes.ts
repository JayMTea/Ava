// Guided-fix resolution for machine-readable backend error codes.
//
// The backend's feature registry (ava_bridge/features.py) emits REGULAR codes:
//   <feature>_off   — the capability's switch is off (Setup → System panel)
//   <feature>_down  — the switch is on but the backing service won't answer
// We resolve fixes from the code PATTERN, not a per-feature map, so a newly
// registered capability gets its fix-it link here with zero frontend changes.

export interface FixAction {
  label: string;  // link text
  hash: string;   // destination (location.hash route, e.g. 'hub/system')
  tip: string;    // hover-popover copy: where the link leads + what to do
}

const pretty = (key: string) => key.replace(/_/g, ' ');

export function fixForCode(code?: string | null): FixAction | undefined {
  if (!code) return undefined;
  let m = /^(.+)_off$/.exec(code);
  if (m) {
    return {
      label: 'Turn it on in Setup',
      hash: 'hub/system',
      tip: `Opens Setup → System — switch ${pretty(m[1])} back on under Optional features.`,
    };
  }
  // The engine is up and simply does not hold the configured model — the most
  // likely first-run failure, and the one place Operations is the wrong answer:
  // nothing is down, so there is nothing there to restart. Checked BEFORE the
  // `_down` pattern, which `model_unknown` does not match anyway but which a
  // future `model_..._down` code would.
  if (code === 'model_unknown') {
    return {
      label: 'Pick a model in Setup',
      hash: 'hub/agent',
      tip: 'Opens Setup → Agent — the engine is running but has no such model loaded; choose one it actually serves.',
    };
  }
  m = /^(.+)_down$/.exec(code);
  if (m) {
    return {
      label: 'Check it on Operations',
      hash: 'ops',
      tip: `Opens Operations — the ${pretty(m[1])} service looks down; its status and controls live there.`,
    };
  }
  return undefined;
}
