"""No capability in this app is conditioned on paying for it.

Ava is Apache-2.0 and runs on the owner's own hardware. Re-branding it, or any
other feature, is not something they can be charged for here — not merely
because a gate would be trivially deleted from source they already have, but
because a paywall in a self-hosted app is a promise broken rather than a product
sold. This guard turns "we will not paywall your box" from a sentence in a README
into something a stranger can check in one command.

**What it actually proves, and what it does not.** It fails the build when
executable source names a commercial-gate concept — a licence key, an
entitlement, a subscription, a plan, a paywall, a billing integration. That is a
ratchet against the obvious form and against absent-minded drift, not a proof of
absence: someone determined to gate a feature could spell it `feature_flag_7`
and this would not notice. It is worth having anyway, for the same reason
`test_no_owner_identity` is worth having — the failures it catches are the ones
that actually happen, and its existence states the intent where the next person
will trip over it.

**Prose is deliberately not scanned.** `agent/docs/adr/0004` discusses a
third-party diagramming tool that is genuinely subscription-gated, and NOTICE
uses the word in its Apache boilerplate. Writing ABOUT licensing is not gating,
so only executable source is in scope.

Style follows tests/test_no_owner_identity.py and tests/test_diagram_sync.py: a
static scan over `git ls-files` that runs anywhere and fails with the reasoning
in the message.
"""
from __future__ import annotations

import pathlib
import re

from gitfiles import ROOT, tracked

# Identifiers that name a commercial gate. Deliberately specific: bare "tier"
# and "plan" are excluded because Ava already uses `tier` for HARDWARE tiers and
# consent tiers, and a guard that fires on those is one people learn to skip.
_GATE = re.compile(
    r"\b("
    r"licen[sc]e_key|licenseKey|licence_key"
    r"|entitlement|entitlements"
    r"|subscription_(?:id|status|tier|active)|subscriptionId|subscriptionStatus"
    r"|is_paid|isPaid|is_premium|isPremium|is_pro\b|isPro\b"
    r"|paywall|pay_wall"
    r"|billing_(?:id|status|plan)|stripe_|STRIPE_"
    r"|plan_(?:id|tier|name)|planId|planTier"
    r"|seats_remaining|seat_count"
    r"|trial_(?:expires|ends|active)"
    r")\b",
    re.I,
)

_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".sh"}

# Files that may name a gate concept because their job is to forbid it.
_ALLOW = {
    "tests/test_no_capability_gate.py": "the guard itself",
}


def _scan() -> list[str]:
    hits: list[str] = []
    for rel in tracked():
        p = pathlib.Path(rel)
        if p.suffix not in _CODE_SUFFIXES:
            continue
        if rel in _ALLOW or rel.startswith("frontend/dist/"):
            continue
        try:
            body = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _GATE.finditer(body):
            line = body[:m.start()].count("\n") + 1
            hits.append(f"{rel}:{line}  {m.group(0)}")
    return hits


def test_no_feature_is_gated_behind_a_payment() -> None:
    hits = _scan()
    assert not hits, (
        "executable source names a commercial gate:\n    "
        + "\n    ".join(hits)
        + "\n\nAva is Apache-2.0 and self-hosted. A capability here cannot be sold:\n"
          "a gate is deletable by anyone holding the source they already have, and\n"
          "shipping one breaks the premise the project is positioned on. If this is\n"
          "a hosted service talking to Ava rather than a gate inside it, it belongs\n"
          "in that service's own repository, not in this tree."
    )


def test_the_branding_feature_in_particular_is_ungated() -> None:
    """Branding is the specific capability this guard was written alongside, so
    it is asserted by name rather than left to the general scan."""
    for rel in ("ava_bridge/brand.py", "ava_bridge/brand_pack.py",
                "ava_bridge/hub/branding.py",
                "frontend/src/components/hub/panels/BrandingPanel.tsx"):
        body = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        assert not _GATE.search(body), f"{rel} conditions branding on a payment"


def test_the_pack_loader_does_not_verify_a_signature() -> None:
    """A pack may be SIGNED for provenance, but the loader must accept any
    well-formed pack, unsigned included.

    The moment the app verifies a signature to decide whether to load, there is
    a gate in it — and the obvious next step is refusing packs that did not come
    from a paid generator, which is the exact thing this file exists to prevent.
    Provenance is a label; it must never become a permission.
    """
    body = (ROOT / "ava_bridge/brand_pack.py").read_text(encoding="utf-8")
    for banned in ("verify_signature", "check_signature", "signature_valid",
                   "rsa", "ed25519", "public_key"):
        assert banned not in body.lower(), (
            f"brand_pack.py references {banned!r}. A pack loader that can refuse "
            "an unsigned pack is a gate wearing a security hat.")


def test_the_allowlist_has_not_gone_stale() -> None:
    stale = [rel for rel in _ALLOW if not (ROOT / rel).is_file()]
    assert not stale, "stale allowlist entries: " + ", ".join(stale)
