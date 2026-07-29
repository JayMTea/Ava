"""Unmocked HAL assertions on whatever hardware is actually running the suite.

Every other hwinfo test simulates a platform. This one deliberately does not: it
reads the real machine and asserts the real answer is coherent. That is what makes
a `ci-native` tier in `deploy/platforms.conf` mean anything — CI runs on
`ubuntu-latest`, a genuine CPU-only Linux box, so the `linux-cpu` row is backed by
code that ran there rather than by a fixture someone wrote.

Each test skips on hardware it does not describe. A skip here is honest: the
question "does the CPU-only path work" has no answer on a GPU box. What it must
never do is pass vacuously, so every test asserts something about the platform it
DID find before deciding whether to skip.

This is the pattern tests/test_theme_matches_ava.py uses for its missing-checkout
case — correct degradation rather than a hole.
"""
import pytest

from ava_bridge import hwinfo, platforms


def test_the_detected_platform_has_a_matrix_row() -> None:
    """Runs everywhere. Whatever this machine is, the table must describe it."""
    pid = hwinfo.platform_id()
    row = platforms.detect()
    assert row is not None, (
        f"this machine detects as {pid!r} and no row in deploy/platforms.conf "
        "matches it, so `ava doctor` cannot tell its owner what is known about "
        "their hardware. Add a row.")
    assert row.platform_id == pid
    assert row.tier in platforms.TIERS
    # The tier is the claim; make sure it is sayable out loud.
    assert row.summary()


def test_fit_memory_is_coherent_on_this_machine() -> None:
    """Runs everywhere. The pool must be readable-and-sane, or honestly unknown."""
    fit = hwinfo.fit_memory()
    if not fit.readable:
        # Legitimate on a box with no psutil and no /proc. Assert it degraded
        # cleanly rather than half-way.
        assert fit.free_gb is None and fit.total_gb is None and fit.source is None
        return
    assert fit.source, "a readable pool must say where it came from"
    assert fit.free_gb >= 0
    assert fit.total_gb and fit.total_gb > 0
    assert fit.free_gb <= fit.total_gb + 1e-6, (
        f"free ({fit.free_gb}) exceeds total ({fit.total_gb}) from "
        f"{fit.source!r} — a pool reading that cannot be true")


def test_cpu_only_linux_reports_the_system_pool() -> None:
    """The `ci-native` evidence for the linux-cpu row (ubuntu-latest is one)."""
    pid = hwinfo.platform_id()
    if pid != "linux-cpu":
        pytest.skip(f"not a CPU-only Linux box (detected {pid}) — "
                    "this is the assertion CI contributes for that row")
    fit = hwinfo.fit_memory()
    assert fit.readable, "a CPU-only Linux box must still report a memory pool"
    assert fit.source in ("system-psutil", "system-proc"), (
        f"CPU-only must gate on the system pool, got {fit.source!r}")
    assert hwinfo.gpus() == [], (
        "linux-cpu means no DRM card was found, so telemetry must be empty "
        "rather than a fabricated row")
    row = platforms.detect()
    assert row and row.key == "linux-cpu"
    assert not row.power_measurable or row.power_source == "rapl-x86", (
        "the only power source claimable on CPU-only is x86 RAPL")


def test_unified_memory_nvidia_falls_through_to_the_system_pool() -> None:
    """The maintainer's GB10 path, asserted unmocked rather than by fixture."""
    if hwinfo.platform_id() != "linux-nvidia":
        pytest.skip("no NVIDIA driver on this machine")
    vram = hwinfo.vram_mem()
    fit = hwinfo.fit_memory()
    if vram.readable:
        pytest.skip("discrete NVIDIA (VRAM readable) — not the unified path")
    # Unified: the VRAM query yields nothing, which is WHY fit uses system memory.
    assert fit.source and fit.source.startswith("system"), (
        f"unified-memory NVIDIA must gate on the system pool, got {fit.source!r}")
    row = platforms.detect()
    assert row and row.mem_model == "unified", (
        f"detected row {row.key if row else None!r} should be the unified NVIDIA "
        "one when the VRAM query returns nothing")
