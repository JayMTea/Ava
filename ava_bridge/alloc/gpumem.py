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
    """Per-process accelerator memory, or None when nothing can report it.

    Two sources, tried in order. NVIDIA answers through `nvidia-smi`'s compute-apps
    query; everything else answers through the kernel's own DRM per-process
    accounting in `/proc/<pid>/fdinfo/<fd>`, which is unprivileged and is what
    `amdgpu_top` and `nvtop` read.

    Why the second one matters more than it looks: with no reader, this returned
    None on every non-NVIDIA box, and the consequence was four layers deep —
    `drivers/docker.py` fell back to `docker stats` (the 12x under-report at the
    top of this file), `Holder.measured` stayed False, and
    `capacity._baseline_sample()` discards any sample containing an unmeasured
    holder, so **the learned baseline never converged at all** on AMD or Intel.
    """
    smi = shutil.which("nvidia-smi")
    if smi:
        table = _read_nvidia(smi)
        if table is not None:
            return table
    return _read_drm_fdinfo()


def _read_nvidia(smi: str) -> "dict[int, float] | None":
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


# Kernel DRM per-process accounting. The key names are the documented
# drm-usage-stats ABI; which one carries device memory differs by driver, so both
# spellings are summed and whichever the driver emits is the one that counts.
_PROC_ROOT_ENV = "AVA_PROC_ROOT"
_MEM_KEYS = (
    "drm-memory-vram",       # amdgpu: device-local memory
    "drm-resident-vram",     # amdgpu, newer kernels
    "drm-total-local0",      # i915 / xe: local memory region 0
    "drm-resident-local0",
)
_UNIT_GIB = {"KiB": 1024.0 ** 2, "MiB": 1024.0, "GiB": 1.0, "B": 1024.0 ** 3}


def _proc_root() -> str:
    return os.environ.get(_PROC_ROOT_ENV) or "/proc"


def _read_drm_fdinfo() -> "dict[int, float] | None":
    """Per-process device memory from `/proc/<pid>/fdinfo/<fd>`.

    Returns None when the kernel exports no DRM stats at all (so the caller keeps
    saying "unknown" rather than "zero") and a table — possibly empty — when it
    does.

    ⚠️ NOT VERIFIED on real hardware. NVIDIA's proprietary driver does not export
    drm-usage-stats, so there is nothing on the maintainer's box to read; this is
    written against the documented ABI and exercised by a fixture
    (`tests/test_gpumem_fdinfo.py`) driving the real parser over a fake /proc.

    **Deduplicated by `drm-client-id`.** A process holding several fds on the same
    DRM client reports the same memory on each, so summing fds naively multiplies
    residency by the fd count — which for the allocator would look like an engine
    holding several times the whole pool, and would release far more than needed.
    """
    root = _proc_root()
    try:
        entries = os.listdir(root)
    except OSError:
        return None

    table: dict[int, float] = {}
    seen: set[tuple[int, str]] = set()
    saw_any_drm = False

    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        fdinfo_dir = os.path.join(root, name, "fdinfo")
        try:
            fds = os.listdir(fdinfo_dir)
        except OSError:
            continue        # process gone, or not ours to read
        for fd in fds:
            fields = _parse_fdinfo(os.path.join(fdinfo_dir, fd))
            if not fields or "drm-driver" not in fields:
                continue
            saw_any_drm = True
            client = fields.get("drm-client-id", f"fd:{fd}")
            key = (pid, client)
            if key in seen:
                continue    # same DRM client through another fd — already counted
            seen.add(key)
            gib = 0.0
            for mk in _MEM_KEYS:
                if mk in fields:
                    gib += _to_gib(fields[mk])
            if gib:
                table[pid] = table.get(pid, 0.0) + gib

    if not saw_any_drm:
        return None         # no DRM accounting on this kernel: unknown, not zero
    return table


def _parse_fdinfo(path: str) -> dict:
    """`key: value` pairs from one fdinfo file, or {} if it is not a DRM fd."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return {}
    if "drm-driver" not in head:
        return {}
    out = {}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        if v:
            out[k.strip()] = v.strip()
    return out


def _to_gib(raw: str) -> float:
    """`"1234 KiB"` -> GiB. An unparsable value contributes nothing."""
    parts = raw.split()
    if not parts:
        return 0.0
    try:
        n = float(parts[0])
    except ValueError:
        return 0.0
    unit = parts[1] if len(parts) > 1 else "B"
    div = _UNIT_GIB.get(unit)
    return n / div if div else 0.0


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
