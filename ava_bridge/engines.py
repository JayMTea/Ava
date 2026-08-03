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
    # Whether health_path hangs off the server ROOT or off the OpenAI `/v1` base.
    # `/models` is the OpenAI surface and lives under the base; a native path
    # (Ollama's `/api/tags`, llama.cpp's `/health`) does not, and probing
    # `…/v1/api/tags` reported a healthy Ollama as down. That was fixed once, by
    # hand, for Ollama alone inside setup_wizard._health_url — which is why the
    # rule lives here now: llama.cpp declared `/health` and was still probed at
    # `/v1/models`, so the registry described a check nobody ran.
    health_at_root: bool = False
    # Where this engine lists what it is actually serving. Separate from
    # health_path on purpose: llama.cpp answers health at /health but models at
    # /models, so aliasing the two would ask it the wrong question. Ollama is the
    # only engine whose list is not the OpenAI shape, hence its override below.
    models_path: str = "/models"
    # Whether `models_path` answers "what is in memory" or only "what is on
    # disk". Engines that load one model at boot and hold it (vLLM, llama.cpp,
    # MLX) make the two the same question, so listing a model IS observing it
    # resident. Ollama does not: it lists every pulled tag and evicts a model
    # after ~5 min idle, so a listed model may be entirely on disk. Reading a
    # list as residency is what let the monitor claim three resident brains on
    # a box holding none.
    serves_resident: bool = True
    # Where this engine reports a model's ARCHITECTURE — layer count, KV head
    # count, embedding width — which is what a KV-cache size can be computed
    # from. None = it cannot be asked, so a footprint for this engine stays
    # honestly partial rather than being invented from a parameter count.
    # Only Ollama exposes one today; saying so here is cheaper than a caller
    # discovering it by getting nothing back.
    arch_path: str | None = None
    # Where this engine reports the models it is holding IN MEMORY right now,
    # when that is a different question from models_path. None = it cannot be
    # asked, and residency stays honestly unknown rather than assumed.
    resident_path: str | None = None
    launcher: str | None = None
    flags_table: bool = False
    pull: str | None = None
    platforms: tuple[str, ...] = field(default_factory=tuple)
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # A local engine consumes THIS box's memory, so model-fit must gate on it.
    local: bool = True
    # The port this engine listens on when nobody has said otherwise. Load-bearing
    # for the same reason health_path is: the setup probe has to GUESS where an
    # engine might be before anything is configured, and a port it does not know
    # is an engine it cannot find. This lived as a hardcoded ladder inside
    # setup_wizard._engine_of while the candidate list hardcoded its own copy,
    # so `llamacpp` was recognisable-from-a-URL and undiscoverable-in-fact.
    # None = no default worth guessing (a cloud provider has no port of ours).
    default_port: int | None = None
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
        default_port=8002,
        note="Needs CUDA. ROCm is opt-in via inference.allow_vllm_rocm — see "
             "models.engine_servable_here.",
    ),
    Engine(
        key="ollama", label="Ollama", tier=FIRST_CLASS,
        health_path="/api/tags",
        health_at_root=True,
        models_path="/api/tags",
        # /api/tags is the pulled-to-disk inventory; /api/ps is the resident
        # set, with per-model size and the VRAM/RAM split. Verified against a
        # live Ollama: three pulled tags, /api/ps empty until a turn arrives.
        serves_resident=False,
        arch_path="/api/show",
        resident_path="/api/ps",
        usage=True,
        usage_reason="verified: Ollama omits usage UNLESS include_usage is sent",
        usage_verified=True,
        launcher="the `ollama` / `ollama-cuda` / `ollama-rocm` compose services",
        pull="ava models pull --auto (ollama pull)",
        default_port=11434,
        note="The portable GPU path: handles CUDA, ROCm and Metal itself.",
    ),
    Engine(
        key="llamacpp", label="llama.cpp", tier=FIRST_CLASS,
        aliases=("llama.cpp", "gguf", "llamafile"),
        health_path="/health",
        health_at_root=True,
        usage=False,
        usage_reason="NOT VERIFIED — no llama-server on the maintainer's box, so "
                     "whether it accepts stream_options is untested. Excluded "
                     "because a rejected param costs the whole turn, while an "
                     "exclusion costs only the token counts.",
        usage_verified=False,
        launcher="bring your own llama-server (no Ava launcher yet)",
        pull="ava models pull (GGUF store)",
        default_port=8080,
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
        default_port=8080,
        note="Wired up and honestly NOT first-class: promoting it needs a macOS "
             "CI job, which needs the repo to be public. Ollama-on-Metal is the "
             "supported Mac path meanwhile.",
    ),
    Engine(
        key="lmstudio", label="LM Studio", tier=GENERIC,
        aliases=("lm-studio",),
        health_path="/models",
        # Its /v1/models lists everything downloaded, loaded or not, and the
        # native endpoint that distinguishes them is unverifiable here (closed
        # desktop GUI, not scriptable in CI). So: listed is not resident, and
        # with no resident_path residency stays unknown rather than guessed.
        serves_resident=False,
        usage=False,
        usage_reason="NOT VERIFIED — closed-source desktop GUI, not scriptable "
                     "in CI.",
        usage_verified=False,
        launcher=None, pull=None,
        default_port=1234,
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


def default_ports() -> dict[int, str]:
    """`{port: engine key}` for the engines worth guessing at.

    Ports are NOT unique — llama.cpp and MLX both default to 8080 — so a shared
    port resolves to the FIRST-CLASS claimant. That is not a tie-break for its
    own sake: `models.engine_servable_here` refuses MLX anywhere but Apple
    Silicon, and calling an 8080 `llama-server` "mlx" health-checks the wrong
    path, so the ambiguous port must land on the engine that can actually serve.
    """
    out: dict[int, str] = {}
    for e in ENGINES:
        if e.default_port is None:
            continue
        held = out.get(e.default_port)
        if held is None or (_BY_KEY[held].tier != FIRST_CLASS and e.tier == FIRST_CLASS):
            out[e.default_port] = e.key
    return out


def probeable_local() -> tuple[Engine, ...]:
    """Local engines with a port worth trying blind, first-class first.

    This is the list the setup probe sweeps when nothing is configured yet. It
    is ordered rather than a set because the first engine to answer wins the
    wizard's preselection, and a first-class engine answering should beat a
    generic one on the same machine.
    """
    return tuple(sorted((e for e in ENGINES if e.local and e.default_port),
                        key=lambda e: (e.tier != FIRST_CLASS, e.key)))


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
