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
  // The agent runtime is a registry capability like any other, but its switch,
  // its status and its provisioning all live in Setup → Agent rather than in the
  // Optional-features list — so the generic `_off` rule below would send the
  // owner to a checkbox that isn't there. Its three codes are handled together
  // because the destination is the same for all of them; only the advice differs.
  if (code === 'agent_off' || code === 'agent_down' || code === 'agent_conflict') {
    const tip = {
      agent_off: 'Opens Setup → Agent — the agent runtime is switched off, so Ava has no tools, memory or skills. Turn it back on there.',
      agent_down: 'Opens Setup → Agent — the agent runtime is required but not answering. Apply it there, or run `ava agent provision --install`.',
      agent_conflict: 'Opens Setup → Agent — agent.required is on while agent.runtime is the tool-less Direct floor. Those two cannot both hold; pick one there.',
    }[code];
    return { label: 'Open Setup → Agent', hash: 'hub/agent', tip };
  }
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
      hash: 'hub/agent/brain',
      tip: 'Opens Setup → Agent → Brain — the engine is running but has no such model loaded; choose one it actually serves.',
    };
  }
  // `model_released` deliberately has NO entry. The control that undoes it lives
  // in the hardware monitor, which floats over every view and has no hash route,
  // so every destination here would be a dead end — `#hub/hardware` shows the
  // memory pool but not the button, and an empty hash navigates to the top. The
  // instruction belongs in the banner's own text instead; see inferenceView.ts.
  m = /^(.+)_down$/.exec(code);
  if (m) {
    return {
      label: 'Check it on Operations',
      hash: 'ops',
      tip: `Opens Operations — the ${pretty(m[1])} service looks down; its status and controls live there.`,
    };
  }
  // The other three codes `connectors.unreachable()` emits. Only `_down` was
  // resolved here, so a connected app that timed out, failed DNS, or errored in
  // any other way gave the owner an accurate message with nowhere to go — which
  // reads as a dead end rather than a fixable problem. Each of these is about
  // ONE app's address, so they all lead to that app's row in Setup, not to
  // Operations: nothing of Ava's is down.
  m = /^(.+)_(timeout|unreachable|error)$/.exec(code);
  if (m) {
    const app = pretty(m[1]);
    const why = {
      timeout: `${app} accepted the connection but did not answer in time`,
      unreachable: `${app}'s address could not be reached at all`,
      error: `the call to ${app} failed`,
    }[m[2]] as string;
    return {
      label: 'Check its address in Setup',
      hash: 'hub/connectors',
      tip: `Opens Setup → Connectors — ${why}. Check it is running and that the address in its manifest is right.`,
    };
  }
  return undefined;
}
