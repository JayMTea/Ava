"""`ava attest` — an evidence bundle for a single-host Ava, with its own verifier.

This exists because **a trust claim that only works with a control plane is a trust
claim almost nobody can check.** Practically every Ava is one person on one box, and
"we write NOT MEASURED where a claim is unverified" is a property of a repo's culture
until something ships that a stranger can run.

`ava-attest/1` is a deliberate **strict subset** of `ava-attest-multi/1`. Four
artifacts, because four are all a single host can honestly produce:

| artifact | class | source |
|---|---|---|
| `stores`   | inventory   | `data_api.stores()` — Ava's own view of what it keeps |
| `policies` | inventory   | `policy_inventory` — declared, generated and overlay |
| `chain`    | measurement | `audit.verify_chain()` over `logs/audit.jsonl` |
| `health`   | measurement | the live feature registry + router reachability |

Absent on purpose, and named in `not_measured` rather than silently missing:
`container`, `containment`, `ceiling` and `usage`. Each needs a second party — a
namespace to probe from outside, a second instance to read across, a control plane
that injected something. A single host claiming any of them would be asserting
isolation from itself.

**The one asymmetry with a control plane's bundle, stated so it reads as
judgement.** A multi-tenant bundle withholds the voiceprint digest, because a hash
of a biometric template is a stable pseudonym and that bundle is handed to whoever
asked for it. Here the
digest **is** included: `ava attest` is the owner attesting about their own box to
themselves, and the digest is what makes destruction provable (see
`docs/BIOMETRICS.md`). If you are handing the bundle to someone else, pass
`--redact-biometrics`.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

SCHEMA = "ava-attest/1"

COLLECTED = "collected"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"
STATES = (COLLECTED, DEGRADED, UNAVAILABLE, UNKNOWN)
INCOMPLETE_STATES = (DEGRADED, UNAVAILABLE, UNKNOWN)

MEASUREMENT = "measurement"
CONFIGURATION = "configuration"
INVENTORY = "inventory"
EVIDENCE_CLASSES = (MEASUREMENT, CONFIGURATION, INVENTORY)

CODE_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Artifact:
    name: str
    state: str
    evidence_class: str
    data: dict = field(default_factory=dict)
    reason: str = ""
    producer: str = ""
    producer_digest: str = ""

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"{self.name}: unknown state {self.state!r}")
        if self.evidence_class not in EVIDENCE_CLASSES:
            raise ValueError(
                f"{self.name}: unknown evidence_class {self.evidence_class!r}")
        if self.state in INCOMPLETE_STATES and not self.reason:
            # In the constructor, not a later check: an artifact that is not
            # `collected` and cannot say why is indistinguishable from one nobody
            # looked at.
            raise ValueError(
                f"{self.name}: state {self.state!r} requires a reason naming what "
                "is missing or why it could not be read")

    @property
    def complete(self) -> bool:
        return self.state == COLLECTED

    def to_dict(self) -> dict:
        return asdict(self)


def canonical_json(obj) -> str:
    """Sorted keys, two-space indent, trailing newline. The digest rule, stated once.

    A third party recomputes these offline and cannot be asked to guess a formatting
    convention, so the same sentence appears in `provenance.json` and `VERIFY.md`.
    """
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _sha256_file(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _producer(rel: str) -> dict:
    """Which source file produced an artifact, and its digest.

    Simpler than a multi-tenant bundle's producer check on purpose: there is one
    checkout here, so there
    is no "the instance's copy vs the control plane's" question to answer. A reader
    who wants to know whether this is upstream's code compares the digest against a
    release themselves.
    """
    return {"producer": rel, "producer_digest": _sha256_file(CODE_ROOT / rel)}


# ---- collectors ---------------------------------------------------------------- #
def collect_stores(*, redact_biometrics: bool = False) -> Artifact:
    """Ava's own inventory. Never a hardcoded filename table.

    `data_api.stores()` returns each store's authoritative `path`, so this cannot
    drift from Ava's naming — the failure mode that once had a probe reading
    `chats.json` after the corpus became `chats.db`.
    """
    from . import data_api

    try:
        body = data_api.stores()
    except Exception as e:  # noqa: BLE001
        return Artifact(name="stores", state=UNKNOWN, evidence_class=INVENTORY,
                        reason=f"{type(e).__name__}: {e}",
                        **_producer("ava_bridge/data_api.py"))
    stores = {s["id"]: dict(s) for s in body.get("stores", [])}

    # `secrets` lists names and descriptions, never values — assert it rather than
    # trust it, because this is a live producer that could grow a field.
    blob = json.dumps(stores).lower()
    for bad in ('"password"', '"api_key"', '"token":'):
        if bad in blob:
            return Artifact(name="stores", state=DEGRADED, evidence_class=INVENTORY,
                            data={"withheld": True},
                            reason=f"the inventory contained a {bad} value and was "
                                   "withheld rather than published",
                            **_producer("ava_bridge/data_api.py"))

    vp = _voiceprint(redact=redact_biometrics)
    stores["voiceprint"] = vp
    return Artifact(name="stores", state=COLLECTED, evidence_class=INVENTORY,
                    data={"home": body.get("home"),
                          "total_bytes": body.get("total_bytes"),
                          "retention_days": body.get("retention_days"),
                          "stores": stores},
                    **_producer("ava_bridge/data_api.py"))


def _voiceprint(*, redact: bool) -> dict:
    """`{present, bytes, mtime}` plus a digest — unless you are sharing the bundle.

    The digest is INCLUDED by default here and WITHHELD by a multi-tenant bundle.
    Not an
    inconsistency: `ava attest` is the owner attesting about their own box to
    themselves, and the pre-delete digest is exactly what makes destruction provable
    (docs/BIOMETRICS.md). A multi-tenant bundle goes to a third party, where the same value
    becomes a stable pseudonym correlating one person across bundles.
    """
    import speaker as spk

    paths = [Path(p) for p in spk.voiceprint_paths()]
    present = [p for p in paths if p.exists()]
    out = {
        "present": bool(present),
        "paths": [p.as_posix() for p in paths],
        "bytes": sum(p.stat().st_size for p in present),
        "mtime": max((p.stat().st_mtime for p in present), default=None),
        "special_category": "GDPR Art. 9 biometric data",
        "destruction": "Setup → Agent → Voice → Delete voiceprint (docs/BIOMETRICS.md)",
    }
    if redact or not present:
        out["digest"] = None
        out["digest_withheld"] = (
            "--redact-biometrics: a hash of a biometric template is a stable "
            "pseudonym that correlates one person across bundles"
            if redact else "no voiceprint enrolled")
    else:
        out["digest"] = spk.voiceprint_digest()
        out["digest_note"] = (
            "included because this bundle is the owner attesting about their own "
            "box; it is what makes destruction provable. Pass --redact-biometrics "
            "before sharing.")
    return out


def collect_policies() -> Artifact:
    from . import policy_inventory

    try:
        snap = policy_inventory.snapshot()
    except Exception as e:  # noqa: BLE001
        return Artifact(name="policies", state=UNKNOWN, evidence_class=INVENTORY,
                        reason=f"{type(e).__name__}: {e}",
                        **_producer("ava_bridge/policy_inventory.py"))
    if snap.get("parse_errors") or snap.get("shadowed"):
        why = []
        if snap.get("parse_errors"):
            why.append(f"{len(snap['parse_errors'])} policy file(s) do not parse, "
                       "so their rules could not be inventoried")
        if snap.get("shadowed"):
            # The bundle's claim is "these are the policies in force". A file whose
            # `preset.name` a later-applied file reuses is inventoried and NOT in
            # force, so the claim is only partly true and must say so.
            why.append(f"{len(snap['shadowed'])} policy file(s) are shadowed by "
                       "another file declaring the same preset name, so their "
                       "rules are not the ones the sandbox enforces")
        return Artifact(
            name="policies", state=DEGRADED, evidence_class=INVENTORY, data=snap,
            reason="; ".join(why),
            **_producer("ava_bridge/policy_inventory.py"))
    return Artifact(name="policies", state=COLLECTED, evidence_class=INVENTORY,
                    data=snap, **_producer("ava_bridge/policy_inventory.py"))


def collect_chain() -> Artifact:
    """The ledger's own integrity.

    ⚠️ **Here Ava is checking its own ledger**, which is exactly the arrangement the
    multi-tenant bundle refuses — there the control plane recomputes from disk instead. On a
    single host there is no second party to ask, and pretending otherwise would be
    worse than saying so. `verify.py` ships the rule so a reader can recompute
    independently, and `not_measured` records the limitation.
    """
    from . import audit

    try:
        r = audit.verify_chain()
    except Exception as e:  # noqa: BLE001
        return Artifact(name="chain", state=UNKNOWN, evidence_class=MEASUREMENT,
                        reason=f"{type(e).__name__}: {e}",
                        **_producer("ava_bridge/audit.py"))
    r["self_reported"] = True
    r["caveat"] = ("computed by Ava about Ava's own ledger — a single host has no "
                   "second party to ask. Recompute it yourself with the rule in "
                   "provenance.json.")
    state = r.get("state")
    if state == "unverifiable":
        return Artifact(name="chain", state=DEGRADED, evidence_class=MEASUREMENT,
                        data=r, reason=r.get("reason") or "no chained records",
                        **_producer("ava_bridge/audit.py"))
    if r.get("unverifiable_before"):
        return Artifact(
            name="chain", state=DEGRADED, evidence_class=MEASUREMENT, data=r,
            reason=f"records before seq {r['unverifiable_before']} predate the "
                   "chain and are neither vouched for nor accused",
            **_producer("ava_bridge/audit.py"))
    return Artifact(name="chain", state=COLLECTED, evidence_class=MEASUREMENT,
                    data=r, **_producer("ava_bridge/audit.py"))


def collect_health() -> Artifact:
    """Which optional capabilities are on, and whether the router answers."""
    from . import features

    try:
        snap = features.snapshot()
    except Exception as e:  # noqa: BLE001
        return Artifact(name="health", state=UNKNOWN, evidence_class=MEASUREMENT,
                        reason=f"{type(e).__name__}: {e}",
                        **_producer("ava_bridge/features.py"))
    return Artifact(name="health", state=COLLECTED, evidence_class=MEASUREMENT,
                    data={"features": snap},
                    **_producer("ava_bridge/features.py"))


NOT_MEASURED = [
    {"what": "container isolation, containment, the policy ceiling, per-user usage",
     "why": "each needs a second party — a namespace to probe from outside, another "
            "instance to read across, a control plane that injected something. A "
            "single host claiming any of them would be asserting isolation from "
            "itself. They appear in ava-attest-multi/1, of which this schema is a "
            "strict subset."},
    {"what": "independent verification of the audit chain",
     "why": "Ava computes it about its own ledger, because on one box there is no "
            "second party. The rule is in provenance.json so you can recompute it "
            "yourself; a multi-tenant bundle has a control plane do exactly that."},
    {"what": "whether the code that produced this is upstream's",
     "why": "each artifact carries its producer's sha256. With one checkout there is "
            "nothing on this box to compare against — check it against a release."},
]


def build(*, redact_biometrics: bool = False, now: float | None = None) -> dict:
    stamp = now if now is not None else time.time()
    arts = [collect_stores(redact_biometrics=redact_biometrics),
            collect_policies(), collect_chain(), collect_health()]
    by_state = {s: sorted(a.name for a in arts if a.state == s) for s in STATES}
    not_measured = list(NOT_MEASURED)
    if redact_biometrics:
        not_measured = not_measured + [
            {"what": "the voiceprint's content digest",
             "why": "--redact-biometrics was passed; a hash of a biometric template "
                    "is a stable pseudonym that correlates one person across "
                    "bundles"}]
    return {
        "schema": SCHEMA,
        "generated_at": stamp,
        "provenance": provenance(stamp, redact_biometrics=redact_biometrics),
        "artifacts": {a.name: a.to_dict() for a in arts},
        "summary": {
            "artifacts": len(arts),
            "by_state": by_state,
            "complete": all(a.complete for a in arts),
            "incomplete": sorted(n for s in INCOMPLETE_STATES for n in by_state[s]),
            "measurements": sorted(a.name for a in arts
                                   if a.evidence_class == MEASUREMENT),
            "not_measured": not_measured,
            "note": "`complete` describes this bundle's collection, not this box's "
                    "security posture. A single host cannot measure its own "
                    "isolation; see not_measured.",
        },
    }


def provenance(now: float, *, redact_biometrics: bool = False) -> dict:
    """No hostname, no username, no absolute paths — a bundle is handed over."""
    import platform

    return {
        "schema": SCHEMA,
        "generated_at": now,
        "subset_of": "ava-attest-multi/1",
        "python": platform.python_version(),
        "machine": platform.machine(),
        "canonical_json": ("json.dumps(obj, sort_keys=True, indent=2, "
                           "ensure_ascii=False) + chr(10)"),
        "artifact_digest_rule": "sha256 over each artifact file's bytes as written",
        "chain_rule": ("prev(record N) == sha256(exact written line of record N-1, "
                       "UTF-8, no trailing newline). The first chained record chains "
                       "to the line before it — which may be an unchained legacy "
                       "line — and prev == \"\" only if it is the first line in the "
                       "file. Split on newline (chr(10)) only, never "
                       "str.splitlines(), which also breaks on U+2028/U+2029/U+0085."),
        "signature": "unsigned — the trust model is reproducibility, not "
                     "authenticity; every digest recomputes offline with no key",
        "biometrics_redacted": bool(redact_biometrics),
        "withheld": ["hostname", "username", "absolute paths"],
    }


def write_bundle(bundle: dict, out_dir: str) -> list[str]:
    """One file per artifact plus a manifest. `out_dir` is required, no default."""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    wrote: list[str] = []

    def _write(rel: str, obj) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(canonical_json(obj), encoding="utf-8")
        wrote.append(rel)

    _write("provenance.json", bundle["provenance"])
    for name, art in bundle["artifacts"].items():
        _write(f"{name}.json", art)

    manifest = {
        "schema": bundle["schema"],
        "generated_at": bundle["generated_at"],
        "summary": bundle["summary"],
        "artifacts": {rel: {"sha256": hashlib.sha256(
            (root / rel).read_bytes()).hexdigest()} for rel in sorted(wrote)},
        "digest_rule": ("each artifacts[] sha256 is over the file's bytes as "
                        "written; manifest.sha256 is over manifest.json's bytes. "
                        "Canonical form: json.dumps(sort_keys=True, indent=2, "
                        "ensure_ascii=False) plus a trailing newline."),
    }
    _write("manifest.json", manifest)
    (root / "manifest.sha256").write_text(
        hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest() + "\n",
        encoding="utf-8")
    wrote.append("manifest.sha256")

    verifier = CODE_ROOT / "tools" / "verify_bundle.py"
    if verifier.is_file():
        (root / "verify.py").write_text(verifier.read_text(encoding="utf-8"),
                                        encoding="utf-8")
        wrote.append("verify.py")
    (root / "VERIFY.md").write_text(VERIFY_MD, encoding="utf-8")
    wrote.append("VERIFY.md")
    return wrote


VERIFY_MD = """# Verifying this bundle

    python3 verify.py .              # check every digest
    python3 verify.py . --self-test  # check the checker

`verify.py` is stdlib-only and imports nothing from Ava, so you can read all of it
before you run any of it.

## What a clean run proves

That the bundle was not altered after it was written. **That is all.** It says nothing
about whether the claims inside are true — for that, read each artifact's two labels.

| `evidence_class` | means |
|---|---|
| `measurement`   | something was probed and the result observed |
| `configuration` | what was *set*; says nothing about what is enforced |
| `inventory`     | two lists compared, or one enumerated |

| `state` | means |
|---|---|
| `collected`   | present and complete |
| `degraded`    | present but partial — `reason` names what is missing |
| `unavailable` | the producer exists but could not answer |
| `unknown`     | we cannot tell whether it would have answered, because the tool is missing |

There is **no top-level pass/fail**. `summary.complete` describes collection, not
security.

## What a single host cannot attest to

`ava-attest/1` is a strict subset of `ava-attest-multi/1`. Container isolation,
containment, the policy ceiling and per-user usage are **absent on purpose**: each
needs a second party, and a single host claiming them would be asserting isolation
from itself. They are listed in `summary.not_measured` rather than silently missing.

The audit chain is verified **by Ava, about Ava's own ledger** — on one box there is
no second party to ask, and a multi-tenant bundle has a control plane recompute it
instead. The rule is in `provenance.json` so you can recompute it yourself:

    prev(record N) == sha256(exact written line of record N-1, UTF-8,
                             no trailing newline)

The first chained record chains to the line before it, which may be an unchained
legacy line; `prev == ""` only when it is the first line. Split on newline only —
`str.splitlines()` also breaks on U+2028/U+2029/U+0085, and a record written with
`ensure_ascii=False` can carry a raw U+2028 in a label.

## Biometrics

If `provenance.biometrics_redacted` is `false`, `stores.voiceprint.digest` is present.
That is deliberate for an owner attesting about their own box — the digest is what
makes destruction provable (see `docs/BIOMETRICS.md`). **If you received this bundle
from someone else, they should have passed `--redact-biometrics`**: the digest is not
reversible, but it is a stable unique identifier that correlates one person across
every bundle containing it.

## Signatures

Unsigned, and `verify.py` reports that as `unsigned` — never `invalid`. The trust
model is reproducibility: every digest recomputes offline with no key.
"""
