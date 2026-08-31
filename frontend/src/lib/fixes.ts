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
  // The gateway-runtime seam codes. They match neither `_off` nor `_down` nor
  // the connector `_(timeout|unreachable|error)` rule, so before this branch
  // every one of them was a dead end — and `gateway_timeout`, matching that
  // last rule by accident, actively sent the owner to Setup → Connectors to
  // "check the address in its manifest" for a gateway that is not a connector
  // and has no manifest. All of them lead to Setup → Agent → Runtime, the page
  // that owns "is my agent reachable". Checked here, above the generic rules,
  // so `gateway_timeout` never reaches the connector branch.
  let sm = /^(agent_scope_denied|agent_token_rejected|agent_protocol_mismatch|agent_no_gateway|gateway_[a-z_]+)$/.exec(code);
  if (sm) {
    const tip = ({
      agent_scope_denied: 'the gateway token is missing operator.admin — re-mint it with `nemoclaw <name> gateway-token`',
      agent_token_rejected: 'the gateway rejected the token — it rotates when the sandbox restarts; Ava re-reads it, or paste a fresh one',
      agent_protocol_mismatch: 'the gateway speaks a protocol this build does not — the OpenClaw and Ava versions have drifted',
      agent_no_gateway: 'the configured runtime has no gateway control plane — select `agent.runtime: openclaw_gw`',
      gateway_timeout: 'the gateway accepted the call but did not answer in time',
      gateway_key_refused: 'that config key is protected — change it with the nemoclaw CLI, not from here',
      gateway_rate_limited: 'too many calls to the gateway at once — it will clear on its own',
      gateway_unsupported_method: 'this gateway build does not offer that method — the OpenClaw version may have moved on',
      // Deliberately NOT worded like `agent_no_gateway`. That copy says to
      // select `agent.runtime: openclaw_gw`, which on a two-host install is
      // both the wrong instruction and the wrong machine: the runtime is
      // correct, and the container that has to change is on the agent host.
      gateway_proxy_unsupported: 'the agent container does not proxy the gateway — rebuild and restart it on the agent host',
    } as Record<string, string>)[sm[1]]
      ?? `the agent gateway returned ${pretty(sm[1])}`;
    return {
      label: 'Open Setup → Agent → Runtime',
      hash: 'hub/agent/runtime',
      tip: `Opens Setup → Agent → Runtime — ${tip}.`,
    };
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
  // likely first-run failure, and the one place a connector's address is the
  // wrong answer: nothing is down, so there is nothing there to restart.
  // Checked BEFORE the `_down` pattern, which `model_unknown` does not match
  // anyway but which a future `model_..._down` code would.
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
      label: 'Check it in Setup',
      hash: 'hub/connectors',
      tip: `Opens Setup → Connectors — the ${pretty(m[1])} service looks down; its address and controls live there.`,
    };
  }
  // The other three codes `connectors.unreachable()` emits. Each of these is
  // about ONE app's address, so they all lead to that app's row in Setup — the
  // same destination `_down` now uses, since the Operations page it used to
  // point at is gone.
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
