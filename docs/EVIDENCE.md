# Proving it: `ava attest`

One command ships with Ava that checks its own claims rather than restating
them. It was documented nowhere before this page, so it was effectively
invisible.

## `ava attest` - an evidence bundle a stranger can verify

Most trust claims in self-hosted software are properties of a repo's culture: the
docs say a thing, and you either believe the author or read the source. `ava
attest` produces an **evidence bundle** about one running instance, plus a
standalone verifier (`tools/verify_bundle.py`) that checks it without importing
Ava at all.

**The bundle is unsigned, and that is deliberate.** Signing your own bundle on
your own box proves that the machine making the claim also signed it, which buys
nothing but a key to lose. What it offers instead is *reproducibility*: every
digest recomputes offline. The verifier says `signature: unsigned` - never
`invalid`, because those are different claims.

```bash
ava attest                          # human-readable summary
ava attest --json                   # the bundle on stdout
ava attest --out ./evidence         # write it (the only thing that writes)
ava attest --redact-biometrics      # omit the voiceprint digest before sharing
```

`--out` takes a **directory, not a file** - the bundle is nine files (four
artifacts, a manifest, its digest, provenance, and the verifier itself with its
instructions). It has no default: nothing is written unless you name somewhere.

It reports four artifacts, and is explicit about the difference between them:

| Artifact | Kind | Where it comes from |
| --- | --- | --- |
| `stores` | inventory | Ava's own view of every store it keeps |
| `policies` | inventory | declared + generated + overlay egress policies |
| `chain` | **measurement** | recomputing the audit ledger's hash chain |
| `health` | **measurement** | the live feature registry + router reachability |

Three limits are recorded in `not_measured` rather than quietly omitted:

| Not measured | Why not |
| --- | --- |
| container isolation, containment, the policy ceiling, per-user usage | each needs a second party probing from outside; one host claiming them would be asserting isolation from itself |
| independent verification of the audit chain | on one box there is nobody to ask, so the chain result is self-reported and says so |
| whether the code that produced this is upstream's | that comparison needs a control plane holding a second copy |

That distinction is the point of the command: the gaps are named in the bundle,
in the schema, rather than left for a reader to notice.

### Sharing a bundle

By default the bundle includes a digest of your voiceprint, because that digest is
what makes deletion *provable* to you - see [BIOMETRICS.md](BIOMETRICS.md). A hash
of a biometric template is a stable pseudonym, so if you are handing the bundle to
anybody else, pass `--redact-biometrics`.

### Verifying one

```bash
python3 tools/verify_bundle.py ./evidence            # from the Ava checkout
cd ./evidence && python3 verify.py . --self-test     # or from the bundle itself
```

The verifier is deliberately standalone: stdlib only, and it does not import
`ava_bridge`, so it checks the bundle rather than trusting the code that produced
it. A copy travels **inside** every bundle, so whoever you hand it to needs
nothing from this repo.

`--self-test` is worth the extra word. It flips a byte of an in-memory copy and
requires the verifier to notice - because a verifier that always prints `ok` is
indistinguishable from one that works. Run against a bundle that is already
damaged it refuses outright rather than reporting success for detecting damage
that was already there.
