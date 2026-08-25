"""Catalogue validation: a hand-edited taxonomy must fail loudly and locally.

This file builds its own synthetic catalogue rather than reading the instance's.
The real one is per-owner and gitignored, so a test that asserted anything about
its contents would pass on one machine and fail on every other — and would put
the owner's vocabulary into tracked source, which is exactly what this layer is
built to avoid.
"""
from ava_bridge import domains

AXES = {"realm": {"order": ["alpha", "beta"]},
        "domain": {"order": ["one", "two"]}}


def _block(**kw):
    base = {"surfaces": [{"id": "demo/main", "realm": "alpha", "domain": "one"}],
            "metrics": [{"id": "widgets", "surface": "demo/main",
                         "unit": "count", "agg": "sum",
                         "source": {"kind": "http"}, "read": {"value": "v"}}]}
    base.update(kw)
    return base


def _collect(block, axes=AXES):
    problems: list[str] = []
    out = domains._collect_surfaces(block, "demo", {}, axes, problems)
    return out, problems


def test_a_good_block_yields_a_surface_with_its_metrics() -> None:
    surfaces, problems = _collect(_block())
    assert not problems
    assert len(surfaces) == 1
    assert surfaces[0]["metrics"][0]["metric_id"] == "demo.widgets"


def test_an_axis_value_outside_the_declared_order_is_refused() -> None:
    """The axes are closed on purpose: a typo that silently created a new
    grouping would split a series in half and look like a real change."""
    _, problems = _collect(_block(surfaces=[{"id": "demo/main", "realm": "typo",
                                             "domain": "one"}]))
    assert any("realm" in p for p in problems)


def test_a_unit_outside_the_closed_vocabulary_is_refused() -> None:
    """"never total across units" is only checkable while units come from a
    closed set; a free-text unit makes the rule unenforceable."""
    block = _block()
    block["metrics"][0]["unit"] = "bananas"
    surfaces, problems = _collect(block)
    assert any("unit" in p for p in problems)
    assert surfaces[0]["metrics"] == []


def test_a_ratio_must_name_both_of_its_legs() -> None:
    """A stored daily ratio cannot be re-aggregated over a window without
    averaging ratios. Naming both legs is what keeps the window arithmetic
    honest, so a ratio that omits them is rejected rather than stored."""
    block = _block()
    block["metrics"][0].update({"agg": "ratio_of_sums", "unit": "pct"})
    surfaces, problems = _collect(block)
    assert any("ratio_of_sums" in p for p in problems)
    assert surfaces[0]["metrics"] == []


def test_an_unknown_provenance_or_state_is_refused() -> None:
    for field, bad in (("declares", "vibes"), ("state", "probably")):
        block = _block()
        block["metrics"][0][field] = bad
        _, problems = _collect(block)
        assert any(field in p for p in problems), f"{field} accepted {bad!r}"


def test_a_metric_without_an_id_is_dropped_not_fatal() -> None:
    """One malformed entry must not take the catalogue down with it — that is
    how a hand-edited file becomes a boot hazard."""
    block = _block()
    block["metrics"].append({"surface": "demo/main", "unit": "count"})
    surfaces, problems = _collect(block)
    assert problems
    assert len(surfaces[0]["metrics"]) == 1, "the good metric survived"


def test_the_module_ships_no_taxonomy_of_its_own() -> None:
    """Axis values belong to the instance. A default vocabulary compiled in here
    would be this product telling every fork how to think about its own estate."""
    import pathlib
    src = pathlib.Path(domains.__file__).read_text(encoding="utf-8")
    assert "axes" in src
    for shipped in ("REALMS =", "DOMAINS =", "DEFAULT_AXES"):
        assert shipped not in src, f"{shipped} hardcodes a taxonomy"


def test_the_closed_vocabularies_are_the_ones_the_reader_layer_uses() -> None:
    """Drift between the validator's vocabulary and the reader's would let a
    state be declared that nothing can ever produce."""
    assert set(domains.STATES) == {"ok", "insufficient", "unavailable", "no_source"}
    assert "ratio_of_sums" in domains.AGGS
    assert set(domains.PROVENANCE) == {"measured", "derived", "assumed"}
