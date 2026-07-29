"""Convention guard: ALL free-memory reads go through the hwinfo HAL.

An allocator is only as good as its numbers, and on real hardware most of the
numbers on offer are wrong in ways that look plausible:

  * a "free memory" figure that excludes reclaimable page cache reads tens of GiB
    low right after any large model load — because loading weights is what filled
    that cache. Measured on the dev box: 51 GiB reported against 60 GiB genuinely
    available.
  * an engine that caches allocations in a CUDA async pool can truthfully report
    ~0 GiB reserved *by its allocator* while holding tens of GiB of the pool.
    Measured on the dev box: 0.06 GiB reported while holding ~66 GiB.
  * a GPU-memory query is simply unavailable on unified-memory hardware, where it
    returns N/A rather than a number.

Deciding on any of those produces confident, wrong answers — and a wrong "there is
room" is how a box ends up OOM-killing a render, while a wrong "there is no room"
is how it refuses work it could have done. `ava_bridge/hwinfo.py` is the one place
that decides what "free" means per platform, so this test fails any other reader
with instructions.

Note this guards the *pool* question only. "Who is holding the memory" is a
different question with a different source (per-process accounting, which still
works on unified memory where the pool query does not), and drivers answer it from
their own control planes — that is expected and not what this scan is about.

Style follows tests/test_feature_convention.py: a static scan over tracked sources
that runs anywhere, including CI, with no GPU and no bridge.
"""
import pathlib
import re
import unittest
from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The HAL itself is the only module allowed to READ these sources — that is its job.
ALLOWED = {
    "ava_bridge/hwinfo.py",
    # Holds the vocabulary, not a reader: `platforms.py` maps the `power_source`
    # token from deploy/platforms.conf to a human label, so it *names* xpu-smi
    # without ever invoking it. Allowlisted rather than renaming the token,
    # because a power source that does not say which tool produces it is worse
    # documentation. Residual risk accepted knowingly: a real memory read added
    # to this module would not be caught here. It is ~200 lines of table parsing
    # with no subprocess and no /proc access, and `test_platform_matrix_ssot.py`
    # covers its behaviour.
    "ava_bridge/platforms.py",
}

# Readers that are wrong for a fit decision. Each is a real trap, not a style rule:
#   MemFree            — excludes reclaimable cache; use MemAvailable
#   memory.free (smi)  — unavailable on unified memory
#   vram_free / torch_vram_*  — an engine's self-report, not the pool
#
# The vendor sysfs/CLI readers below are banned outside the HAL for a sharper
# reason than tidiness: `mem_info_vram_total` is the BIOS carve-out on an APU
# (512 MiB on `auto`, against a 128 GiB real pool), so a caller reading it
# directly gets a number that is not merely stale but off by two orders of
# magnitude in the *refusing* direction. Only hwinfo pairs it with
# `mem_info_gtt_total` and `_memory_model()` to tell unified from discrete.
# `powercap`/`energy_uj` is here because the directory exists and is EMPTY on
# ARM, so a naive presence check reports a power sensor that cannot be read.
_FORBIDDEN = re.compile(
    r"""(?x)
    \bMemFree\b
  | --query-gpu=[^\s'"]*memory\.free
  | \bvram_free\b
  | \btorch_vram_(?:total|free)\b
  | \bmem_info_(?:vram|gtt)_total\b
  | \brocm-smi\b
  | \bxpu-smi\b
  | \bgpu_busy_percent\b
  | \bpower1_average\b
  | \bpowermetrics\b
  | \benergy_uj\b
  | /sys/class/drm\b
    """)

_FIX = (
    "\n\nRead the pool through the hardware abstraction layer instead:\n"
    "  from ava_bridge import hwinfo\n"
    "  mem = hwinfo.fit_memory()      # MemInfo(free_gb, total_gb, source)\n"
    "  if not mem.readable: ...       # None means UNKNOWN -> do not gate\n"
    "\nhwinfo picks the right source per platform (free VRAM on a discrete GPU,\n"
    "the system pool on unified memory where a GPU query returns nothing) and\n"
    "reports which one it used. Inside the allocator, use\n"
    "ava_bridge/alloc/capacity.py, which is the layer's only HAL caller.\n"
    "\nMeasuring how much a PROCESS holds is a different question — ask the\n"
    "driver for that (ModelDriver.residency), not the pool."
)




class MeasurementConventionTests(unittest.TestCase):
    def test_no_module_reads_a_misleading_memory_source(self):
        offenders = []
        for path in _tracked("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED or rel.startswith("tests/"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue  # a comment explaining the trap is not an offence
                if _FORBIDDEN.search(line):
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
        self.assertFalse(
            offenders,
            "these read a memory source that is wrong for a fit decision:\n  "
            + "\n  ".join(offenders) + _FIX)

    def test_capacity_is_the_allocators_only_hal_caller(self):
        """Inside alloc/, only capacity.py may import hwinfo.

        Drivers receive `free_gib`/`wait_free` through DriverContext instead, which
        is what keeps this rule enforceable and lets a driver be unit-tested against
        a fake pool with no hardware at all.
        """
        offenders = []
        for path in _tracked("ava_bridge/alloc/*.py") + _tracked("ava_bridge/alloc/**/*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.endswith("alloc/capacity.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("#"):
                    continue
                if re.search(r"^\s*(from\s+\.*\s*\S*\s+import\s+.*\bhwinfo\b"
                             r"|import\s+.*\bhwinfo\b)", line):
                    offenders.append(f"{rel}:{n}: {s[:100]}")
        self.assertFalse(
            offenders,
            "only ava_bridge/alloc/capacity.py may read the HAL:\n  "
            + "\n  ".join(offenders)
            + "\n\nTake the pool from DriverContext (ctx.free_gib / ctx.wait_free) "
              "so the driver stays testable without hardware.")


if __name__ == "__main__":
    unittest.main()
