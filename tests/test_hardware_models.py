"""hardware.py model inventory: engine worker processes merge into one row and
model identity is READ (process cmdline / backend config / live API) — never
assumed from the runtime kind — so any self-hosted model is labeled honestly.

Guards the regression where every vLLM-ish GPU process was hardcoded to
"open-model 30B", which (a) showed the model twice in the hardware monitor's
picker (launcher + EngineCore worker = two PIDs, same name) and (b) would
mislabel whatever model a fork actually serves.

And, underneath that, the regression where the panel showed NOTHING: a compose
install whose brain answered every turn at `http://ollama:11434/v1` rendered
"No model process detected yet", because the inventory kept only four loopback
hostname literals and had no nvidia-smi to fall back on. So the two questions
are now asked separately, and `BrainVisibility` pins the split: which row is
the brain is CONFIG (`models.effective_brain`), knowable with zero telemetry;
whether it is up and holding weights is OBSERVED (`probe_serving` /
`probe_resident`) and lives in `state`.

Run: .venv/bin/python -m pytest tests/test_hardware_models.py -q
"""
import contextlib
import unittest
from unittest import mock

from ava_bridge import hardware
from ava_bridge import models as _models
from ava_bridge import router_app

# A vLLM-style engine: launcher (declares --model) + bare-cmdline worker,
# plus an unrelated the GPU service process. Deliberately NOT an NVIDIA/Ava model —
# the inventory must label it from the cmdline, not from a built-in name.
SMI_COMPUTE_APPS = (
    "100, python3, 170\n"
    "101, VLLM::EngineCore, 64000\n"
    "200, /opt/gpusvc/.venv/bin/python, 512\n"
)
CMDLINES = {
    100: "/usr/bin/python3 /usr/local/bin/vllm serve --model acme/Cool-LLM-7B-FP8 --port 9999",
    101: "VLLM::EngineCore",
    200: "/opt/gpusvc/.venv/bin/python /opt/the GPU service/main.py",
}
PPIDS = {101: 100, 100: 50, 200: 60}  # 101's parent is the launcher; 50/60 = shells


def _fake_run(cmd, timeout=4):
    if any("--query-compute-apps" in c for c in cmd):
        return SMI_COMPUTE_APPS
    return ""  # pmon (per-process util): unavailable on this fake box


class GpuProcessGrouping(unittest.TestCase):
    def setUp(self):
        for p in (
            mock.patch.object(hardware.shutil, "which", lambda name: f"/usr/bin/{name}"),
            mock.patch.object(hardware, "_run", _fake_run),
            mock.patch.object(hardware, "_proc_cmdline", lambda pid: CMDLINES.get(pid, "")),
            mock.patch.object(hardware, "_proc_ppid", PPIDS.get),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_one_engine_many_workers_is_one_row(self):
        rows = hardware._gpu_model_processes()
        vllm = [r for r in rows if r["name"] == "vLLM"]
        self.assertEqual(len(vllm), 1, f"launcher+worker must merge, got {vllm}")
        row = vllm[0]
        self.assertEqual(row["id"], "pid:100")          # stable id = owning launcher
        self.assertEqual(row["pid"], 101)               # surfaced pid = weight-holding worker
        self.assertEqual(row["memory_mb"], 64170)       # memory = family total

    def test_model_name_read_from_cmdline_not_assumed(self):
        row = [r for r in hardware._gpu_model_processes() if r["name"] == "vLLM"][0]
        self.assertEqual(row["model"], "Cool-LLM-7B-FP8")
        self.assertEqual(row["model_id"], "acme/Cool-LLM-7B-FP8")
        self.assertNotIn("Omni", row["model"])

    def test_unrelated_process_stays_its_own_row(self):
        rows = hardware._gpu_model_processes()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r for r in rows if r["name"] == "the GPU service"][0]["pid"], 200)


# `_loaded_models` no longer parses /models itself — it asks
# ava_bridge.models.probe_serving, which is now the ONE place that knows each
# engine's list path and response shape (hardware.py hardcoded `/models` and the
# OpenAI `data[].id` envelope, so an Ollama backend worked only via its /v1
# compatibility layer and a llama.cpp one was asked at the wrong path entirely).
#
# So these patch that collaborator rather than the HTTP client. The tuple is
# (reachable, ids): "up but serving nothing" and "down" are different rows, and
# collapsing them would report a running-but-empty Ollama as offline. The
# parsing itself is covered directly in tests/test_served_models.py.
def _serving(ids):
    return lambda url, engine="", key="", timeout=0: (True, list(ids))


def _unreachable(url, engine="", key="", timeout=0):
    return (False, [])


BACKEND = {"id": "brain", "url": "http://127.0.0.1:9999/v1",
           "model": "acme/Cool-LLM-7B-FP8", "label": "Cool LLM", "engine": "vllm"}


class BackendCrossCheck(unittest.TestCase):
    """_loaded_models ties rows to configured backends instead of a name map."""

    def setUp(self):
        self.proc_row = {
            "id": "pid:100", "name": "vLLM", "model": "Cool-LLM-7B-FP8",
            "model_id": "acme/Cool-LLM-7B-FP8", "memory_mb": 64170.0,
            "memory_gb": 62.67, "gpu_util": 12.0, "pid": 101,
            "status": "loaded", "source": "nvidia-smi",
            "cmd": CMDLINES[100],
        }
        for p in (
            mock.patch.object(hardware, "_docker_model_containers", lambda: []),
            mock.patch.object(hardware, "_configured_backends", lambda: [dict(BACKEND)]),
            # Same seam BrainVisibility holds still below, and for the same
            # reason: `effective_brain()`'s FIRST branch is the agent sandbox,
            # so left real this class asks the developer's own box whether it
            # has one. On a machine with a live sandbox `_loaded_models` then
            # (correctly) appends an "agent:sandbox" row and every count here
            # is off by one — a test that passes on CI and fails on the author's
            # laptop is not testing the backend cross-check it claims to.
            mock.patch.object(_models, "effective_brain", lambda: dict(_brain())),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_running_engine_tags_existing_row_no_duplicate(self):
        with mock.patch.object(hardware, "_gpu_model_processes",
                               lambda: [dict(self.proc_row)]), \
             mock.patch.object(_models, "probe_serving",
                               _serving(["acme/Cool-LLM-7B-FP8"])):
            rows = hardware._loaded_models()
        self.assertEqual(len(rows), 1, "API cross-check must not add a second row")
        self.assertEqual(rows[0]["backend"], "brain")
        self.assertEqual(rows[0]["model"], "Cool LLM")  # display label from config
        served = [c for c in rows[0]["components"] if c["kind"] == "served-model"]
        self.assertEqual(served[0]["name"], "acme/Cool-LLM-7B-FP8")  # real id still shown

    def test_configured_engine_down_shows_offline_row_labeled_from_config(self):
        with mock.patch.object(hardware, "_gpu_model_processes", lambda: []), \
             mock.patch.object(_models, "probe_serving", _unreachable):
            rows = hardware._loaded_models()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "offline")
        self.assertEqual(rows[0]["model"], "Cool LLM")

    def test_unnamed_engine_row_claimed_by_api_not_duplicated(self):
        bare = dict(self.proc_row, model="vLLM model", model_id=None, cmd="VLLM::EngineCore")
        with mock.patch.object(hardware, "_gpu_model_processes", lambda: [bare]), \
             mock.patch.object(_models, "probe_serving",
                               _serving(["acme/Cool-LLM-7B-FP8"])):
            rows = hardware._loaded_models()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "Cool LLM")
        self.assertEqual(rows[0]["model_id"], "acme/Cool-LLM-7B-FP8")


# --- brain visibility -------------------------------------------------------- #
# Same seam discipline as above, one layer wider. `probe_serving` answers "is it
# up, and what does it list"; `probe_resident` answers "what is in memory right
# now", and is THREE-valued — None = this engine cannot be asked, [] = it
# answered and holds nothing, [...] = these. Collapsing None into [] reports
# every unaskable engine as empty; collapsing [] into None re-invents the bug
# where three pulled Ollama tags each claimed to be a resident brain.
#
# `effective_brain` is faked separately from both, because identity is config
# and liveness is observation — the entire point of the class below.
def _resident(entries):
    return lambda url, engine="", key="", timeout=0: [dict(e) for e in entries]


def _cannot_ask_residency(url, engine="", key="", timeout=0):
    """vLLM/llama.cpp/LM Studio: no residency endpoint, so residency is unknown."""
    return None


def _ps(model_id, size_mb=4800, vram_mb=4800):
    """One /api/ps entry, already parsed by models.probe_resident."""
    return {"id": model_id, "size_bytes": size_mb * 1024 * 1024,
            "vram_bytes": vram_mb * 1024 * 1024}


def _by_url(table):
    """Per-backend `probe_serving` answers; a URL not in the table is down.

    Keyed by URL because two backends are two engines: handing them one canned
    reply is how a test stops noticing that they got mixed up.
    """
    return lambda url, engine="", key="", timeout=0: (
        (True, list(table[url])) if url in table else (False, []))


def _resident_by_url(table):
    return lambda url, engine="", key="", timeout=0: (
        None if table.get(url) is None else [dict(e) for e in table[url]])


# What Ollama actually answers on a box with three tags pulled: a disk
# inventory, not a residency report.
OLLAMA_TAGS = ["llama3.1:8b", "mistral:latest", "qwen2.5:7b"]

# The row-level residency tri-state each state token implies. Pinned here so a
# row we could not observe ("remote", "unknown") can never answer "no" — that is
# the same lie as reporting a model Ava cannot see as missing.
IN_MEMORY_FOR = {"resident": True, "idle": False, "absent": False,
                 "offline": False, "remote": None, "unknown": None}

# The identity of a brain row, independent of which hostname spelling produced it.
BRAIN_SHAPE = ("id", "backend", "model", "model_id", "state", "status",
               "role_key", "local", "in_memory")

gpusvc_PROC = {"id": "pid:200", "name": "the GPU service", "model": "the GPU service",
              "model_id": None, "memory_mb": 512.0, "memory_gb": 0.5,
              "gpu_util": 0.0, "pid": 200, "status": "loaded",
              "source": "nvidia-smi", "cmd": CMDLINES[200]}


def _backend(bid="brain", url="http://127.0.0.1:11434/v1", engine="ollama",
             model="llama3.1:8b", label="Llama 3.1 8B", key="", local=True):
    """A row shaped like `_configured_backends()` output — `local` already
    decided and CARRIED, rather than non-loopback rows being dropped."""
    return {"id": bid, "url": url, "engine": engine, "model": model,
            "label": label, "api_key": key, "local": local}


def _brain(backend_id="brain", **over):
    """`models.effective_brain()`'s shape: pure config, no liveness in it."""
    return {"source": "configured", "backend_id": backend_id, "model_id": "",
            "label": "", "engine": "", "base_url": "", "api_key": "",
            "local": True, "implicit": False, **over}


class BrainVisibility(unittest.TestCase):
    """Exactly one row is the brain — on any box, at any hostname spelling.

    The monitor told a Docker user "No model process detected yet" while the
    model it was asking about answered every message: `_configured_backends`
    kept four loopback hostname literals, so `http://ollama:11434/v1` — the URL
    every compose install is handed — was discarded as if it were a cloud
    endpoint, and on a laptop with no nvidia-smi nothing else could fill the
    panel. Two rules fall out, and they are what this class pins:

      IDENTITY IS CONFIG. The brain is whichever backend `effective_brain()`
      resolves to, so it is knowable with zero telemetry — no GPU, no docker,
      no /proc, not even a reachable engine. An OFFLINE brain is still the
      brain; it is just offline.

      LIVENESS IS OBSERVED, separately, as `state`. Never inferred from the
      existence of config, and never from a list of what is on DISK: `/api/tags`
      lists every pulled tag, and reading that as residency is what claimed
      three resident brains on a box holding none.
    """

    def _rows(self, backends, brain, serving=None, resident=_cannot_ask_residency,
              procs=()):
        """`_loaded_models()` with the collaborator seams held still.

        `backends=None` leaves `_configured_backends` REAL (so the locality
        decision that shipped is the one under test); `procs=None` leaves
        process/container discovery real (so "this box has no telemetry" is a
        genuinely empty scan rather than a stub).
        """
        serving = serving or _serving([])
        with contextlib.ExitStack() as es:
            for pt in (
                mock.patch.object(_models, "effective_brain", lambda: dict(brain)),
                mock.patch.object(_models, "probe_serving", serving),
                mock.patch.object(_models, "probe_resident", resident),
            ):
                es.enter_context(pt)
            if backends is not None:
                es.enter_context(mock.patch.object(
                    hardware, "_configured_backends",
                    lambda: [dict(b) for b in backends]))
            if procs is not None:
                es.enter_context(mock.patch.object(
                    hardware, "_gpu_model_processes",
                    lambda: [dict(r) for r in procs]))
                es.enter_context(mock.patch.object(
                    hardware, "_docker_model_containers", lambda: []))
            return hardware._loaded_models()

    def _only_brain(self, rows, why=""):
        found = [r for r in rows if r.get("role_key") == "brain"]
        self.assertEqual(
            len(found), 1,
            f"exactly one row must be the brain{why}; got "
            f"{[(r.get('id'), r.get('role_key'), r.get('state')) for r in rows]}")
        return found[0]

    # I1 — existence + uniqueness, across every shape a real install takes.
    def test_exactly_one_row_is_the_brain_in_every_deployment_shape(self):
        cases = [
            # (name, backends, effective_brain, serving, resident, state)
            ("compose hostname", [_backend(url="http://ollama:11434/v1")],
             _brain(), _serving(OLLAMA_TAGS), _resident([]), "idle"),
            ("loopback ollama", [_backend()], _brain(), _serving(OLLAMA_TAGS),
             _resident([_ps("llama3.1:8b")]), "resident"),
            ("loopback vllm",
             [_backend(url="http://127.0.0.1:8000/v1", engine="vllm",
                       model="acme/Cool-LLM-7B-FP8", label="Cool LLM")],
             _brain(), _serving(["acme/Cool-LLM-7B-FP8"]),
             _cannot_ask_residency, "resident"),
            # Not on this box, so it cannot be found by looking at this box's
            # memory — and the owner must still be able to see what Ava thinks
            # with. A key on a cloud backend is normal, not a probe failure.
            ("cloud backend",
             [_backend(bid="cloud", engine="openai", url="https://api.example.test/v1",
                       model="acme-cloud-mini", label="Acme Cloud",
                       key="sk-cloud", local=False)],
             _brain("cloud"), _serving(["acme-cloud-mini"]),
             _cannot_ask_residency, "remote"),
            # The agent runtime answers turns inside its own sandbox, bypassing
            # the router, so it has no backend row of its own — and we cannot
            # see into the sandbox, so residency is unknown, never absent.
            ("agent sandbox", [_backend(bid="fast")],
             _brain(backend_id="", source="agent",
                    model_id="acme/Reasoner-30B-A3B", label="Reasoner-30B-A3B"),
             _serving(OLLAMA_TAGS), _resident([]), "unknown"),
            ("unreachable engine", [_backend()], _brain(), _unreachable,
             _cannot_ask_residency, "offline"),
        ]
        for name, backends, brain, serving, resident, state in cases:
            with self.subTest(name):
                rows = self._rows(backends, brain, serving, resident)
                row = self._only_brain(rows, f" ({name})")
                self.assertEqual(row["state"], state)
                self.assertIs(row["in_memory"], IN_MEMORY_FOR[state])
                # The panel exists to answer "what is Ava thinking with", so the
                # brain sorts first — never below a fatter render process.
                self.assertEqual(rows[0]["role_key"], "brain")

    # I2 — the laptop. No GPU tooling, no docker, no process rows at all.
    def test_a_box_with_no_nvidia_smi_and_no_docker_still_shows_the_brain(self):
        """The regression that started this. Every binary the inventory shells
        out to is absent, so process and container discovery run for real and
        find genuinely nothing — and the brain, being config, is still there."""
        with mock.patch.object(hardware.shutil, "which", lambda name: None):
            rows = self._rows([_backend()], _brain(), _serving(OLLAMA_TAGS),
                              _resident([]), procs=None)
        row = self._only_brain(rows, " on a box with no telemetry at all")
        self.assertEqual(row["backend"], "brain")
        self.assertEqual(row["source"], "api")   # config + one HTTP read, no /proc
        self.assertEqual(row["state"], "idle")

    # I3 — THE core bug: one backend, four ways to spell the same host.
    def test_the_same_backend_at_four_hostnames_gives_the_same_brain_row(self):
        """`_configured_backends` is REAL here — only the router registry under
        it is faked — because the dropped-backend bug lived in exactly the code
        this leaves running. The compose spelling used to yield ZERO rows."""
        hosts = ("127.0.0.1", "localhost", "ollama", "host.docker.internal")
        shapes = []
        for host in hosts:
            with self.subTest(host=host):
                raw = {"id": "brain", "url": f"http://{host}:11434/v1",
                       "engine": "ollama", "model": "llama3.1:8b",
                       "label": "Llama 3.1 8B"}
                with mock.patch.object(router_app, "load_backends",
                                       lambda raw=raw: [dict(raw)]):
                    rows = self._rows(None, _brain(), _serving(OLLAMA_TAGS),
                                      _resident([_ps("llama3.1:8b")]))
                self.assertEqual(len(rows), 1,
                                 f"{host}: a configured backend must never be dropped")
                row = self._only_brain(rows, f" at {host}")
                self.assertIs(row["local"], True,
                              "a compose service name is this box, not a cloud endpoint")
                shapes.append({k: row[k] for k in BRAIN_SHAPE})
        self.assertEqual(shapes, [shapes[0]] * len(hosts),
                         f"one backend spelled {hosts} must give one brain row: {shapes}")

    # I5 — residency is observed, and "listed" is not "loaded".
    def test_three_pulled_tags_and_an_empty_ps_are_one_idle_row(self):
        """Not three rows claiming "resident". /api/tags is the disk inventory;
        Ollama evicts after ~5 min idle, so a listed model may be entirely on
        disk, and one BACKEND is one row however many tags it has pulled."""
        rows = self._rows([_backend()], _brain(), _serving(OLLAMA_TAGS), _resident([]))
        self.assertEqual(len(rows), 1,
                         f"one row per backend, never one per pulled tag: {rows}")
        row = self._only_brain(rows)
        self.assertEqual(row["state"], "idle")
        self.assertIs(row["in_memory"], False)
        self.assertEqual(row["status"], "empty")     # the back-compat field agrees
        self.assertEqual(row["served"], OLLAMA_TAGS)  # evidence kept, just not read
        self.assertIsNone(row["vram_mb"])            # nothing in VRAM to report

    def test_a_model_the_ps_endpoint_lists_is_resident(self):
        rows = self._rows([_backend()], _brain(), _serving(OLLAMA_TAGS),
                          _resident([_ps("llama3.1:8b", size_mb=4800, vram_mb=4000)]))
        row = self._only_brain(rows)
        self.assertEqual(row["state"], "resident")
        self.assertIs(row["in_memory"], True)
        self.assertEqual(row["memory_mb"], 4800.0)
        # The VRAM/RAM split is the only answer to "is my GPU doing anything
        # for this model" on a box where nvidia-smi says nothing useful.
        self.assertEqual(row["vram_mb"], 4000.0)

    def test_a_model_the_engine_does_not_have_is_absent_not_idle(self):
        """Configured but never pulled — the most common broken first install.
        "idle" would tell the owner to wait for something that never loads."""
        rows = self._rows([_backend()], _brain(),
                          _serving(["mistral:latest", "qwen2.5:7b"]), _resident([]))
        row = self._only_brain(rows)
        self.assertEqual(row["state"], "absent")
        self.assertEqual(row["model_id"], "llama3.1:8b")  # still named from config

    def test_the_agent_brain_still_has_a_row_before_its_name_is_known(self):
        """`sandbox_info(wait=False)` serves a 120s cache and returns None while
        it is cold, so the model id arrives a beat after the runtime does.

        Requiring the id before emitting the row meant the first caller after a
        restart saw the ROUTER's backend as the brain and the next one saw the
        sandbox — /api/health and this monitor naming different models on one
        screen, which is the disagreement the resolver exists to end. The row is
        emitted on the runtime, and the name fills itself in.
        """
        rows = self._rows([_backend()],
                          _brain(backend_id="", source="agent", model_id="",
                                 label="", engine="nemoclaw"),
                          _serving(OLLAMA_TAGS), _resident([]))
        row = self._only_brain(rows, " while the sandbox name is still loading")
        self.assertEqual(row["id"], "agent:sandbox")
        self.assertEqual(row["model"], "Agent sandbox")
        self.assertEqual(row["state"], "unknown")
        self.assertIsNone(row["in_memory"], "unknown residency is not 'no'")
        # And the router's own backend is present, but is NOT the brain.
        self.assertEqual([r["role_key"] for r in rows if r.get("backend") == "brain"], [""])

    def test_an_engine_with_an_empty_store_is_absent_not_idle(self):
        """The fresh compose install, which is the shipped first-run path:
        deploy/docker-compose.yml says the Ollama model store starts EMPTY and
        install.sh downgrades a failed pull to a warning.

        Requiring a NON-EMPTY inventory before saying "absent" made this exact
        case report "Ready, not loaded — the engine has it", about a model that
        is not on the disk. An empty store is an observation, not an absence of
        one: probe_serving returns (True, []) only when the engine answered.
        """
        rows = self._rows([_backend()], _brain(), _serving([]), _resident([]))
        row = self._only_brain(rows)
        self.assertEqual(row["state"], "absent")
        self.assertEqual(row["served"], [])

    def test_a_boot_loading_engine_listing_nothing_is_unknown_not_absent(self):
        """The other side of that rule, so it cannot be over-applied. vLLM loads
        one model at boot; while it is still starting it lists nothing, which is
        not the same as having nothing. It has no residency endpoint either, so
        the honest answer is that we could not tell."""
        rows = self._rows(
            [_backend(url="http://127.0.0.1:8000/v1", engine="vllm",
                      model="acme/Cool-LLM-7B-FP8", label="Cool LLM")],
            _brain(), _serving([]), _cannot_ask_residency)
        self.assertEqual(self._only_brain(rows)["state"], "unknown")

    def test_a_running_container_does_not_prove_a_model_is_in_memory(self):
        """A docker row's "loaded" means the CONTAINER is up. For an Ollama
        container that says nothing about whether weights are resident, so it
        must not override the engine's own observed-empty /api/ps — which is the
        disk-listing lie this whole change exists to end. Only a GPU compute
        process holding memory outranks the engine's answer."""
        ctr = {"id": "ctr:ollama", "name": "Ollama", "model": "Ollama",
               "model_id": None, "memory_mb": 51.0, "memory_gb": 0.05,
               "gpu_util": None, "pid": None, "status": "loaded",
               "source": "docker", "cmd": "serve"}
        rows = self._rows([_backend()], _brain(), _serving(OLLAMA_TAGS),
                          _resident([]), procs=[ctr])
        row = self._only_brain(rows)
        self.assertEqual(row["state"], "idle")
        self.assertIs(row["in_memory"], False)

    def test_a_backend_that_is_not_on_this_box_is_never_probed(self):
        """/api/hardware is polled every 2s by every open client. Probing a
        cloud provider on that timer would send the owner's API key off the box
        forever, to learn something the row does not use — what is in THIS
        machine's memory. Locality is decided from config, before any network."""
        calls = []

        def _spy(url, engine="", key="", timeout=0):
            calls.append(url)
            return (True, ["gpt-4o"])

        rows = self._rows([_backend(bid="cloud", url="https://api.openai.com/v1",
                                    engine="openai", model="gpt-4o",
                                    label="GPT-4o", key="sk-secret", local=False)],
                          _brain("cloud"), _spy)
        self.assertEqual(calls, [], "a non-local backend must not be probed")
        row = self._only_brain(rows)
        self.assertEqual(row["state"], "remote")
        self.assertIsNone(row["in_memory"], "remote is unknown residency, not False")

    def test_an_engine_that_cannot_be_reached_is_offline_not_absent(self):
        rows = self._rows([_backend()], _brain(), _unreachable)
        row = self._only_brain(rows)
        self.assertEqual(row["state"], "offline")
        self.assertEqual(row["status"], "offline")

    # I6 — everything else on the box is NOT the brain.
    def test_a_render_process_row_is_never_the_brain(self):
        with mock.patch.object(hardware, "_read_mapped_model_components", lambda pid: []), \
             mock.patch.object(hardware, "_read_open_model_components", lambda pid: []), \
             mock.patch.object(hardware, "_proc_maps_readable", lambda pid: False):
            rows = self._rows([_backend()], _brain(), _serving(OLLAMA_TAGS),
                              _resident([]), procs=[gpusvc_PROC])
        self.assertEqual(len(rows), 2)
        self._only_brain(rows, " with a the GPU service process also running")
        gpusvc = [r for r in rows if r["name"] == "the GPU service"][0]
        self.assertEqual(gpusvc["role_key"], "render")

    def test_a_second_configured_backend_is_never_the_brain(self):
        """`backend` only means "we tied this row to some configured backend".
        Any truthy backend tag used to win, so a second — and even an
        unreachable — backend read as the brain too.

        The two backends are deliberately different ENGINES; the same-engine
        pair is pinned separately below, because it used to fail here.
        """
        urls = {"http://127.0.0.1:11434/v1": OLLAMA_TAGS,
                "http://127.0.0.1:8000/v1": ["acme/Cool-LLM-7B-FP8"]}
        rows = self._rows(
            [_backend(),
             _backend(bid="big", url="http://127.0.0.1:8000/v1", engine="vllm",
                      model="acme/Cool-LLM-7B-FP8", label="Cool LLM")],
            _brain("brain"), _by_url(urls),
            _resident_by_url({"http://127.0.0.1:11434/v1": [],
                              "http://127.0.0.1:8000/v1": None}))
        self.assertEqual(len(rows), 2)
        row = self._only_brain(rows)
        self.assertEqual(row["backend"], "brain")
        other = [r for r in rows if r["backend"] == "big"][0]
        self.assertNotEqual(other["role_key"], "brain")
        # Up, healthy, holding weights — and still not what answers a turn.
        self.assertEqual(other["state"], "resident")

    def test_two_backends_on_one_engine_stay_two_rows(self):
        """The documented brain + fast pair: both Ollama, one endpoint.

        Every backend on one engine is served the SAME /api/tags list, so the
        second backend's ids matched the row the first had just been given and
        `_match_backend_row` handed it over — leaving ONE row carrying the first
        backend's id and model with the second's label and tag, and no row left
        for the brain to be stamped on. "Ava's brain shows empty" again, reached
        without a hostname or a missing GPU being involved at all. A row that
        already belongs to a backend is no longer claimable.
        """
        one = "http://127.0.0.1:11434/v1"
        rows = self._rows(
            [_backend(bid="brain", model="llama3.1:70b", label="Llama 70B"),
             _backend(bid="fast", model="llama3.1:8b", label="Llama 8B")],
            _brain("brain"),
            _by_url({one: ["llama3.1:70b", "llama3.1:8b"]}),
            _resident_by_url({one: []}))
        self.assertEqual(len(rows), 2, f"one engine, two backends, two rows: {rows}")
        brain = self._only_brain(rows)
        self.assertEqual(brain["backend"], "brain")
        # Each row keeps its OWN identity — the mix-up produced a row whose
        # model_id came from one backend and whose label came from the other.
        self.assertEqual(brain["model"], "Llama 70B")
        self.assertEqual(brain["model_id"], "llama3.1:70b")
        fast = [r for r in rows if r["backend"] == "fast"][0]
        self.assertEqual(fast["model"], "Llama 8B")
        self.assertEqual(fast["model_id"], "llama3.1:8b")

    def test_an_offline_brain_is_still_the_brain(self):
        """Identity is config, liveness is state: the row the owner needs most
        is the one whose engine is down, so it must not lose its name for it."""
        rows = self._rows([_backend(),
                           _backend(bid="big", url="http://127.0.0.1:8000/v1",
                                    engine="vllm", model="acme/Cool-LLM-7B-FP8",
                                    label="Cool LLM")],
                          _brain("brain"),
                          _by_url({"http://127.0.0.1:8000/v1": ["acme/Cool-LLM-7B-FP8"]}),
                          _resident_by_url({"http://127.0.0.1:8000/v1": None}))
        row = self._only_brain(rows, " even while its engine is down")
        self.assertEqual(row["backend"], "brain")
        self.assertEqual(row["state"], "offline")
        self.assertEqual(row["model"], "Llama 3.1 8B")  # named from config
        # And it still sorts first: an unreachable brain is the headline, not
        # a footnote under the engine that happens to be up.
        self.assertEqual(rows[0]["backend"], "brain")

    # I7 — the probe is given the credentials the backend declares.
    def test_the_backends_api_key_reaches_the_probes(self):
        """Without it a token-protected local engine answers 401, `probe_serving`
        reads unreachable, and a perfectly healthy brain is reported offline."""
        token = "sk-local-abc"
        seen = []

        def serving(url, engine="", key="", timeout=0):
            seen.append(("serving", url, engine, key))
            if key != token:      # exactly what a guarded engine does: 401
                return (False, [])
            return (True, list(OLLAMA_TAGS))

        def resident(url, engine="", key="", timeout=0):
            seen.append(("resident", url, engine, key))
            return [] if key == token else None

        rows = self._rows([_backend(key=token)], _brain(), serving, resident)
        row = self._only_brain(rows)
        self.assertEqual([c[3] for c in seen], [token, token],
                         f"both probes must be given the backend's key: {seen}")
        self.assertEqual([c[1] for c in seen],
                         ["http://127.0.0.1:11434/v1"] * 2)
        self.assertEqual([c[2] for c in seen], ["ollama"] * 2)
        self.assertEqual(row["state"], "idle", "authenticated, so NOT offline")


class gpusvcComponentFallback(unittest.TestCase):
    """The configured-gpusvc-stack fallback must never claim residency it didn't
    observe: readable-but-empty maps = "not in memory"; unreadable = unknown."""

    def setUp(self):
        self.row = {"id": "pid:200", "name": "the GPU service", "model": "the GPU service", "model_id": None,
                    "memory_mb": 170.0, "memory_gb": 0.17, "gpu_util": None, "pid": 200,
                    "status": "loaded", "source": "nvidia-smi", "cmd": ""}
        for p in (
            mock.patch.object(hardware, "_read_mapped_model_components", lambda pid: []),
            mock.patch.object(hardware, "_read_open_model_components", lambda pid: []),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_observed_empty_scan_means_not_in_memory(self):
        with mock.patch.object(hardware, "_proc_maps_readable", lambda pid: True):
            item = hardware._attach_components([dict(self.row)])[0]
        self.assertTrue(item["components"])
        self.assertTrue(all(c["in_memory"] is False for c in item["components"]))
        self.assertEqual(item["component_count"], 0)

    def test_unobservable_process_means_unknown_not_loaded_claim(self):
        with mock.patch.object(hardware, "_proc_maps_readable", lambda pid: False):
            item = hardware._attach_components([dict(self.row)])[0]
        self.assertTrue(all(c["in_memory"] is None for c in item["components"]))
        self.assertEqual(item["component_count"], 0)

    def test_actually_mapped_files_still_report_resident(self):
        mapped = [{"name": "gpu_model_base", "kind": "checkpoints",
                   "kind_label": "Base checkpoint", "path": "/m/gpu_model_base",
                   "in_memory": True}]
        with mock.patch.object(hardware, "_read_mapped_model_components", lambda pid: mapped):
            item = hardware._attach_components([dict(self.row)])[0]
        self.assertEqual(item["component_count"], 1)
        self.assertTrue(item["components"][0]["in_memory"])


class HonestNaming(unittest.TestCase):
    def test_placeholders_stay_generic_per_runtime(self):
        self.assertEqual(hardware._short_model_name(None, runtime="vllm serve"), "vLLM model")
        self.assertEqual(hardware._short_model_name("VLLM::EngineCore"), "vLLM model")
        self.assertEqual(hardware._short_model_name("python3", runtime="/x/the GPU service/main.py"),
                         "the GPU service")
        self.assertEqual(hardware._short_model_name(None), "Model")

    def test_real_ids_keep_their_own_name(self):
        self.assertEqual(hardware._short_model_name("acme/Cool-LLM-7B-FP8"), "Cool-LLM-7B-FP8")
        self.assertEqual(hardware._short_model_name("gpu_model_base"),
                         "gpu_model_base")

    def test_brain_role_comes_from_backend_tag_not_name_guess(self):
        self.assertIn("brain", hardware._model_role("anything", backend_id="brain"))
        self.assertNotIn("brain", hardware._model_role("Some-Nemotron-30B-Omni"))

    def test_role_key_never_derives_a_brain_from_a_models_name(self):
        """The `role_key` twin of the rule above (`_model_role` is now only the
        connector-declared copy; Ava's own roles travel as `role_key`).

        A big reasoning-sounding id is not evidence of anything: which row is
        the brain is `models.effective_brain()`'s answer and nothing else, so
        `_role_key` must decline to guess it — see BrainVisibility.
        """
        for name in ("Some-Nemotron-30B-Omni", "brain-model-v2", "llama3.1:70b",
                     "acme/Cool-LLM-7B-FP8", "Ava-Brain", None):
            self.assertNotEqual(hardware._role_key(name), "brain", name)
        # The keys it MAY read from a name are the ones that are about what the
        # weights are for, not about which turn they answer.
        self.assertEqual(hardware._role_key("the GPU service"), "render")
        self.assertEqual(hardware._role_key("flux1-dev"), "image")


if __name__ == "__main__":
    unittest.main()
