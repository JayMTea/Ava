"""The domain layer must be removable without taking anything with it.

The claim this layer makes is that it is additive: apps never learn about it,
and deleting it leaves the rest of the product working. A claim like that decays
the moment some unrelated module starts importing it for convenience, so it is
asserted here rather than trusted.

Parsed with `ast`, not grepped: a grep for "import domains" matches a comment,
a docstring and a string literal, and misses `from . import domains as d`.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "ava_bridge"

LAYER = {"domains", "domains_api", "kpi_read", "kpi_store", "kpi_collect"}

# The only places allowed to know the layer exists. Each is a deliberate seam:
# the app mounts its routes and starts its collector; the Data page inventories
# the store it writes (a store the owner cannot see is a store they cannot
# decide about); and the layer's own modules import each other.
ALLOWED = {"data_api"} | LAYER


def _imports(path: pathlib.Path) -> set[str]:
    """Module names this file imports from within ava_bridge."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("ava_bridge"):
                out |= {a.name for a in node.names}
            elif node.level and node.module is None:
                out |= {a.name for a in node.names}      # from . import x
            elif node.level and node.module:
                out.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("ava_bridge."):
                    out.add(a.name.split(".")[-1])
    return out


def test_nothing_outside_the_seams_imports_the_domain_layer() -> None:
    offenders = {}
    for path in sorted(PKG.glob("*.py")):
        if path.stem in ALLOWED:
            continue
        hit = _imports(path) & LAYER
        if hit:
            offenders[path.name] = sorted(hit)
    assert not offenders, (
        f"modules outside the declared seams import the domain layer: {offenders}. "
        "Deleting the layer would now break them, which is the coupling this "
        "layer promises not to create. Add a seam deliberately or invert the call.")


def test_the_layer_does_not_reach_into_private_names() -> None:
    """Importing another module's underscore-private helper forks its contract:
    the owning module can no longer change it without breaking a caller it does
    not know it has."""
    bad = {}
    for name in sorted(LAYER):
        path = PKG / f"{name}.py"
        if not path.exists():
            continue
        private = {n for n in _imports(path) if n.startswith("_")}
        if private:
            bad[path.name] = sorted(private)
    assert not bad, f"domain-layer modules import private names: {bad}"


def test_the_collector_never_writes_to_an_app() -> None:
    """Pull, not push. The direction is the decoupling: if this layer ever POSTs
    into an app, the app has acquired a dependency on being observed."""
    src = (PKG / "kpi_collect.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("post", "put", "patch", "delete")]
    assert not calls, (
        "the collector issues a mutating HTTP call. It reads what apps already "
        "expose; it never writes into one.")
