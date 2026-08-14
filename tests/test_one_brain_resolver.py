"""Convention guard: ONE resolver names the brain, ONE compares it to reality.

The rule (CLAUDE.md, "a surface has exactly one home"):

  * `models.effective_brain()` — what CONFIG says Ava thinks with. Never liveness.
  * `models.serving_truth()`   — what the ENGINE is actually serving, and whether
                                 that agrees with the config.

A surface renders both or neither. What it must not do is derive its own answer
from `inference.primary`, `inference.roles.chat`, `AVA_BACKEND_MODEL`,
`load_backends()` or `sandbox_info()`, because a second derivation is free to
disagree with the first — and did:

  * `phone_bridge.api_model_get` gated the header pill on `rt.name == "nemoclaw"`
    while the resolver gates on `rt.name != "direct"`, so on a `remote` runtime
    the resolver said the sandbox WAS the brain and the pill went blank;
  * `setup_wizard.recommend_brain` picked the first backend with a non-empty
    model where the resolver picks `backends[0]`;
  * `BrainPanel.tsx` took the DECISION from the resolver and then rendered the
    NAME from `sandbox_model`, beside a liveness badge fed by /api/hardware — a
    config name and an observed state in one sentence.

That last composition is the one that cost twelve days. From 2026-08-01 the
router served a model id its engine did not have, every completion 404'd, and
the hardware panel showed a green, correctly-named brain throughout, because the
panel read the engine, the router read the config, and nothing compared them.

Style follows tests/test_feature_convention.py: a static scan over tracked
sources that runs anywhere, failing with instructions. It ships GREEN — the
offenders above were fixed rather than grandfathered, which is what makes this a
fix and not a lint.

Run: .venv/bin/python -m pytest tests/test_one_brain_resolver.py -q
"""
import pathlib
import re

from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Modules that may name the brain or compare config to reality, each with the
# reason it is here. A ratchet, in the style of tests/test_module_boundaries.py:
# an entry without a reason is how an allow-list becomes a permanent hole.
ALLOWED = {
    "ava_bridge/models.py":
        "effective_brain() + serving_truth() ARE the two answers",
    "ava_bridge/router_app.py":
        "load_backends() is the backend REGISTRY the resolver reads",
    "ava_bridge/router_host.py":
        "compares the running router's stamp against the config; resolves nothing",
    "ava_bridge/hardware.py":
        "calls serving_truth(); observes the engine, never resolves the brain",
    "ava_bridge/hub/models.py":
        "the backend EDITOR — it WRITES inference.*, and asks the resolver which "
        "one is the brain",
    "ava_bridge/hub/system.py":
        "compares the router's booted signature against ava.yaml; resolves nothing",
    "ava_bridge/settings.py":
        "the config layer itself (role_backend is its own accessor)",
    "ava_bridge/setup_wizard.py":
        "asks the resolver; the remaining match_served() call validates a "
        "CANDIDATE model the owner picked, which is not the brain",
    "ava_bridge/hub/agent.py":
        "same — validates a candidate model and returns the model_unknown code",
    "ava_bridge/runtime/nemoclaw.py":
        "sandbox_info()'s own implementation",
    "ava_bridge/alloc/spec.py":
        "reads backends for FIT profiles (weight/tier), never to pick a brain",
    "ava_bridge/bench.py":
        "benchmarks every backend in turn; picks none",
    "ava_bridge/model_fit.py":
        "scores backends against memory; picks none",
    "ava_cli.py":
        "the CLI reports through the resolver and lists backends for doctor",
    "phone_bridge.py":
        "asks the resolver; the inference.backends mention is a route contract",
}

# Deriving "which model is the brain" from somewhere other than the resolver.
_ATOMS = {
    "sandbox_info(": re.compile(r"\bsandbox_info\s*\("),
    "an inference.* config key": re.compile(
        r"""['"]inference\.(?:primary|roles\.chat|backends)['"]"""),
    "AVA_BACKEND_MODEL": re.compile(r"AVA_BACKEND_MODEL"),
    "load_backends(": re.compile(r"\bload_backends\s*\("),
}
_MATCH_SERVED = re.compile(r"\bmatch_served\s*\(")

_FIX = (
    "\n\nAsk the ONE resolver instead of deriving a second answer:\n"
    "  models.effective_brain()  what CONFIG says Ava thinks with (never liveness)\n"
    "  models.serving_truth()    what the ENGINE is actually serving, and whether\n"
    "                            it disagrees with the config\n"
    "\nA surface renders BOTH or NEITHER. A config name printed beside an\n"
    "observed state is how a router served a model id its engine did not have\n"
    "for twelve days while the hardware panel showed a green, correctly-named\n"
    "brain and every chat turn 404'd.\n"
    "\nIf this module genuinely needs the registry rather than the answer (a\n"
    "backend editor, a benchmark, a fit scorer), add it to ALLOWED above WITH\n"
    "THE REASON.\nSee CLAUDE.md 'a surface has exactly one home'."
)


def _sources():
    for p in _tracked("*.py"):
        rel = p.relative_to(ROOT).as_posix() if p.is_absolute() else p.as_posix()
        if rel.startswith(("tests/", "qa/", "demo/", "agent/")):
            continue
        try:
            yield rel, p.read_text(encoding="utf-8")
        except OSError:
            continue


def test_no_module_derives_its_own_answer_to_which_model_is_the_brain():
    offenders = []
    for rel, src in _sources():
        if rel in ALLOWED or "effective_brain" in src:
            continue
        hits = [name for name, rx in _ATOMS.items() if rx.search(src)]
        if hits:
            offenders.append(f"{rel}: {', '.join(hits)}")
    assert not offenders, (
        "These modules derive the brain themselves instead of asking the "
        "resolver:\n  " + "\n  ".join(offenders) + _FIX)


def test_the_config_reality_comparison_has_one_home():
    """`match_served` is the comparison itself.

    Two modules used to hand-roll it with OPPOSITE consequences: hardware.py
    silently swapped the displayed label, hub/agent.py returned a fix-it code.
    One of those hid the outage. Everything that asks "does the engine have what
    the config named" goes through serving_truth() now.
    """
    offenders = [rel for rel, src in _sources()
                 if rel not in ALLOWED and _MATCH_SERVED.search(src)]
    assert not offenders, (
        "match_served() called outside the modules allowed to compare config to "
        "reality:\n  " + "\n  ".join(offenders) + _FIX)


def test_no_frontend_surface_renders_a_config_name_beside_an_observed_state():
    """The BrainPanel composition, as a rule.

    A component that imports the observed-state vocabulary is rendering
    liveness; if it also reaches for `sandbox_model`/`agent_model` it is putting
    a config name next to that state, which is the exact sentence that read as
    healthy for twelve days. Take the name from `brain.label`/`brain.model` —
    the same resolver that gave you the decision.
    """
    offenders = []
    seen = set()
    for p in _tracked("frontend/src/*.tsx") + _tracked("frontend/src/**/*.tsx"):
        if p in seen:            # the two globs overlap; report a file once
            continue
        seen.add(p)
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith(("//", "*", "/*")))
        if "lib/modelState" not in code and "./modelState" not in code:
            continue
        if re.search(r"\b(?:sandbox_model|agent_model)\b", code):
            rel = p.relative_to(ROOT).as_posix() if p.is_absolute() else p.as_posix()
            offenders.append(rel)
    assert not offenders, (
        "These components render a CONFIG name beside an OBSERVED state:\n  "
        + "\n  ".join(offenders) + _FIX)


def test_every_allowed_path_still_exists_and_carries_a_reason():
    """The ratchet. A deleted file must not leave a permanent hole, and an entry
    without a reason is how the next reader learns nothing from the list."""
    missing = [rel for rel in ALLOWED if not (ROOT / rel).exists()]
    assert not missing, (
        "ALLOWED names files that no longer exist — drop them so the exemption "
        f"cannot outlive its subject: {missing}")
    blank = [rel for rel, why in ALLOWED.items() if not (why or "").strip()]
    assert not blank, f"ALLOWED entries with no reason: {blank}"


def test_the_atoms_still_match_a_real_offender():
    """Non-vacuity.

    A guard whose regexes have quietly stopped matching passes forever while
    enforcing nothing. Each atom is checked against the literal text of a real
    bypass this change removed.
    """
    samples = {
        "sandbox_info(": 'r["agent_model"] = (rt.sandbox_info(wait=False) or {})',
        "an inference.* config key": 'primary = settings.get("inference.primary")',
        "AVA_BACKEND_MODEL": 'model = os.environ.get("AVA_BACKEND_MODEL", "")',
        "load_backends(": "backends = router_app.load_backends()",
    }
    for name, rx in _ATOMS.items():
        assert rx.search(samples[name]), f"the {name!r} atom no longer matches"
    assert _MATCH_SERVED.search("exact = _models.match_served(model, served)")
