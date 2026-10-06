#!/usr/bin/env python3
"""Verify an `ava-attest/*` bundle. Stdlib only; imports nothing from Ava.

    python3 verify.py .              # verify the bundle in this directory
    python3 verify.py . --self-test  # prove the verifier can FAIL

This file is copied verbatim into every bundle as `verify.py`, so a third party
recomputes every digest offline with nothing but a Python interpreter. That is the
trust model: **reproducibility, not authenticity.** A hobbyist signing their own
bundle on their own box proves only that the box which made the claim also signed
it — a key-loss failure mode for no trust. Recomputation is stronger and needs no
key.

Signing is designed for and not shipped: `manifest.sig` is an additive sibling, and
a missing signature is reported as `unsigned`, **never** as `invalid`. Those are
different claims and conflating them would make an unsigned bundle look tampered.

`--self-test` is not optional. It mutates a byte of an in-memory copy and asserts the
verifier reports MISMATCH, because **a verifier that always prints `ok` is
indistinguishable from one that works.** A clean run alone evidences nothing.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Both bundle schemas. `ava-attest/1` is a strict subset produced by a single-host
# Ava; `ava-attest-multi/1` adds the artifacts that need a control plane. ONE verifier
# reads either, because the alternative is two files that drift and a recipient who
# has to know which one they were sent.
SCHEMAS = ("ava-attest-multi/1", "ava-attest/1")

OK, MISMATCH, ABSENT, EXTRA = "ok", "MISMATCH", "ABSENT", "not-in-manifest"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    """The bundle's serialization. Stated in VERIFY.md and here, identically.

    A third party cannot be asked to guess a formatting convention, so the rule is
    written in two places on purpose and `provenance.json` carries it a third time.
    """
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def verify(root: Path, mutate: str | None = None) -> dict:
    """Check every artifact against the manifest.

    `mutate` is a bundle-relative path whose bytes are flipped IN MEMORY before
    hashing — the mechanism behind `--self-test`. Nothing on disk is touched.
    """
    man_path = root / "manifest.json"
    if not man_path.is_file():
        return {"ok": False, "error": f"no manifest.json in {root}"}
    try:
        raw_manifest = man_path.read_bytes()
        manifest = json.loads(raw_manifest)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"manifest.json is unreadable: {e}"}

    schema = manifest.get("schema")
    if schema not in SCHEMAS:
        return {"ok": False,
                "error": f"schema {schema!r}; this verifier understands "
                         f"{', '.join(SCHEMAS)}"}

    results: dict[str, str] = {}
    declared = manifest.get("artifacts") or {}
    for rel, meta in sorted(declared.items()):
        p = root / rel
        if not p.is_file():
            results[rel] = ABSENT
            continue
        try:
            data = p.read_bytes()
        except OSError:
            results[rel] = ABSENT
            continue
        if mutate is not None and rel == mutate:
            data = _flip(data)
        results[rel] = OK if _digest(data) == meta.get("sha256") else MISMATCH

    # Files present in the bundle but not indexed. Reported, not failed: verify.py,
    # VERIFY.md and manifest.sha256 are legitimately un-indexed (a manifest cannot
    # index itself or its own sibling digest without a convention the reader would
    # have to be told).
    known = set(declared) | {"manifest.json", "manifest.sha256", "verify.py",
                             "VERIFY.md"}
    extra = sorted(p.relative_to(root).as_posix()
                   for p in root.rglob("*") if p.is_file()
                   and p.relative_to(root).as_posix() not in known)

    # The manifest's own digest, from its sibling file.
    side = root / "manifest.sha256"
    manifest_state = "no manifest.sha256"
    if side.is_file():
        want = side.read_text(encoding="utf-8").strip()
        got = _digest(_flip(raw_manifest) if mutate == "manifest.json"
                      else raw_manifest)
        manifest_state = OK if got == want else MISMATCH

    sig = root / "manifest.sig"
    signature = "unsigned" if not sig.is_file() else "present-but-unchecked"

    bad = [r for r, v in results.items() if v != OK]
    return {
        "ok": not bad and manifest_state == OK,
        "schema": schema,
        "artifacts": results,
        "manifest": manifest_state,
        "failed": bad,
        "extra_files": extra,
        "signature": signature,
        "summary": manifest.get("summary") or {},
    }


def _flip(data: bytes) -> bytes:
    """Flip one bit of the first byte. Enough to change the digest, nothing more."""
    if not data:
        return b"\x01"
    return bytes([data[0] ^ 0x01]) + data[1:]


def self_test(root: Path) -> int:
    """Prove the verifier can fail. A clean run alone evidences nothing.

    Picks a real indexed artifact, flips a bit of it in memory, and requires the
    result to be MISMATCH. If it is not, this verifier is broken and every `ok` it
    has ever printed is worthless.
    """
    man = root / "manifest.json"
    if not man.is_file():
        print("SELF-TEST: cannot run — no manifest.json")
        return 2
    declared = sorted((json.loads(man.read_text(encoding="utf-8")).get("artifacts")
                       or {}))
    target = next((r for r in declared if (root / r).is_file()), None)
    if target is None:
        print("SELF-TEST: cannot run — the manifest indexes no present artifact")
        return 2

    clean = verify(root)
    if not clean["ok"]:
        print(f"SELF-TEST: the bundle does not verify clean to begin with "
              f"({clean.get('failed') or clean.get('error')}), so a MISMATCH "
              "afterwards would prove nothing")
        return 2

    tampered = verify(root, mutate=target)
    if tampered["artifacts"].get(target) != MISMATCH:
        print(f"SELF-TEST FAILED: flipping a bit of {target} still verified as "
              f"{tampered['artifacts'].get(target)!r}. This verifier cannot detect "
              "tampering and every `ok` it prints is meaningless.")
        return 1
    if tampered["ok"]:
        print("SELF-TEST FAILED: a tampered artifact left the overall result ok")
        return 1

    m = verify(root, mutate="manifest.json")
    if m["manifest"] != MISMATCH:
        print("SELF-TEST FAILED: flipping a bit of manifest.json did not break its "
              "sibling digest check")
        return 1

    print(f"SELF-TEST ok: tampering with {target} is detected, and so is tampering "
          "with manifest.json")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a for a in argv[1:] if a.startswith("--")}
    root = Path(args[0] if args else ".").resolve()

    if "--self-test" in flags:
        return self_test(root)

    r = verify(root)
    if "error" in r:
        print(f"cannot verify: {r['error']}")
        return 2
    if "--json" in flags:
        print(json.dumps(r, indent=2, sort_keys=True))
        return 0 if r["ok"] else 1

    print(f"{r['schema']}   manifest: {r['manifest']}   signature: {r['signature']}")
    for rel, state in r["artifacts"].items():
        mark = " " if state == OK else "!"
        print(f" {mark} {state:<14} {rel}")
    for rel in r["extra_files"]:
        print(f" ? {EXTRA:<14} {rel}")
    s = r["summary"]
    if s:
        # Repeat the bundle's own caveat rather than summarising it away. A verifier
        # that prints "ok" over an incomplete bundle invites exactly the reading the
        # bundle refuses to support.
        print()
        print(f"the bundle reports complete={s.get('complete')}"
              + (f", incomplete: {', '.join(s.get('incomplete') or [])}"
                 if not s.get("complete") else ""))
        if s.get("note"):
            print(f"note: {s['note']}")
    print()
    if r["ok"]:
        print("every present artifact matches the manifest.")
        print("This confirms the bundle was not altered after it was written. It "
              "says nothing about whether the claims inside it are true — for that, "
              "read each artifact's `evidence_class` and `state`.")
    else:
        print(f"FAILED: {len(r['failed'])} artifact(s) do not match: "
              f"{', '.join(r['failed'])}")
    print("Run with --self-test to confirm this verifier can actually fail.")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
