"""vLLM's per-model flags are resolved in exactly ONE place: deploy/model-flags.conf.

The two flags that must match a model — `--tool-call-parser` and
`--reasoning-parser` — used to be chosen in two places: an inline `case` in
deploy/local-serve.sh and a hardcoded `${AVA_TOOL_PARSER:-hermes}` in
deploy/docker-compose.yml. They had already drifted, and the compose copy could
not express a reasoning parser at all, so the container path silently served
reasoning models with no reasoning parser.

A wrong parser does not raise. vLLM returns NO tool_calls, the call arrives as
plain prose, the agent never sees it, and every turn runs to its timeout — it
presents as "Ava is not responding", which is about as far from the cause as a
symptom gets. So the knowledge lives in one table, and this guard keeps it there.

It also pins the ordering, because the table is first-match-wins and a general
glob placed above a specific one silently steals its models
(`*nemotron*` above `*nemotron*omni*` would serve Omni with hermes and return no
tool calls at all).

Style matches tests/test_no_eval_data.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no network. It shells deploy/resolve-model-flags.sh, which is stdlib
bash and touches nothing.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONF = ROOT / "deploy" / "model-flags.conf"
RESOLVER = ROOT / "deploy" / "resolve-model-flags.sh"
DEFAULT_MODEL_ENV = ROOT / "deploy" / "default-model.env"

# Where a parser NAME may legitimately appear outside the table: the resolver's
# own usage examples, release history, and this file.
_NAME_ALLOW = {
    "deploy/model-flags.conf",         # the table itself
    "deploy/resolve-model-flags.sh",   # usage examples in its header
    "tests/test_model_flags_ssot.py",  # this file names what it checks
    "CHANGELOG.md",                    # history, never rewritten
}

# A parser name being RECOMMENDED: `AVA_TOOL_PARSER=hermes`. Deliberately only
# the assignment form — matching `--tool-call-parser <word>` also matches flowing
# prose ("the --tool-call-parser flag is …") and produced nothing but noise.
_NAME_USE = re.compile(
    r"(?:AVA_TOOL_PARSER|AVA_REASONING_PARSER)=([A-Za-z0-9_]*)")


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _rows() -> list[tuple[str, ...]]:
    rows = []
    for raw in CONF.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        assert len(parts) == 6, f"expected 6 pipe-separated columns, got {len(parts)}: {raw!r}"
        rows.append(tuple(parts))
    return rows


def _default_model() -> str:
    for line in DEFAULT_MODEL_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("AVA_MODEL="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"no AVA_MODEL= line in {DEFAULT_MODEL_ENV}")


def _resolve(model: str) -> dict[str, str]:
    out = subprocess.run(["bash", str(RESOLVER), "--env", model],
                         capture_output=True, text=True, check=True).stdout
    return dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)


def test_table_is_well_formed() -> None:
    rows = _rows()
    assert rows, "deploy/model-flags.conf has no rows — did the table move?"

    offenders: list[str] = []
    seen: set[str] = set()
    for glob, family, tool, reason, ctx, _extra in rows:
        if glob in seen:
            offenders.append(f"{glob}: duplicate glob")
        seen.add(glob)
        if not family or family == "-":
            offenders.append(f"{glob}: family must be a label, not '-'")
        if not tool:
            offenders.append(f"{glob}: tool parser column is empty (use '-' for none)")
        if not reason:
            offenders.append(f"{glob}: reasoning parser column is empty (use '-' for none)")
        if ctx != "-" and not ctx.isdigit():
            offenders.append(f"{glob}: native_ctx must be an integer or '-', got {ctx!r}")
        if ctx.isdigit() and int(ctx) < 8192:
            offenders.append(f"{glob}: native_ctx {ctx} is implausibly small")
    assert not offenders, (
        "deploy/model-flags.conf rows are malformed — the format is "
        "`glob | family | tool_parser | reasoning_parser | native_ctx | extra_flags` "
        f"with '-' meaning empty: {offenders}"
    )


def test_specific_globs_precede_the_general_ones_they_overlap() -> None:
    """First match wins, so a general glob above a specific one steals its models."""
    globs = [r[0] for r in _rows()]
    offenders: list[str] = []
    for i, general in enumerate(globs):
        core = general.strip("*")
        for j, specific in enumerate(globs):
            if j <= i or general == specific:
                continue
            # `specific` would never be reached if `general` matches everything it does.
            probe = specific.replace("*", "")
            if core and core in probe:
                offenders.append(f"{specific} (line {j + 1}) is unreachable behind {general} (line {i + 1})")
    assert not offenders, (
        "these globs can never match, because an earlier, more general glob "
        "claims their models first (first match wins). Move the specific glob "
        f"ABOVE the general one — {offenders}"
    )


def test_no_document_recommends_a_parser_the_table_does_not_know() -> None:
    """Docs and examples may show a parser name; they may not show one the table
    has never heard of. That is how a doc keeps recommending `hermes` for a model
    family whose row has since moved to something else."""
    known = set()
    for _glob, _family, tool, reason, _ctx, _extra in _rows():
        known.update(t for t in (tool, reason) if t and t != "-")

    offenders: list[str] = []
    for pattern in ("*.yml", "*.yaml", "*.sh", "*.py", "*.md", "*.conf", ".env.example"):
        for rel in _tracked(pattern):
            if rel in _NAME_ALLOW:
                continue
            for name in _NAME_USE.findall(_read(rel)):
                # `AVA_REASONING_PARSER=` with no value is the documented way to
                # say "deliberately pass no flag".
                if name and name not in known:
                    offenders.append(f"{rel}: {name}")
    assert not offenders, (
        "these files name a vLLM parser that does not appear in "
        "deploy/model-flags.conf. Either the table lost a family or the doc is "
        "stale — a parser that does not match the model returns NO tool_calls "
        f"silently, so a wrong recommendation is worse than none: {sorted(set(offenders))}"
    )


def test_local_serve_has_no_inline_table() -> None:
    src = _read("deploy/local-serve.sh")
    assert "resolve-model-flags.sh" in src, (
        "deploy/local-serve.sh must source deploy/resolve-model-flags.sh rather "
        "than resolving parsers itself")
    assert 'case "$lc_model"' not in src, (
        "deploy/local-serve.sh has re-grown an inline per-family `case` — that is "
        "the second source of truth this table exists to remove")


def test_compose_vllm_fallbacks_match_the_resolved_default_model() -> None:
    """The vllm service keeps `:-` fallbacks for the two flag variables so that
    setting only AVA_MODEL (to the shipped default) still works. They are a copy
    of the resolver's answer, so they may exist only while they still agree."""
    model = _default_model()
    resolved = _resolve(model)
    compose = _read("deploy/docker-compose.yml")

    offenders: list[str] = []
    want_len = resolved.get("AVA_VLLM_MAX_LEN", "")
    if f"${{AVA_VLLM_MAX_LEN:-{want_len}}}" not in compose:
        offenders.append(f"AVA_VLLM_MAX_LEN fallback is not {want_len!r}")
    want_flags = resolved.get("AVA_VLLM_MODEL_FLAGS", "")
    if f"${{AVA_VLLM_MODEL_FLAGS:-{want_flags}}}" not in compose:
        offenders.append(f"AVA_VLLM_MODEL_FLAGS fallback is not {want_flags!r}")

    assert not offenders, (
        "deploy/docker-compose.yml's vllm fallbacks have drifted from what "
        f"deploy/resolve-model-flags.sh resolves for {model} (the shipped default "
        f"in deploy/default-model.env). Re-run `deploy/resolve-model-flags.sh "
        f"--env {model}` and paste the values into the vllm `command:` — {offenders}"
    )


def test_every_profile_pins_the_shipped_default_model() -> None:
    """A profile that serves a LOCAL engine must name the shipped default, so
    "change the default model" stays a one-line edit in default-model.env.
    cloud.env is exempt: its model is the user's provider's, and ships empty."""
    model = _default_model()
    offenders: list[str] = []
    for rel in _tracked("deploy/profiles/*.env"):
        src = _read(rel)
        declared = [ln.split("=", 1)[1].strip()
                    for ln in src.splitlines() if ln.startswith("AVA_MODEL=")]
        if not declared:
            offenders.append(f"{rel}: no AVA_MODEL line")
            continue
        got = declared[0]
        engine = next((ln.split("=", 1)[1].strip() for ln in src.splitlines()
                       if ln.startswith("AVA_BACKEND_ENGINE=")), "")
        if engine == "openai":
            continue                      # the user's provider, not ours
        if engine == "ollama":
            continue                      # Ollama tags are its own namespace
        if got != model:
            offenders.append(f"{rel}: AVA_MODEL={got!r}, expected {model!r}")
    assert not offenders, (
        "these profiles name a model other than the shipped default in "
        "deploy/default-model.env, so changing the default would silently leave "
        f"them behind — {offenders}"
    )


def test_compose_requires_a_backend_rather_than_guessing_one() -> None:
    """The `ava` service starts under EVERY profile, so a single global default
    for the backend trio is wrong for most of them. `http://vllm:8002/v1` pointed
    `--profile cpu` at a service that profile never starts."""
    compose = _read("deploy/docker-compose.yml")
    offenders = [
        var for var in ("AVA_BACKEND_URL", "AVA_BACKEND_ENGINE")
        if re.search(rf"{var}:\s*\$\{{{var}:-", compose)
    ]
    if re.search(r"AVA_BACKEND_MODEL:\s*\$\{AVA_MODEL:-", compose):
        offenders.append("AVA_BACKEND_MODEL")
    assert not offenders, (
        "these carry a `:-` default in deploy/docker-compose.yml. The bridge runs "
        "under every profile, so a default that suits one silently misconfigures "
        "the others — use `${VAR:?<instruction>}` and put the value in "
        f"deploy/profiles/<profile>.env instead: {offenders}"
    )
