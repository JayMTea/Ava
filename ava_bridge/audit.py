"""Append-only audit ledger — the flight recorder's durable substrate.

One JSONL file, `$AVA_HOME/logs/audit.jsonl`: every agent turn (tools used,
status, errors) and every self-edit event (parked / auto-applied / approved /
rejected) is appended as a single flat JSON line. Unlike the in-memory ops
views (state.turns, pruned hourly) and the 20-cycle Learning window, this file
is never truncated by the app — so "what did my agent do yesterday" stays
answerable across restarts.

Properties: append-only via O_APPEND (atomic line writes at this size on
POSIX), 0600 (contains prompts/diff paths), flock'd against concurrent
writers, best-effort (a ledger failure must never break serving). The agent's
own edit tools cannot touch it (access_policy denies logs/**).

Read side: `tail(n, kind=...)` for the API/CLI. Rotation is the operator's
business; the app never deletes audit history — a ledger that prunes itself is
not a ledger. `deploy/logrotate.conf` ships the config that implements this
(~3.7 MB/year measured, so this bounds one file rather than reclaiming space).
Media and telemetry are the stores that actually grow; those are governed by
`data.retention_days` (see data_api.prune_media).

## The chain, in seven lines a stranger can implement

Each record carries `seq` (1-based, monotonic) and `prev`:

    prev(record N) == sha256(exact written line of record N-1, UTF-8,
                             EXCLUDING its trailing newline)
    prev == "" only when the record is the FIRST LINE in the file

⚠️ This used to read "the first chained record has seq 1 and prev \"\"", which is
false in exactly the state every existing ledger is in. `record()` hashes whatever
line precedes it and does not care whether that line carried a `seq`, so appending
to a ledger that already holds unchained records writes `seq: 1` with `prev` = the
digest of the last **legacy** line. A verifier written literally from the old
sentence reports `broken` at seq 1 on every pre-chain ledger — which is all of them.
Found by writing a second, independent implementation of this rule and running it
against the same ledgers: it reported `broken` where this module reported `intact`.
Two implementations disagreeing is the point of having two.

**Split the file on `"\n"` only, never `str.splitlines()`.** `splitlines()` also
breaks on U+2028, U+2029, U+0085, \v and \f, and a record written with
`ensure_ascii=False` can carry a raw U+2028 inside a label or a prompt. A
`splitlines()` verifier then sees two lines where one was written and reports
`broken` on an intact ledger. This module uses `readlines()` / `rsplit("\n", 1)`.

To verify without this module: read the lines, hash each one verbatim, and check
it equals the next record's `prev`. That is all. Nothing depends on JSON key
order, separators or escaping, because the digest is over the bytes on disk
rather than over a re-serialization — the same property that lets
`tests/test_diagram_sync.py` check a rendered SVG without owning the `d2` binary.

`verify_chain()` reports `intact` / `broken` / `unverifiable`. **Records written
before the chain existed carry no `seq` and are reported as
`unverifiable_before`, never as verified** — the maintainer's own ledger holds
2,723 of them (measured 2026-07-29), and claiming to have checked records that
predate the mechanism is precisely the class of false assurance this project
keeps finding in itself.

Rotation interacts with this deliberately: losing the ledger's *tail* is not
corruption and still verifies `intact`, because logrotate does exactly that.
A missing record in the *middle* surfaces as a `seq` gap.

## `actor`: provenance, not identity

`owner` (cookie-authenticated request) · `agent` (the `/internal/*` sandbox-tool
surface) · `cli` · `control-plane` · `unknown`. Ava has no user model — the session
cookie has no subject field — so this answers "did I do this, or did my agent?",
which was previously unanswerable for every event in the ledger. It does not
answer "which person", and inventing that to fill the column would duplicate the
a multi-user control plane's own account table for nothing. An unwired call
site records `unknown` rather
than a plausible guess.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import threading
import time

_lock = threading.Lock()


def _path() -> str:
    from . import settings
    return os.path.join(settings.logs_dir(), "audit.jsonl")


def _opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


# ---- the chain -------------------------------------------------------------- #
# THE RULE, stated once so a third party can recompute it without this code:
#
#   Every record carries `seq` (1-based, monotonic) and `prev`.
#   `prev` is the hex sha256 of the PREVIOUS record's exact written line,
#   encoded UTF-8, EXCLUDING its trailing newline.
#   The first chained record has seq 1 and prev "".
#
# The digest is over the written LINE, not over a re-serialization of the parsed
# object, and that is deliberate: it means a verifier needs no JSON canonicalization
# rules and no copy of this module — read a line, hash the bytes, compare to the
# next record's `prev`. Same property as the `d2sum` stamp in agent/docs/arch.py,
# where the rendered artifact carries the digest of its own source so the checker
# needs neither the generator nor the source.
#
# Why this is worth ~30 lines even with no adversary: `record()` is best-effort and
# swallows every exception, so a full disk silently drops events today and nothing
# can tell. A sequence number makes a dropped record DETECTABLE. Tamper-evidence is
# the second-order benefit, not the motivation.
CHAIN_ALGO = "sha256"

# Provenance of an action, NOT the identity of a person. Ava has no user model —
# the session cookie has no subject field — and inventing one to fill an audit
# column would duplicate a control plane's own account table for no gain. "Did I
# do this or
# did my agent?" is a real single-owner question that was unanswerable; "which of
# my colleagues?" is not a question this product has.
ACTORS = ("owner", "agent", "cli", "control-plane", "unknown")

_actor: contextvars.ContextVar[str] = contextvars.ContextVar("ava_audit_actor",
                                                            default="")


def set_actor(actor: str):
    """Set the actor for this context. Returns the token for `reset_actor`.

    A contextvar rather than a global so a request handler and a background thread
    cannot overwrite each other's attribution.
    """
    return _actor.set(actor if actor in ACTORS else "unknown")


def reset_actor(token) -> None:
    """Restore the actor context to what it was before the matching set_actor().

    Pass the token set_actor() returned. Safe to call with a stale or foreign
    token — a failed reset is suppressed, because losing the actor label on an
    audit record is strictly better than a request failing inside a `finally`.
    """
    with contextlib.suppress(Exception):
        _actor.reset(token)


def _digest_line(line: str) -> str:
    """sha256 of one written record line, newline excluded. THE chain primitive."""
    return hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()


def _last_line(f) -> str:
    """The final non-empty line of an open file, read from the end.

    Seeks backward rather than reading the whole ledger: this runs inside the write
    lock on every single event, and the file only grows.
    """
    try:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if not end:
            return ""
        buf = b""
        step = 1024
        pos = end
        while pos > 0:
            pos = max(0, pos - step)
            f.seek(pos)
            buf = f.read(end - pos)
            if buf.count(b"\n") >= 2 or pos == 0:
                break
            step *= 4
        return buf.decode("utf-8", "replace").rstrip("\n").rsplit("\n", 1)[-1]
    except Exception:  # noqa: BLE001 — an unreadable tail means "start a new chain"
        return ""


def record(kind: str, **fields) -> None:
    """Append one event. Best-effort: never raises into the caller."""
    actor = fields.pop("actor", None) or _actor.get() or "unknown"
    if actor not in ACTORS:
        actor = "unknown"
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock:
            # "a+b": append-only writes (the O_APPEND atomicity this file relies
            # on) plus the ability to read the tail for the chain link. Binary so
            # the backward seek cannot land mid-codepoint.
            with open(path, "a+b", opener=_opener) as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    prev_line = _last_line(f)
                    seq, prev = 1, ""
                    if prev_line:
                        try:
                            prev_evt = json.loads(prev_line)
                            last_seq = prev_evt.get("seq")
                            seq = (int(last_seq) + 1) if last_seq else 1
                        except (ValueError, TypeError):
                            seq = 1        # unparsable tail: begin a fresh chain
                        prev = _digest_line(prev_line)
                    evt = {"ts": round(time.time(), 3), "seq": seq, "prev": prev,
                           "kind": kind, "actor": actor, **fields}
                    line = json.dumps(evt, ensure_ascii=False, default=str)
                    f.write((line + "\n").encode("utf-8"))
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 — auditing must never take the app down
        pass


def verify_chain(path: str | None = None) -> dict:
    """Recompute the chain over the ledger and report what it proves.

    Three outcomes, and the third is the one that matters:

      intact        every chained record links to its predecessor
      broken        a link does not match — `broken_at` is the first bad seq
      unverifiable  there are no chained records at all

    Records written BEFORE the chain existed carry no `seq`, and this box has 2,723
    of them. They are reported as `unverifiable_before`, never folded into
    `intact` — claiming to have verified records that predate the mechanism is
    exactly the "check that reported safety which did not exist" this repo keeps
    finding in itself. It is also distinct from `broken`: nothing is wrong with a
    legacy record, we simply cannot speak for it.
    """
    path = path or _path()
    out = {"state": "unverifiable", "algo": CHAIN_ALGO, "records": 0,
           "chained": 0, "first_seq": None, "last_seq": None,
           "broken_at": None, "unverifiable_before": None, "detail": ""}
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        out["detail"] = "no ledger on disk"
        return out

    out["records"] = len(lines)
    expect_seq: int | None = None
    prev_line: str | None = None

    for line in lines:
        try:
            evt = json.loads(line)
        except ValueError:
            prev_line = line
            continue
        seq = evt.get("seq")
        if seq is None:
            # Pre-chain record. Anything chained must come after it.
            prev_line = line
            continue
        seq = int(seq)
        if out["first_seq"] is None:
            out["first_seq"] = seq
            out["unverifiable_before"] = seq
        out["chained"] += 1
        out["last_seq"] = seq

        if expect_seq is not None:
            if seq != expect_seq:
                out["state"] = "broken"
                out["broken_at"] = seq
                out["detail"] = f"seq jumped: expected {expect_seq}, got {seq}"
                return out
            if evt.get("prev") != _digest_line(prev_line or ""):
                out["state"] = "broken"
                out["broken_at"] = seq
                out["detail"] = (f"prev digest at seq {seq} does not match the "
                                 "preceding line")
                return out
        expect_seq = seq + 1
        prev_line = line

    if out["chained"]:
        out["state"] = "intact"
        legacy = out["unverifiable_before"]
        out["detail"] = (
            f"{out['chained']} chained record(s) verified"
            + (f"; {out['records'] - out['chained']} pre-chain record(s) are "
               f"UNVERIFIABLE (written before seq {legacy})"
               if out["records"] > out["chained"] else ""))
    else:
        out["detail"] = (f"{out['records']} record(s), none chained — this ledger "
                         "predates the chain entirely")
    return out


def tail(n: int = 200, kind: str | None = None) -> list[dict]:
    """Last n events (newest first), optionally filtered by kind."""
    path = _path()
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-max(n * 4, n):]  # over-read to survive filtering
        for line in reversed(lines):
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            if kind and evt.get("kind") != kind:
                continue
            out.append(evt)
            if len(out) >= n:
                break
    except OSError:
        return []
    return out
