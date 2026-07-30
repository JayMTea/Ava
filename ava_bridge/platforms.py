"""Reader for deploy/platforms.conf — the platform support matrix SSOT.

`hwinfo` answers "what hardware is this?". This module answers "what do we KNOW
about that hardware, and how strongly?" — the install profile, the engine that can
actually serve there, where watts come from, and the verification tier.

Kept separate from `hwinfo` deliberately: hwinfo reads the machine, this reads a
table. Mixing them would put a docs concern on the fit path.

Stdlib only and no `ava_bridge` imports beyond nothing at all, because
`deploy/install.sh` shells into it on a bare system before anything is installed —
the same reason `perf_log.py` stays import-free.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

_CONF_REL = os.path.join("deploy", "platforms.conf")
_COLUMNS = ("key", "platform_id", "mem_model", "label", "profile", "engine",
            "power_source", "nominal_w", "tier", "evidence")

# Tiers, strongest first. A tier ABOVE ci-simulated must cite evidence; the guard
# in tests/test_platform_matrix_ssot.py enforces that rather than trusting review.
TIERS = ("verified-on-device", "ci-native", "ci-simulated",
         "community-reported", "unsupported")
EVIDENCE_REQUIRED = ("verified-on-device", "ci-native", "community-reported")


@dataclass
class Platform:
    key: str = ""
    platform_id: str = ""
    mem_model: str = ""
    label: str = ""
    profile: str = ""
    engine: str = ""
    power_source: str = ""
    nominal_w: float | None = None
    tier: str = ""
    evidence: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """True only for a tier backed by real hardware of this class."""
        return self.tier in ("verified-on-device", "ci-native")

    @property
    def power_measurable(self) -> bool:
        return self.power_source not in ("", "none", "-")

    def summary(self) -> str:
        """One line for `ava doctor` / install.sh. Names the tier, always."""
        s = f"{self.label} [{self.tier}]"
        if self.tier == "ci-simulated":
            s += " — logic tested, numbers unconfirmed on this hardware"
        elif self.tier == "unsupported":
            s += " — not a supported target"
        return s


def _conf_path() -> str:
    override = os.environ.get("AVA_PLATFORMS_CONF")
    if override:
        return override
    # <repo>/ava_bridge/platforms.py -> <repo>/deploy/platforms.conf
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        _CONF_REL)


def _num(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def load() -> list[Platform]:
    """Every row, in file order. Malformed rows are skipped, not fatal.

    A broken table must not brick `ava doctor` on a box whose only problem is a
    stale checkout — the caller degrades to "unknown platform" instead.
    """
    out: list[Platform] = []
    try:
        with open(_conf_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != len(_COLUMNS):
            continue
        d = dict(zip(_COLUMNS, parts))
        out.append(Platform(
            key=d["key"], platform_id=d["platform_id"], mem_model=d["mem_model"],
            label=d["label"], profile=d["profile"], engine=d["engine"],
            power_source=d["power_source"], nominal_w=_num(d["nominal_w"]),
            tier=d["tier"], evidence=d["evidence"]))
    return out


def for_platform(platform_id: str, mem_model: str | None = None) -> Platform | None:
    """The row for a platform class, narrowed by memory model when given.

    `linux-nvidia` matches two rows (unified GB10 vs discrete RTX) because hwinfo
    cannot tell them apart from the platform id alone — `mem_model` is what
    disambiguates, and it comes from `hwinfo.fit_memory()`/`_memory_model()`.
    Without it, the first row wins, which is the documented first-match-wins rule.
    """
    rows = [p for p in load() if p.platform_id == platform_id]
    if not rows:
        return None
    if mem_model:
        for p in rows:
            if p.mem_model == mem_model:
                return p
    return rows[0]


# --- rendered docs tables ---------------------------------------------------- #
# Two views of the same rows, because the two docs answer different questions:
# deploy/README.md asks "which install path do I take", HWINFO_VALIDATION.md asks
# "what can Ava read on this hardware". They used to be hand-maintained and had
# already drifted about Apple Silicon.
_MARK = {
    "verified-on-device": ':ava-check:{ title="Verified on device" }',
    "ci-native": ':ava-check:{ title="Verified by CI on real hardware of this class" }',
    "ci-simulated": ':ava-close:{ title="Logic tested by simulation; numbers unconfirmed" }',
    "community-reported": ':ava-check:{ title="Reported by a community on-device run" }',
    "unsupported": ':ava-close:{ title="Not a supported target" }',
}
_MEM_LABEL = {"unified": "system RAM (unified)", "discrete": "free VRAM",
              "system": "system RAM", "unknown": "system RAM (unverified)"}
_POWER_LABEL = {"nvml-or-smi": "Yes (NVML / nvidia-smi)", "amd-hwmon": "Yes (amdgpu hwmon)",
                "xpu-smi": "Yes (xpu-smi)", "rapl-x86": "CPU package only (x86 RAPL)",
                "none": "None"}


def render_markdown(view: str) -> str:
    """The table body for a docs block. Deterministic, so a test can diff it."""
    rows = load()
    if view == "hwinfo":
        out = ["| Platform | Fit memory source | GPU power | Tier | Evidence |",
               "|---|---|---|---|---|"]
        for p in rows:
            ev = "—" if p.evidence in ("", "-") else f"`{p.evidence}`"
            out.append(f"| {p.label} | {_MEM_LABEL.get(p.mem_model, p.mem_model)} "
                       f"| {_POWER_LABEL.get(p.power_source, p.power_source)} "
                       f"| {p.tier} | {ev} |")
        return "\n".join(out)
    if view == "install":
        out = ["| Your machine | Profile | Local engine | Verified on device |",
               "|---|---|---|---|"]
        for p in rows:
            if p.tier == "unsupported":
                continue
            prof = ("bare metal" if p.profile == "bare-metal"
                    else f"`{p.profile}` profile")
            out.append(f"| {p.label} | {prof} | {p.engine} "
                       f"| {_MARK.get(p.tier, p.tier)} |")
        return "\n".join(out)
    raise ValueError(f"unknown view {view!r} (want 'hwinfo' or 'install')")


BEGIN = "<!-- platforms:begin:{view} — generated from deploy/platforms.conf -->"
END = "<!-- platforms:end -->"


def splice(text: str, view: str) -> str:
    """Replace the marked block in `text` with the freshly rendered table."""
    begin = BEGIN.format(view=view)
    i = text.find(begin)
    if i < 0:
        raise ValueError(f"no {begin!r} marker in the target document")
    j = text.find(END, i)
    if j < 0:
        raise ValueError(f"{begin!r} has no matching {END!r}")
    return (text[:i] + begin + "\n" + render_markdown(view) + "\n"
            + text[j:])


def detect() -> Platform | None:
    """The row describing the machine this is running on.

    Imports hwinfo lazily so `install.sh` can read the table on a bare system.
    """
    try:
        from . import hwinfo
    except Exception:  # noqa: BLE001 — no package context (script use)
        return None
    pid = hwinfo.platform_id()
    fit = hwinfo.fit_memory()
    model, _why = hwinfo._memory_model()
    if model == "unknown":
        # fit_memory already resolved this; infer from what it chose rather than
        # asking again, so the table and the fit path cannot disagree.
        src = fit.source or ""
        if src.startswith("vram"):
            model = "discrete"
        elif src.startswith("system"):
            model = "unified" if pid in ("linux-nvidia", "darwin-apple") else "system"
    return for_platform(pid, model)


def power_profile() -> dict:
    """Where GPU watts come from on this box, and the fallback if nowhere.

    Replaces a single global `nominal_gpu_watts: 180` — a number measured on one
    machine and applied to every other. 180 W is roughly right for a mid-range
    discrete NVIDIA card and wrong by up to an order of magnitude for a Mac mini
    (~10-30 W) or a Strix Halo APU (~50-120 W), so an "estimate" built on it was
    not an estimate of anything in particular.

    `nominal_w` may legitimately be None: a platform with no defensible figure
    reports NOT MEASURED rather than a guess. Callers must treat None as "do not
    compute energy", never as zero — zero kWh is a claim about free electricity.
    """
    row = detect()
    return {
        "platform_key": row.key if row else None,
        "power_source": (row.power_source if row and row.power_measurable
                         else None),
        "nominal_w": row.nominal_w if row else None,
        "provenance": (f"platforms.conf:{row.key}" if row else "unknown-platform"),
    }


def _shq(v: str) -> str:
    """Single-quote for `eval` in sh. Labels contain spaces, slashes and parens."""
    return "'" + str(v).replace("'", "'\\''") + "'"


def _main(argv: list[str]) -> int:
    """`python3 -m ava_bridge.platforms [--markdown VIEW | --sync | --detect]`"""
    import sys
    args = argv[1:]
    if not args or args[0] == "--detect":
        p = detect()
        print(p.summary() if p else "unknown platform (no matching row)")
        return 0
    if args[0] == "--markdown":
        view = args[1] if len(args) > 1 else "hwinfo"
        print(render_markdown(view))
        return 0
    if args[0] == "--install-detect":
        # Shell-evalable, for deploy/install.sh. One subprocess, one contract, so
        # the installer's idea of the platform and the app's cannot diverge — they
        # used to, because install.sh ran its own nvidia-smi probes.
        #
        # Prints nothing and exits 1 when the platform has no row, so the caller
        # can fall back to its shell probes rather than acting on a blank.
        p = detect()
        if p is None:
            return 1
        for k, v in (("AVA_DETECTED_PROFILE", p.profile),
                     ("AVA_DETECTED_KEY", p.key),
                     ("AVA_DETECTED_PLATFORM", p.platform_id),
                     ("AVA_DETECTED_MEM_MODEL", p.mem_model),
                     ("AVA_DETECTED_ENGINE", p.engine),
                     ("AVA_DETECTED_TIER", p.tier),
                     ("AVA_DETECTED_LABEL", p.label)):
            print(f"{k}={_shq(v)}")
        return 0
    if args[0] == "--sync":
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        targets = [("hwinfo", os.path.join(root, "docs", "HWINFO_VALIDATION.md")),
                   ("install", os.path.join(root, "deploy", "README.md"))]
        changed = []
        for view, path in targets:
            with open(path, encoding="utf-8") as f:
                before = f.read()
            after = splice(before, view)
            if after != before:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(after)
                changed.append(os.path.relpath(path, root))
        print("synced: " + (", ".join(changed) if changed else "nothing (up to date)"))
        return 0
    print(__doc__ or "", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv))
