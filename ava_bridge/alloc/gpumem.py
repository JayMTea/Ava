"""Per-process accelerator memory — the residency oracle.

"How much is free" and "who is holding it" are different questions with different
answers, and using the wrong source for the second one is a trap this module exists to
avoid. Measured on the development box, for one language-model container:

    docker stats MemUsage ............  3.6 GiB
    systemd cgroup MemoryCurrent ....  under-reports the same way
    nvidia-smi --query-compute-apps .. 43.7 GiB   <- the truth

A container runtime and a service manager both report what the *cgroup* is charged
for. An inference engine's weights are device allocations, which on this class of
hardware are not charged there — so both under-report by an order of magnitude. Acting
on that is worse than having no number: the planner would project a few GiB from
releasing a model that actually holds tens, refuse work that would have fit, and
release more things than necessary trying to reach a target it had already passed.

So residency is read from the accelerator's own per-process accounting and attributed
to a container or unit through its process tree.

**This is deliberately NOT a free-memory source.** The pool question stays with the
`hwinfo` HAL, which knows that on unified memory a device-memory query returns nothing
at all and the system pool is the honest answer. The convention guard enforces that
split: this module may ask which *process* holds what, never how much is *free*.

**Portability.** No accelerator tooling, a non-NVIDIA accelerator, or a CPU-only box
yields None — unknown — and each driver falls back to whatever its own control plane
can say, then to the declared weight. Unknown is never zero.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

_TTL_S = float(os.environ.get("AVA_ALLOC_GPUMEM_TTL", "3"))
_lock = threading.RLock()
_cache: dict = {"ts": 0.0, "by_pid": None}


def by_pid(*, force: bool = False) -> "dict[int, float] | None":
    """`{pid: GiB}` of accelerator memory held per process, or None if unreadable.

    TTL-cached: a plan touches several models, and each would otherwise spawn its own
    query. None means "no per-process accounting available here", which callers must
    treat as unknown rather than as zero.
    """
    with _lock:
        fresh = (not force and _cache["by_pid"] is not None
                 and time.monotonic() - _cache["ts"] < _TTL_S)
        if fresh:
            return _cache["by_pid"]
    got = _read()
    with _lock:
        _cache.update(ts=time.monotonic(), by_pid=got)
    return got


def for_pids(pids: "set[int] | list[int]") -> float | None:
    """Total GiB held by these pids. None when per-process accounting is unavailable.

    Returns 0.0 (not None) when accounting works and none of the pids appear — that is
    a real answer: the process exists but holds no accelerator memory. Distinguishing
    the two is the whole point; conflating them is how a model that failed to load its
    weights passes for a healthy one.
    """
    table = by_pid()
    if table is None:
        return None
    want = {int(p) for p in pids}
    return round(sum(g for p, g in table.items() if p in want), 2)


def for_tree(root_pid: int | None) -> float | None:
    """Total GiB held by a process and its descendants.

    Engines commonly split a launcher from the worker that actually owns the weights,
    so asking only about the pid a supervisor reports would miss almost all of it.
    """
    if not root_pid:
        return None
    table = by_pid()
    if table is None:
        return None
    tree = _descendants(int(root_pid))
    return round(sum(g for p, g in table.items() if p in tree), 2)


# --- readers ------------------------------------------------------------------ #
def _read() -> "dict[int, float] | None":
    """Per-process accelerator memory, or None when nothing can report it."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run(
            [smi, "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
    except Exception:  # noqa: BLE001 — absent/hung tooling is "unknown", not an error
        return None
    if out.returncode != 0:
        return None
    table: dict[int, float] = {}
    for line in (out.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            pid, mib = int(parts[0]), float(parts[1])
        except ValueError:
            continue        # a row reporting N/A is unknown, not zero — skip it
        table[pid] = table.get(pid, 0.0) + mib / 1024.0
    # An empty table is a real reading ("nothing holds accelerator memory"), which is
    # different from being unable to read at all.
    return table


def _descendants(root: int) -> set[int]:
    """`{root}` plus every process below it, from /proc. Best-effort."""
    tree = {root}
    try:
        children: dict[int, list[int]] = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            ppid = _ppid(pid)
            if ppid:
                children.setdefault(ppid, []).append(pid)
    except OSError:
        return tree
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in tree:
                tree.add(child)
                stack.append(child)
    return tree


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        # The comm field can contain spaces and parentheses, so split after it.
        return int(data[data.rindex(")") + 1:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def reset_cache() -> None:
    with _lock:
        _cache.update(ts=0.0, by_pid=None)
