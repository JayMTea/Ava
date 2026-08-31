"""The one deny-list for gateway config writes, shared by both entry points.

WHY THIS IS ITS OWN MODULE. The rules below lived in `ava_bridge/gateway_api.py`
and that was correct while the bridge was the only way into `config.set`. It
stopped being sufficient the moment `agent_runtime_server` grew `/gateway/rpc`:
that is a SECOND, independent door to the same gateway, opened by anything
holding `X-Ava-Agent-Token`, on the keys that decide whether the gateway
authenticates browsers at all.

Two doors need the same lock. The alternative — copy-pasting the predicate into
the shim — is a table that drifts, and this one is value-aware and subtle enough
that a drifted copy would look right. So: extract, import from both, and keep
`gateway_api`'s own `audit.record(...)` line where it is (a test greps that file's
source for it).

PURE ON PURPOSE. Nothing here imports from `ava_bridge` — not config, not audit,
not settings. The shim's import graph must not gain `ws_auth`,
`starlette.websockets` and the whole runtime package for four constants, and a
policy predicate that can raise is a policy that fails open.
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# `config.set` can reach the gateway's own device-authentication settings. A UI
# bug that writes one of these converts a transient mistake into a PERMANENT
# posture change that survives every restart, on the setting that decides
# whether the gateway authenticates browsers at all.
#
# Two entries, the reason in the code, the same shape as `_OURS` in
# `provision.retire_policies`. This is technically an "extra gate" and was an
# explicit decision rather than a default. Setup -> Agent -> Runtime shows the
# current posture read-only, with the `nemoclaw` command to change it, so the
# capability is visible rather than silently missing.
# ---------------------------------------------------------------------------
DENIED_CONFIG_KEYS = (
    "gateway.controlUi.dangerouslyDisableDeviceAuth",
    "gateway.controlUi.allowInsecureAuth",
)

# config.set, config.patch and config.apply ALL take one param, `raw`, holding
# the ENTIRE openclaw.json as a string (verified live 2026-08-24). Three
# consequences the old substring check got wrong:
#   1. It gated only `config.set`, so patch/apply were two ungated routes to the
#      same keys.
#   2. It matched `repr(params)` as a substring, so on a real whole-config write
#      it FALSE-NEGATIVED — the dotted string never appears in the JSON that
#      nests it {"gateway":{"controlUi":{...}}} — and simultaneously
#      FALSE-POSITIVED on this box, where the strings occur verbatim inside an
#      unrelated doctor-suppression block, refusing every legitimate write.
# So: parse `raw`, walk the dotted path (and tolerate a flattened dotted key at
# any dict level), refuse only when a denied key is actually ASSERTED (set truthy
# — the dangerous direction), so writing it false or a round-trip that leaves it
# alone still passes.
CONFIG_WRITES = ("config.set", "config.patch", "config.apply")


def asserts_key(doc, dotted: str) -> bool:
    """True if `doc` SETS the dotted key to a truthy value — nested or flattened.

    Value-aware on purpose: both denied keys are "dangerously disable" booleans,
    so the posture change the deny-list exists to prevent is setting them TRUE.
    Writing them false, or omitting them, is the safe direction and passes — which
    is also what lets an owner round-trip a whole config (fetch, edit an unrelated
    key, write back) without being blocked, as long as they are not asserting the
    dangerous flag.
    """
    if not isinstance(doc, dict):
        return False
    if dotted in doc:
        return bool(doc[dotted])
    head, _, rest = dotted.partition(".")
    if rest and head in doc and asserts_key(doc[head], rest):
        return True
    return any(asserts_key(v, dotted) for v in doc.values() if isinstance(v, dict))


def denied_config_write(method: str, params: dict) -> str | None:
    """The denied key this call would assert, or None.

    Returns the KEY rather than a bool so both callers can name it in the
    refusal — an owner who is told only "refused" goes looking through a
    whole-config write for something the message could simply have pointed at.
    """
    if method not in CONFIG_WRITES:
        return None
    raw = (params or {}).get("raw")
    if not isinstance(raw, str):
        return None          # not the live shape; the gateway validates it
    try:
        doc = json.loads(raw)
    except ValueError:
        return None          # malformed raw is the gateway's to refuse, not ours
    for key in DENIED_CONFIG_KEYS:
        if asserts_key(doc, key):
            return key
    return None
