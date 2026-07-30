# Proving it: `ava attest` and `ava eval`

Two commands ship with Ava that check its own claims rather than restate them.
Neither was documented anywhere before this page, so both were effectively
invisible.

## `ava attest` — an evidence bundle a stranger can verify

Most trust claims in self-hosted software are properties of a repo's culture: the
docs say a thing, and you either believe the author or read the source. `ava
attest` produces a signed-shaped **evidence bundle** about one running instance,
plus a standalone verifier (`tools/verify_bundle.py`) that checks it without
importing Ava at all.

```bash
ava attest                          # human-readable summary
ava attest --json                   # the bundle on stdout
ava attest --out bundle.json        # write it (the only thing that writes)
ava attest --redact-biometrics      # omit the voiceprint digest before sharing
```

It reports four artifacts, and is explicit about the difference between them:

| Artifact | Kind | Where it comes from |
| --- | --- | --- |
| `stores` | inventory | Ava's own view of every store it keeps |
| `policies` | inventory | declared + generated + overlay egress policies |
| `chain` | **measurement** | recomputing the audit ledger's hash chain |
| `health` | **measurement** | the live feature registry + router reachability |

Four things a single host *cannot* honestly attest — `container`, `containment`,
`ceiling`, `usage` — are listed in `not_measured` rather than quietly omitted.
Each needs a second party to probe from outside; one host claiming them would be
asserting isolation from itself. That distinction is the point of the command.

### Sharing a bundle

By default the bundle includes a digest of your voiceprint, because that digest is
what makes deletion *provable* to you — see [BIOMETRICS.md](BIOMETRICS.md). A hash
of a biometric template is a stable pseudonym, so if you are handing the bundle to
anybody else, pass `--redact-biometrics`.

### Verifying one

```bash
python tools/verify_bundle.py bundle.json
```

The verifier is deliberately standalone: it does not import `ava_bridge`, so it
checks the bundle rather than trusting the code that produced it.

## `ava eval` — score the intent router on your own traffic

Ava decides server-side whether a message is a chat turn or an image render
(`ava_bridge/turn_router.py`). Whether that router is any good is a question about
*your* phrasing, not a benchmark — so the eval set is yours, built from your own
history, and **never ships with the product** (`data/evals/` is gitignored, and
`tests/test_no_eval_data.py` fails the build if a dataset is ever committed).

```bash
ava eval intent mine        # label candidate cases from your chat history
ava eval intent run         # score the labelled set
ava eval intent run-fingerprint   # score, and record the router fingerprint
```

`mine` walks your messages and asks you to label the ambiguous ones. `run` scores
the labelled set and prints the router's fingerprint and mode alongside the
result, so a score is always attached to the router version that produced it —
a number without that pairing tells you nothing after the next change.

Use `--chats <path>` to mine a different instance's `chats.json`.

Starting from nothing, `run` says the set is empty and points you at `mine`
instead of reporting a meaningless 100%.
