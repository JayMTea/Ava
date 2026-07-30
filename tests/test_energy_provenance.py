"""Energy provenance must describe the WINDOW, not the live power sensor.

The defect this guards, measured before the fix: `perf_cost` reported
`power_measured: bool(powers)` where `powers` came from `hardware.history()` — a
two-hour ring buffer (`hardware._SAMPLE_KEEP_S`) — while `perf.hot_window` is
48h and everything older than that is summed from rollups whose watt-hours are
`nominal_gpu_watts * seconds`, a static constant. So on an ordinary NVIDIA box a
**7-day** energy figure was labelled "measured" when roughly five days of it had
never been sampled, and `VitalsView` dropped its `(est.)` suffix and printed
"· measured" on the strength of that flag.

The three states are asserted separately because the interesting one is
`partial`: it is the ordinary case on the maintainer's own hardware, and it is
the case both neighbours would misreport.

These monkeypatch the two inputs rather than seeding files, deliberately — the
thing under test is the provenance state machine, and a disk fixture would let a
rollup-format change turn a real regression into a green test.
"""
import pytest

from ava_bridge import dashboard


@pytest.fixture
def stub(monkeypatch):
    """Drive perf_cost's two energy inputs directly."""
    def _apply(*, sampled: bool, hot_seconds: float, cold_wh: float):
        monkeypatch.setattr(
            dashboard.hardware, "history",
            lambda *a, **k: ([{"gpu_power": 200.0}] if sampled else []),
        )
        monkeypatch.setattr(
            dashboard.perf_store, "cold_cost",
            lambda *a, **k: {"spend_usd": 0.0, "energy_wh": cold_wh,
                             "energy_estimated_wh": cold_wh, "by": {}},
        )
        rows = ([{"ts": dashboard.time.time(), "model": "m", "gen_seconds": hot_seconds}]
                if hot_seconds else [])
        monkeypatch.setattr(dashboard, "_all_rows", lambda *a, **k: rows)
    return _apply


def test_fully_sampled_window_is_measured(stub) -> None:
    stub(sampled=True, hot_seconds=3600.0, cold_wh=0.0)
    c = dashboard.perf_cost("1d")
    assert c["energy_state"] == "measured"
    assert c["power_measured"] is True
    assert c["energy_estimated_kwh"] == 0.0


def test_unsampled_window_is_estimated(stub) -> None:
    stub(sampled=False, hot_seconds=3600.0, cold_wh=0.0)
    c = dashboard.perf_cost("1d")
    assert c["energy_state"] == "estimated"
    assert c["power_measured"] is False
    # Every watt-hour is nominal, so the estimated share is the whole figure.
    assert c["energy_estimated_kwh"] == pytest.approx(c["energy_kwh"])


def test_a_long_window_with_rollup_energy_is_never_called_measured(stub) -> None:
    """The regression: sampled *now*, but most of the window predates the buffer."""
    stub(sampled=True, hot_seconds=3600.0, cold_wh=5000.0)
    c = dashboard.perf_cost("7d")
    assert c["energy_state"] == "partial", (
        "a 7-day figure whose bulk came from nominal-wattage rollups reported "
        f"{c['energy_state']!r}. hardware.history() keeps two hours; perf.hot_window "
        "is 48h. The flag must describe the window, not the sensor."
    )
    assert c["power_measured"] is False
    assert c["power_sampled_now"] is True, (
        "the live-sensor signal must still be reportable — it is a real fact, "
        "just the answer to a different question than the label asks."
    )
    assert 0 < c["energy_estimated_kwh"] < c["energy_kwh"]


def test_the_response_contract_still_carries_power_measured(stub) -> None:
    """qa/test_02_api_contracts.py asserts this key; narrowing must not drop it."""
    stub(sampled=True, hot_seconds=1.0, cold_wh=0.0)
    c = dashboard.perf_cost("1d")
    for key in ("ok", "spend_usd", "energy_kwh", "power_measured", "by"):
        assert key in c, f"/api/perf/cost lost its contracted key {key!r}"


# ---- B3: whose watts are these? -------------------------------------------- #
@pytest.fixture
def watts(monkeypatch):
    """Control the three wattage inputs: samples, owner-declared, platform."""
    def _apply(*, sampled=None, declared=None, platform_w=None):
        monkeypatch.setattr(
            dashboard.hardware, "history",
            lambda *a, **k: ([{"gpu_power": sampled}] if sampled else []))
        monkeypatch.setattr(dashboard, "_cost_cfg", lambda: {
            "prices": {}, "electricity_rate_per_kwh": 0.15, "currency": "$",
            "budgets": {},
            **({"nominal_gpu_watts": declared} if declared else {})})
        monkeypatch.setattr(dashboard, "_power_profile", lambda: {
            "platform_key": "test", "power_source": "nvml-or-smi",
            "nominal_w": platform_w, "provenance": "platforms.conf:test"})
        monkeypatch.setattr(
            dashboard.perf_store, "cold_cost",
            lambda *a, **k: {"spend_usd": 0.0, "energy_wh": 0.0,
                             "energy_estimated_wh": 0.0, "by": {}})
        monkeypatch.setattr(dashboard, "_all_rows", lambda *a, **k: [
            {"ts": dashboard.time.time(), "model": "m", "gen_seconds": 3600.0}])
    return _apply


def test_no_defensible_wattage_reports_not_measured_never_zero(watts) -> None:
    """The whole point of killing the global 180.

    A platform with no nominal and no samples must say NOTHING about energy. Zero
    kWh is a claim about free electricity; 180 W was a claim about someone else's
    GPU.
    """
    watts(sampled=None, declared=None, platform_w=None)
    c = dashboard.perf_cost("1d")
    assert c["energy_kwh"] is None, (
        f"energy_kwh={c['energy_kwh']!r} with no wattage available — must be null")
    assert c["avg_gpu_watts"] is None
    assert c["energy_state"] == "unknown"
    assert c["power_source"] is None
    assert c["energy_usd"] is None


def test_the_platform_nominal_is_used_and_named(watts) -> None:
    watts(sampled=None, declared=None, platform_w=30.0)
    c = dashboard.perf_cost("1d")
    assert c["power_source"] == "platform-nominal"
    assert c["avg_gpu_watts"] == 30.0, (
        "a Mac mini must not be charged a discrete NVIDIA card's 180 W")
    assert c["energy_kwh"] == pytest.approx(0.03, abs=1e-3)
    assert "platforms.conf" in (c["power_provenance"] or "")


def test_an_owner_declared_figure_beats_the_platform_nominal(watts) -> None:
    watts(sampled=None, declared=250.0, platform_w=30.0)
    c = dashboard.perf_cost("1d")
    assert c["power_source"] == "declared"
    assert c["avg_gpu_watts"] == 250.0


def test_real_samples_beat_everything(watts) -> None:
    watts(sampled=77.0, declared=250.0, platform_w=30.0)
    c = dashboard.perf_cost("1d")
    assert c["power_source"] == "sampled"
    assert c["avg_gpu_watts"] == 77.0


def test_dollars_are_gated_harder_than_kilowatt_hours(watts) -> None:
    """kWh may carry an "(est.)" label; a currency figure reads as settled."""
    watts(sampled=None, declared=None, platform_w=30.0)
    est = dashboard.perf_cost("1d")
    assert est["energy_kwh"] is not None, "kWh should still be shown, labelled"
    assert est["energy_usd"] is None, (
        "a platform nominal must not become a dollar figure — nobody declared it "
        "and nothing measured it")

    watts(sampled=None, declared=30.0, platform_w=30.0)
    dec = dashboard.perf_cost("1d")
    assert dec["energy_usd"] is not None, (
        "an owner who declared their card's draw HAS earned a currency figure")
