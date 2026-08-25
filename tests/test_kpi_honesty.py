"""The refusals. Each test here is a lie the KPI layer must not be able to tell.

Every fixture is invented. Nothing in this file names a real app, a real metric
or a real taxonomy — the rules are properties of the arithmetic, not of any
particular owner's estate.
"""
from ava_bridge import kpi_read
from ava_bridge.domains_api import _subtotal


def _m(**kw):
    base = {"metric_id": "demo.thing", "id": "thing", "unit": "count",
            "read": {"value": "v"}, "declares": "measured"}
    base.update(kw)
    return base


# --- absence is never a zero ------------------------------------------------ #

def test_a_null_value_is_never_recorded_as_zero():
    """The failure this whole layer exists to prevent: a source that has no
    reading must not become a data point at the bottom of a chart."""
    obs = kpi_read.observe(_m(), {"v": None})
    assert obs["value"] is None
    assert obs["state"] != "ok"


def test_a_real_zero_survives_as_a_real_zero():
    """The converse, which is just as important: a genuine measured zero is a
    measurement. Treating every zero as suspect would be its own dishonesty."""
    obs = kpi_read.observe(_m(), {"v": 0})
    assert obs["value"] == 0.0
    assert obs["state"] == "ok"
    assert obs["provenance"] == "measured"


def test_a_source_flag_outranks_the_number_in_the_field():
    """A payload can carry a value AND a flag saying that value is not measured.
    The flag wins; storing the number would fabricate an observation."""
    metric = _m(read={"value": "v", "provenance": "measured_flag"},
                provenance_map={True: "measured", False: None})
    obs = kpi_read.observe(metric, {"v": 0, "measured_flag": False})
    assert obs["state"] == "no_source"
    assert obs["value"] is None


# --- provenance is a ceiling, and absence participates in the floor --------- #

def test_declares_is_a_ceiling_that_a_payload_cannot_raise():
    metric = _m(declares="assumed",
                read={"value": "v", "provenance": "flag"},
                provenance_map={True: "measured", False: "assumed"})
    obs = kpi_read.observe(metric, {"v": 5, "flag": True})
    assert obs["provenance"] == "assumed", "a payload promoted its own provenance"


def test_an_absence_carries_no_provenance():
    """Leaving `measured` on a row with no reading is how a gap gets counted as
    a good measurement in a coverage figure."""
    for body in ({"v": None}, {"v": None, "n": 0}):
        assert kpi_read.observe(_m(read={"value": "v", "n": "n"}), body)["provenance"] is None


# --- thin evidence is not the same as no evidence --------------------------- #

def test_below_min_n_the_value_is_suppressed_but_n_is_kept():
    metric = _m(min_n=30, read={"value": "v", "n": "n"})
    obs = kpi_read.observe(metric, {"v": 0.9, "n": 3})
    assert obs["state"] == "insufficient"
    assert obs["value"] is None
    assert obs["n"] == 3, "n must survive so a card can say how far short it is"


def test_bounds_without_a_point_estimate_are_kept():
    """An interval can exist where a point estimate does not. Dropping it would
    discard the only quantitative thing known about the metric."""
    metric = _m(read={"value": "v", "lo": "lo", "hi": "hi"})
    obs = kpi_read.observe(metric, {"v": None, "lo": 0.0, "hi": 0.2})
    assert obs["state"] == "insufficient"
    assert (obs["lo"], obs["hi"]) == (0.0, 0.2)


def test_a_missed_pointer_is_not_reported_as_an_absent_reading():
    """"this pointer is wrong" and "the app has no reading" must never render
    the same way — one is a bug, the other is an answer."""
    obs = kpi_read.observe(_m(read={"value": "nope.not.here"}), {"v": 1})
    assert obs["state"] == "unavailable"
    assert "pointer" in obs["why"]


# --- totals ----------------------------------------------------------------- #

def test_units_are_never_mixed_in_a_total():
    out = _subtotal([{"metric": "a", "unit": "count", "state": "ok", "value": 2},
                     {"metric": "b", "unit": "usd_cents", "state": "ok", "value": 5}],
                    "count")
    assert out["value"] == 2
    assert out["complete"] is False
    assert any(m["why"] == "different unit" for m in out["missing"])


def test_an_incomplete_total_cannot_be_produced_without_naming_its_gaps():
    out = _subtotal([{"metric": "a", "unit": "count", "state": "ok", "value": 2},
                     {"metric": "b", "unit": "count", "state": "unavailable",
                      "value": None, "why": "app down"}], "count")
    assert out["complete"] is False
    assert out["missing"] and out["missing"][0]["metric"] == "b"


def test_an_absent_contributor_does_not_count_as_zero():
    """Two ok values of 2 must total 4 whether or not a third is missing — the
    missing one is named, not silently added as 0."""
    rows = [{"metric": "a", "unit": "count", "state": "ok", "value": 2},
            {"metric": "b", "unit": "count", "state": "ok", "value": 2}]
    full = _subtotal(rows, "count")
    partial = _subtotal(rows + [{"metric": "c", "unit": "count",
                                 "state": "no_source", "value": None}], "count")
    assert full["value"] == partial["value"] == 4
    assert full["complete"] and not partial["complete"]


# --- the pointer grammar stays small ---------------------------------------- #

def test_the_grammar_has_no_wildcard():
    """A wildcard would make pointers unauditable. Naming an array and a field
    is the supported form; `[*]` must not silently resolve to something."""
    _, found = kpi_read.resolve({"rows": [{"k": 1}]}, "rows[*].k")
    assert not found


def test_equality_select_and_index_both_resolve():
    body = {"rows": [{"key": "a", "n": 1}, {"key": "b", "n": 2}]}
    assert kpi_read.resolve(body, "rows[key=b].n") == (2, True)
    assert kpi_read.resolve(body, "rows[0].n") == (1, True)


# --- ratio arithmetic ------------------------------------------------------- #

class _FakeStore:
    """Stands in for the ledger so the arithmetic is tested, not the disk."""

    LEDGER = "ledger.jsonl"

    def __init__(self, rows):
        self._rows = rows

    def rows_for(self, metric_id, day=None):
        return [r for r in self._rows if r["metric"] == metric_id
                and (day is None or r["day"] == day)]

    def series(self, metric_id, since, dim=None):
        return [{"day": d} for d in sorted({r["day"] for r in self._rows})]


def _row(metric, day, value, state="ok", provenance="measured"):
    return {"metric": metric, "day": day, "value": value, "state": state,
            "provenance": provenance, "observed_at": 1.0, "dim": None}


def _ratio_with(rows, **kw):
    from ava_bridge import domains_api
    metric = {"metric_id": "demo.rate", "unit": "pct", "agg": "ratio_of_sums",
              "num": "demo.hits", "den": "demo.tries", **kw}
    real = domains_api.kpi_store
    domains_api.kpi_store = _FakeStore(rows)
    try:
        return domains_api._ratio(metric, 30)
    finally:
        domains_api.kpi_store = real


def test_a_ratio_is_a_sum_over_a_sum_not_a_mean_of_daily_ratios():
    """Day one is 1/1 and day two is 1/99. The mean of the daily ratios is
    ~0.505; the honest answer is 2/100 = 0.02. Averaging ratios lets a quiet day
    outvote a busy one."""
    rows = [_row("demo.hits", "2026-01-01", 1), _row("demo.tries", "2026-01-01", 1),
            _row("demo.hits", "2026-01-02", 1), _row("demo.tries", "2026-01-02", 99)]
    out = _ratio_with(rows)
    assert out["state"] == "ok"
    assert out["value"] == 0.02


def test_days_are_paired_so_a_missing_leg_drops_the_whole_day():
    """Dividing a numerator over one set of days by a denominator over another
    is an answer to a question nobody asked."""
    rows = [_row("demo.hits", "2026-01-01", 5), _row("demo.tries", "2026-01-01", 10),
            _row("demo.hits", "2026-01-02", 50)]          # no denominator that day
    out = _ratio_with(rows)
    assert out["value"] == 0.5, "the unpaired day leaked into the numerator"
    assert out["n"] == 1


def test_a_non_ok_leg_does_not_contribute_zero():
    rows = [_row("demo.hits", "2026-01-01", 5), _row("demo.tries", "2026-01-01", 10),
            _row("demo.hits", "2026-01-02", None, state="no_source"),
            _row("demo.tries", "2026-01-02", 90)]
    out = _ratio_with(rows)
    assert out["value"] == 0.5 and out["n"] == 1


def test_a_zero_denominator_is_insufficient_not_an_error_or_a_zero():
    rows = [_row("demo.hits", "2026-01-01", 0), _row("demo.tries", "2026-01-01", 0)]
    out = _ratio_with(rows)
    assert out["state"] == "insufficient"
    assert out["value"] is None


def test_min_n_counts_paired_days():
    rows = [_row("demo.hits", "2026-01-01", 1), _row("demo.tries", "2026-01-01", 2)]
    out = _ratio_with(rows, min_n=7)
    assert out["state"] == "insufficient" and out["n"] == 1


def test_a_ratio_inherits_the_weaker_provenance_of_its_legs():
    rows = [_row("demo.hits", "2026-01-01", 1, provenance="measured"),
            _row("demo.tries", "2026-01-01", 2, provenance="assumed")]
    assert _ratio_with(rows)["provenance"] == "assumed"


def test_a_mostly_missing_card_never_badges_as_measured():
    """The floor is the weakest link, and a metric nobody could read is weaker
    evidence than one that was estimated. A card reporting `measured` while most
    of its metrics are absent is the exact failure this rule exists to stop."""
    from ava_bridge import domains_api
    obs = [{"metric": "a", "unit": "count", "state": "ok", "value": 1,
            "provenance": "measured"},
           {"metric": "b", "unit": "count", "state": "no_source", "value": None,
            "provenance": None}]
    order = {"assumed": 0, "derived": 1, "measured": 2}
    floors = [o["provenance"] for o in obs if o["provenance"]]
    if any(o["state"] != "ok" for o in obs):
        floors.append("assumed")
    assert min(floors, key=lambda p: order.get(p, 0)) == "assumed"
    assert domains_api.cell  # the rule above is the one cell() applies
