"""The engine registry — what Ava actually supports, per engine, and how well.

`docs/CHOOSE_A_MODEL.md` used to present six engines as peers: "Ollama, LM Studio,
or MLX on a Mac; vLLM on NVIDIA; llama.cpp anywhere; or any OpenAI-compatible
cloud provider." Three of those had a preset string and prose and nothing else —
no health path, no launcher, no flag row, no pull support. A user picking MLX got
a filled-in base URL and no other help, which is not what "supported" reads as.

So support is declared here, in one place, as six specific affordances rather than
a yes/no. Two engines are `first-class`; the rest are honestly `generic`: they work,
because anything OpenAI-compatible works, but Ava does not launch or tune them.

The six affordances, and why each is load-bearing:

  health_path   how to ask "are you there". Without it the setup wizard cannot
                preselect a running engine, so it recommends cloud to someone who
                just started a local server on purpose.
  launcher      something reproducible that starts it. `None` = bring your own.
  flags_table   whether deploy/model-flags.conf can resolve boot flags for it.
                Only vLLM needs this: it is the one engine where a wrong
                --tool-call-parser silently returns no tool_calls and every turn
                runs to timeout.
  pull          how weights arrive, or None for "you fetch them".
  usage         whether its streaming endpoint accepts
                `stream_options: {include_usage: true}`. This is a DECISION WITH A
                REASON, not a guess — see below.
  platforms     platform_id values it can serve on. Empty = anywhere.

**On `usage`: an unverified exclusion is recorded as unverified.** Token counts
vanish silently when this is wrong, so `router_app` used to carry a bare set with
a comment guessing that "llama.cpp builds may reject unknown params". That guess
may well be right; nobody had measured it. The registry now forces a reason
string, and `usage_verified` says whether anyone actually checked. `llamacpp` and
`mlx` are unverified because neither binary exists on the maintainer's Linux box —
verifying them needs the engine present, which is a CI job on a runner of the
right class, not an opinion.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FIRST_CLASS = "first-class"
GENERIC = "generic"


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    tier: str
    health_path: str
    usage: bool
    usage_reason: str
    usage_verified: bool = False
    launcher: str | None = None
    flags_table: bool = False
    pull: str | None = None
    platforms: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # A local engine consumes THIS box's memory, so model-fit must gate on it.
    local: bool = True
    note: str = ""

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.key,) + self.aliases


ENGINES: tuple[Engine, ...] = (
    Engine(
        key="vllm", label="vLLM", tier=FIRST_CLASS,
        health_path="/models",
        usage=True, usage_reason="verified in production on this box",
        usage_verified=True,
        launcher="deploy/local-serve.sh (and the `vllm` compose service)",
        flags_table=True,
        pull="ava models pull (HuggingFace cache)",
        platforms=("linux-nvidia", "windows-nvidia", "linux-amd"),
        note="Needs CUDA. ROCm is opt-in via inference.allow_vllm_rocm — see "
             "models.engine_servable_here.",
    ),
    Engine(
        key="ollama", label="Ollama", tier=FIRST_CLASS,
        health_path="/api/tags",
        usage=True,
        usage_reason="verified: Ollama omits usage UNLESS include_usage is sent",
        usage_verified=True,
        launcher="the `ollama` / `ollama-rocm` compose services",
        pull="ava models pull --auto (ollama pull)",
        note="The portable GPU path: handles CUDA, ROCm and Metal itself.",
    ),
    Engine(
        key="llamacpp", label="llama.cpp", tier=FIRST_CLASS,
        aliases=("llama.cpp", "gguf", "llamafile"),
        health_path="/health",
        usage=False,
        usage_reason="NOT VERIFIED — no llama-server on the maintainer's box, so "
                     "whether it accepts stream_options is untested. Excluded "
                     "because a rejected param costs the whole turn, while an "
                     "exclusion costs only the token counts.",
        usage_verified=False,
        launcher="bring your own llama-server (no Ava launcher yet)",
        pull="ava models pull (GGUF store)",
        note="Serves anywhere. First-class because it is the only local engine "
             "whose whole path is verifiable on a free CI runner with no GPU.",
    ),
    Engine(
        key="openai", label="Cloud (OpenAI-compatible)", tier=FIRST_CLASS,
        health_path="/models",
        usage=True, usage_reason="verified against OpenAI-compatible providers",
        usage_verified=True,
        launcher=None, pull=None, local=False,
        note="Any provider exposing a /v1 base. No local memory is consumed, so "
             "model-fit does not gate it.",
    ),
    Engine(
        key="mlx", label="MLX (Apple Silicon)", tier=GENERIC,
        aliases=("mlx-lm",),
        health_path="/models",
        usage=False,
        usage_reason="NOT VERIFIED — mlx_lm.server runs only on Apple Silicon, "
                     "which cannot be exercised here or in CI while the repo is "
                     "private (no macos-14 runner).",
        usage_verified=False,
        launcher="mlx_lm.server --model <id> --port 8080 (documented, unverified)",
        pull="huggingface cache (mlx-community/*)",
        platforms=("darwin-apple",),
        note="Wired up and honestly NOT first-class: promoting it needs a macOS "
             "CI job, which needs the repo to be public. Ollama-on-Metal is the "
             "supported Mac path meanwhile.",
    ),
    Engine(
        key="lmstudio", label="LM Studio", tier=GENERIC,
        aliases=("lm-studio",),
        health_path="/models",
        usage=False,
        usage_reason="NOT VERIFIED — closed-source desktop GUI, not scriptable "
                     "in CI.",
        usage_verified=False,
        launcher=None, pull=None,
        note="Deliberately generic. It is a desktop app: it cannot be installed "
             "headless, scripted from an installer, or given a reproducible "
             "launcher, and its endpoint is already covered by the `openai` "
             "engine pointed at 127.0.0.1:1234/v1. Building it a launcher and a "
             "flag table would be inventing evidence. The UI preset stays, "
             "because filling in a base URL is genuinely useful.",
    ),
)

_BY_KEY: dict[str, Engine] = {k: e for e in ENGINES for k in e.keys}


def get(key: str | None) -> Engine | None:
    return _BY_KEY.get((key or "").strip().lower())


def health_paths() -> dict[str, str]:
    """`{engine key: health path}` for every key and alias."""
    return {k: e.health_path for e in ENGINES for k in e.keys}


def usage_engines() -> set[str]:
    """Engines whose streaming endpoint may be sent `include_usage`."""
    return {k for e in ENGINES if e.usage for k in e.keys}


def local_engines() -> set[str]:
    return {k for e in ENGINES if e.local for k in e.keys}


def first_class() -> tuple[Engine, ...]:
    return tuple(e for e in ENGINES if e.tier == FIRST_CLASS)


def render_markdown() -> str:
    """The docs table. Deterministic, so a guard can diff it."""
    rows = ["| Engine | Support | Health | Launcher | Weights | Token counts |",
            "|---|---|---|---|---|---|"]
    for e in ENGINES:
        launcher = e.launcher or "bring your own"
        pull = e.pull or "bring your own"
        if e.usage:
            usage = "yes"
        else:
            usage = "not reported (unverified)" if not e.usage_verified else "no"
        rows.append(f"| **{e.label}** | {e.tier} | `{e.health_path}` "
                    f"| {launcher} | {pull} | {usage} |")
    return "\n".join(rows)


BEGIN = "<!-- engines:begin — generated from ava_bridge/engines.py -->"
END = "<!-- engines:end -->"
_DOC = "docs/CHOOSE_A_MODEL.md"


def splice(text: str) -> str:
    """Replace the marked block with the freshly rendered engine table."""
    i = text.find(BEGIN)
    if i < 0:
        raise ValueError(f"no {BEGIN!r} marker in the target document")
    j = text.find(END, i)
    if j < 0:
        raise ValueError(f"{BEGIN!r} has no matching {END!r}")
    return text[:i] + BEGIN + "\n" + render_markdown() + "\n" + text[j:]


def _main(argv: list[str]) -> int:
    """`python3 -m ava_bridge.engines [--markdown | --sync]`"""
    import os
    args = argv[1:]
    if args and args[0] == "--sync":
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, _DOC)
        with open(path, encoding="utf-8") as f:
            before = f.read()
        after = splice(before)
        if after != before:
            with open(path, "w", encoding="utf-8") as f:
                f.write(after)
            print(f"synced: {_DOC}")
        else:
            print("synced: nothing (up to date)")
        return 0
    print(render_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv))
