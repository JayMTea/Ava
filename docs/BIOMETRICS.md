# Biometrics: the voiceprint

Ava's voice gate answers to one enrolled voice. That means it stores a
**biometric identifier**, which is the most legally and ethically loaded thing on
the box — so this page states plainly what is collected, where it lives, what
derives from it, and how to destroy it.

This document exists partly because the law asks for it. Illinois **BIPA
§15(a)** requires a *written* retention-and-destruction policy for biometric
identifiers, and under **GDPR Article 9** a voiceprint is special-category data.
Ava is self-hosted and single-owner, which changes who the obligations fall on but
not what a straight answer looks like.

## What is collected

One file: `$AVA_HOME/models/voiceprint.npy` — a 192-dimension normalised
embedding, 896 bytes, mode `0600`. It is produced by ECAPA-TDNN from several short
phrases you speak during enrollment (**Setup → Voice**).

**It is not a recording.** It is a vector, and audio cannot be reconstructed from
it. That is a meaningful privacy property and not a licence to be careless with
it: an embedding still identifies you, which is exactly why it is regulated.

Enrollment clips are held in memory and never written to disk by
`voice_enroll.enroll()`. If you enrolled with the older CLI
(`enroll_voice.py --from-wav`) then **your own** source recordings may sit in
`enroll/` — those are files you supplied, and Ava does not touch them.

## Where it lives, and what derives from it

| Path | What | Destroyed by the delete? |
|---|---|---|
| `$AVA_HOME/models/voiceprint.npy` | the enrolled voiceprint | **yes** |
| `<code root>/models/voiceprint.npy` | a legacy copy `load_voiceprint()` migrates forward | **yes** — see below |
| `state.heavy["voiceprint"]` / `["verifier"]` | the running process's cached copy | **yes**, evicted together |
| `voice.threshold` in `ava.yaml` | a gate tuned to that print | **yes**, reset to default |
| `$AVA_HOME/logs/last_talk.wav` | raw audio, only written under `AVA_DEBUG_TALK` | **yes** |
| `$AVA_HOME/models/ecapa/**` | public pretrained weights, identical for every install | **no** — nothing about you |
| `enroll/*.wav`, `*.m4a` | source recordings *you* provided to the CLI | **no** — your files |

**Why two stored copies matter.** `speaker.load_voiceprint()` migrates a legacy
repo-local voiceprint into the persistent store when the live one is absent, so
that a Docker rebuild never silently loses an enrollment. The consequence is that
deleting only `$AVA_HOME/models/voiceprint.npy` lets the biometric **come back**
on the next gate check. Deletion therefore goes through
`speaker.delete_voiceprint()`, which knows about both, and
`tests/test_voiceprint_deletable.py` asserts `load_voiceprint() is None`
afterwards rather than checking that a path is gone — because a path check passes
on the broken version.

## Retention

**Until you delete it.** There is no timer, and Ava will not quietly expire your
enrollment — a voice gate that stops working on a schedule is a worse product and
a worse promise. That is the policy, stated rather than implied: indefinite
retention, owner-triggered destruction, no third party involved.

The voiceprint never leaves the machine. It is not sent to any inference backend,
cloud or local; the gate runs on-device.

## How to destroy it

**Setup → Voice → Delete voiceprint**, or:

```bash
curl -X POST -b "$COOKIE" http://127.0.0.1:8096/api/hub/voice/delete
```

You get a **receipt** listing absolute paths, what was absent, what failed, and
what was deliberately kept. Three independent ways to confirm it worked, weakest
first:

1. `GET /api/hub/voice/status` reports `enrolled: false` — this is the tool's own
   report about itself, and is the one you should trust least.
2. `test -f` each path in the receipt, and check the **Models & voiceprint** row
   on the **Data** page for its size and last-write time.
3. Read the ledger: `audit.tail(kind="voiceprint")` shows the enrollment and the
   deletion, each with a **content digest**. The digest is what makes destruction
   *provable* — the record can say "an artifact hashing to `a1b2…` existed and was
   destroyed at T" without retaining the artifact, and a 16-hex-character hash of
   a 896-byte vector is not reversible into a voiceprint. Those records are
   `seq`-chained (see `ava_bridge/audit.py`), so a deletion record cannot be
   quietly removed afterwards.

After deletion the gate **fails open**: Ava answers any voice, exactly as on a box
that was never enrolled. It does not lock you out.

## What this does not claim

- **Speaker verification is not authentication.** It reduces casual
  misuse — a housemate or a TV — and it is not a defence against a recording of
  your voice or a deliberate clone. Do not treat the gate as a password. The
  password gate (`SECURITY.md`) is the security boundary; the voice gate is a
  convenience with a privacy cost, which is why it is off by default
  (`features.voice`).
- **Ava does not identify anyone else.** There is one enrolled voice, no
  demographic inference, no classification of a speaker's attributes. The EU AI
  Act's biometric-categorisation provisions concern inferring characteristics from
  biometric data; Ava does not do that.
- **Distributing Ava is not distributing a voiceprint.** The artifact is generated
  on your machine and `models/**` is in the agent's hard-deny list
  (`ava_bridge/access_policy.py`), so Ava's own self-editing cannot read or
  rewrite it.

See also: [SECURITY.md](../SECURITY.md) for the trust boundaries,
[MEMORY.md](MEMORY.md) for how other personal data is stored and erased.
