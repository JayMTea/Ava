"""Model allocation: declarations, capacity, admission, and residency honesty.

Guards the properties that make this layer safe to ship to a fork on unknown
hardware:

  * an absent `alloc:` block governs nothing, so an install that never opts in
    behaves exactly as it did before the layer existed;
  * unreadable memory means UNKNOWN, which means never gate — never zero;
  * a driver whose tooling is missing degrades to observe-only rather than erroring;
  * `resident=None` (unknown) is never softened to False, because memory we cannot
    see is memory the planner must not promise to free;
  * a service that is "active" with its port open but its model absent reports
    NOT ready — the failure that ran for six days undetected on the dev box;
  * no GPU container is launched outside the allocator, because one started with
    an unbounded restart policy retries a start the allocator has declined — the
    **7,997 attempts** that motivated this whole layer.

No GPU, no network, no subprocess: the pool is passed in as an argument and every
external probe is patched at its module seam.

Run: .venv/bin/python -m pytest tests/test_alloc.py -q
"""
import os
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from gitfiles import tracked_paths as _tracked

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-alloc-test-")

from ava_bridge import hwinfo, settings
from ava_bridge.alloc import capacity, spec
from ava_bridge.alloc.base import (DriverContext, ModelDriver,
                                   ReleaseMode, Residency)
from ava_bridge.alloc.drivers import for_spec
from ava_bridge.alloc.drivers._probe import probe_ready

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The modules allowed to launch a GPU container. Launchers register a model with the
# allocator; they do not decide memory policy themselves.
GPU_RUN_ALLOWED = {"ava_bridge/alloc/drivers/docker.py"}

_GPU_RUN = re.compile(r"docker[\s\"',\]]+run\b[^\n]*--gpus")

_FIX_GPU_RUN = (
    "\n\nDeclare the model in ava.yaml `alloc.models` with `driver: docker` and let\n"
    "the allocator own its lifecycle, instead of launching a GPU container inline.\n"
    "A container started with an unbounded restart policy will retry a start the\n"
    "allocator has declined, with no backoff — that is how a restart storm happens."
)


def _cfg(patch: dict):
    """Rebind settings._CFG for one test (the repo's global-rebinding pattern:
    settings parses ava.yaml once at import, so the file is not a seam)."""
    return mock.patch.object(settings, "_CFG", patch)


def _no_ingest():
    """Silence the two auto-ingest sources so a test sees only what it declares."""
    return (mock.patch.object(spec, "_backend_profiles", lambda: {}),
            mock.patch.object(spec, "_connector_services", lambda: []))


class AbsentConfigTests(unittest.TestCase):
    """The default an unconfigured fork gets: inert, not opinionated."""

    def setUp(self):
        for p in (_cfg({}), *_no_ingest()):
            p.start()
            self.addCleanup(p.stop)

    def test_no_alloc_block_governs_nothing_of_the_operators(self):
        """The promise is about THEIR processes, and it is unchanged.

        Ava's own voice models are always declared — it loaded them, it knows where
        they are because it put them there, and an owner who wants to free them
        should not have to write config to be allowed to. Nothing of the operator's
        is governed until they say so, which is the part that ever mattered.
        """
        own = {s.id for s in spec.load_models()}
        self.assertEqual(own, {"ava-voice", "ava-voice-gpu"})
        self.assertTrue(all(s.source == "ava" for s in spec.load_models()))

    def test_an_operator_can_still_take_avas_own_models_out_of_the_registry(self):
        # An explicit declaration wins over the built-in one, like any other.
        with _cfg({"alloc": {"models": {"ava-voice": {"driver": "observe"}}}}):
            self.assertEqual(spec.by_id()["ava-voice"].driver, "observe")

    def test_enabled_defaults_true_but_harmless(self):
        # On by default is safe precisely because nothing is declared: an operator
        # opts in one model at a time rather than flipping a master switch.
        self.assertTrue(spec.enabled())

    def test_undeclared_model_is_not_governed(self):
        from ava_bridge import alloc
        ok, reason = alloc.admit("anything")
        self.assertTrue(ok)
        self.assertIn("not declared", reason)


class SpecTests(unittest.TestCase):
    def setUp(self):
        for p in _no_ingest():
            p.start()
            self.addCleanup(p.stop)

    def test_partial_declaration_gets_permissive_defaults(self):
        with _cfg({"alloc": {"models": {"m": {}}}}):
            s = spec.by_id()["m"]
        self.assertIsNone(s.weight_gb)              # unknown, not 0
        self.assertIsNone(s.need_gib)               # so nothing to gate on
        self.assertEqual(s.priority, spec.DEFAULT_PRIORITY)
        self.assertEqual(s.min_free_gb, spec.DEFAULT_MIN_FREE_GB)
        self.assertFalse(s.pinned)
        self.assertEqual(s.driver, "observe")       # nothing inferable -> floor

    def test_bad_priority_clamps_instead_of_raising(self):
        with _cfg({"alloc": {"models": {"m": {"priority": "urgent!"}}}}):
            self.assertEqual(spec.by_id()["m"].priority, spec.DEFAULT_PRIORITY)

    def test_driver_inferred_from_config_shape(self):
        cfg = {"alloc": {"models": {
            "a": {"driver_config": {"container": "c"}},
            "b": {"driver_config": {"unit": "u.service"}},
            "c": {"driver_config": {"base": "http://x"}},
        }}}
        with _cfg(cfg):
            by_id = spec.by_id()
        got = {k: v.driver for k, v in by_id.items() if not k.startswith("ava-")}
        self.assertEqual(got, {"a": "docker", "b": "systemd", "c": "http-unload"})

    def test_need_gib_is_weight_plus_headroom(self):
        with _cfg({"alloc": {"models": {"m": {"weight_gb": 20, "min_free_gb": 6}}}}):
            self.assertEqual(spec.by_id()["m"].need_gib, 26)

    def test_env_placeholders_are_expanded(self):
        # One declaration must serve bare metal and a container, where host/port
        # differ. Unexpanded, `base` would reach the driver verbatim and build a
        # nonsense URL. Uses the connector manifest expander so the syntax is the
        # same everywhere in Ava.
        cfg = {"alloc": {"models": {"m": {
            "driver_config": {"base": "${ALLOC_TEST_URL:-http://127.0.0.1:9}"}}}}}
        with _cfg(cfg):
            self.assertEqual(spec.by_id()["m"].driver_config["base"],
                             "http://127.0.0.1:9")
        with mock.patch.dict(os.environ, {"ALLOC_TEST_URL": "http://engine:80"}), \
             _cfg(cfg):
            self.assertEqual(spec.by_id()["m"].driver_config["base"],
                             "http://engine:80")

    def test_remote_host_is_local_false(self):
        with _cfg({"alloc": {"models": {"m": {"host": "other-box"}}}}):
            s = spec.by_id()["m"]
        self.assertFalse(s.local)
        self.assertEqual(s.driver, "observe")

    def test_via_model_is_never_actuated_directly(self):
        with _cfg({"alloc": {"models": {"ckpt": {"via": "engine"}}}}):
            s = spec.by_id()["ckpt"]
        self.assertEqual(s.driver, "observe")
        self.assertTrue(any("hosted via engine" in n for n in s.notes))


class InheritanceTests(unittest.TestCase):
    """Sizing is declared once. A number under `fit:` is never restated."""

    class _Prof:
        weight_gb = 24.0
        min_free_gb = 6.0
        tier = "large"
        workloads = frozenset({"chat"})
        local = True

    # An engine with no unload endpoint in the registry, so these tests stay about
    # SIZING inheritance. Lever inference has its own class below.
    #
    # `url`, NOT `base_url` — this is the shape `load_backends()` actually emits,
    # and LeverInferenceTests pins that. A fixture written in ava.yaml's spelling
    # is what let a read of the wrong key pass its own tests.
    _BACKEND = {"id": "b1", "engine": "llamacpp",
                "url": "http://127.0.0.1:8080/v1", "model": "m"}

    def test_fit_block_fills_blanks(self):
        with mock.patch.object(spec, "_backend_profiles",
                               lambda: {"b1": (self._BACKEND, self._Prof())}), \
             mock.patch.object(spec, "_connector_services", lambda: []), \
             _cfg({"alloc": {"models": {"b1": {"driver": "docker",
                                               "driver_config": {"container": "c"}}}}}):
            s = spec.by_id()["b1"]
        self.assertEqual(s.weight_gb, 24.0)
        self.assertEqual(s.min_free_gb, 6.0)
        self.assertEqual(s.tier, "large")

    def test_explicit_declaration_beats_inheritance(self):
        with mock.patch.object(spec, "_backend_profiles",
                               lambda: {"b1": (self._BACKEND, self._Prof())}), \
             mock.patch.object(spec, "_connector_services", lambda: []), \
             _cfg({"alloc": {"models": {"b1": {"weight_gb": 99}}}}):
            self.assertEqual(spec.by_id()["b1"].weight_gb, 99)

    def test_backend_alone_is_declared_but_observe_only(self):
        # Inheriting sizing must not imply permission to actuate. An engine with no
        # unload endpoint of its own stays exactly where it was: visible, untouchable.
        with mock.patch.object(spec, "_backend_profiles",
                               lambda: {"b1": (self._BACKEND, self._Prof())}), \
             mock.patch.object(spec, "_connector_services", lambda: []), \
             _cfg({}):
            s = spec.by_id()["b1"]
        self.assertTrue(s.implicit)
        self.assertEqual(s.source, "backends")
        self.assertEqual(s.driver, "observe")


class LeverInferenceTests(unittest.TestCase):
    """The one narrowing of "declared, never discovered", and its hard edges.

    A backend is configured for SERVING, not for being unloaded, so inferring a lever
    from one is a real departure — taken because an owner-facing Free button that is
    inert on every stock install is not a feature. What keeps it defensible is that
    the inference cannot reach anything the operator did not already write down: it
    fills a REVERSIBLE unload, aimed at the declared engine, at the declared URL,
    using that engine's own documented endpoint. Everything below pins one edge of
    that. `alloc.infer_levers: false` restores the old behaviour exactly.
    """

    _PROF = InheritanceTests._Prof
    _OLLAMA = {"id": "b1", "engine": "ollama",
               "url": "http://127.0.0.1:11434/v1", "model": "llama3.2:3b"}

    def test_the_fixtures_match_what_load_backends_actually_emits(self):
        """The guard that would have caught the real bug.

        `load_backends()` normalises ava.yaml's `base_url` to `url`. Every fixture
        here was hand-written in the CONFIG spelling, so `_inherit_unload` read a
        key that is always None in production and its tests passed anyway — the
        inference never fired once on a real box. Pin the shape at the source.
        """
        from ava_bridge.router_app import load_backends
        with _cfg({"inference": {"backends": {"b1": {
                "base_url": "http://127.0.0.1:11434/v1", "model": "m",
                "engine": "ollama"}}}}):
            emitted = load_backends()
        self.assertTrue(emitted, "load_backends returned nothing")
        keys = set(emitted[0])
        self.assertIn("url", keys)
        self.assertNotIn("base_url", keys,
                         "load_backends now emits base_url — _inherit_unload and "
                         "these fixtures both assume it does not")
        # And the fixtures below speak that shape.
        for fx in (self._OLLAMA, InheritanceTests._BACKEND):
            self.assertIn("url", fx)
            self.assertNotIn("base_url", fx)

    def _spec(self, backend, cfg=None):
        with mock.patch.object(spec, "_backend_profiles",
                               lambda: {"b1": (backend, self._PROF())}), \
             mock.patch.object(spec, "_connector_services", lambda: []), \
             _cfg(cfg or {}):
            return spec.by_id()["b1"]

    def test_an_engine_with_an_unload_endpoint_gets_a_reversible_lever(self):
        s = self._spec(self._OLLAMA)
        self.assertEqual(s.driver, "http-unload")
        self.assertEqual(s.driver_config["json"]["model"], "llama3.2:3b")

    def test_the_native_control_path_hangs_off_the_root_not_the_v1_base(self):
        """`…/v1/api/generate` is a 404 — the bug `health_at_root` exists to stop."""
        s = self._spec(self._OLLAMA)
        self.assertEqual(s.driver_config["base"], "http://127.0.0.1:11434")

    def test_it_never_infers_a_lever_that_stops_a_process(self):
        """The whole safety argument. A guessed unload hits the engine Ava already
        talks to; a guessed container name can stop something that was never Ava's."""
        for backend in (self._OLLAMA, InheritanceTests._BACKEND):
            s = self._spec(backend)
            self.assertNotIn(s.driver, ("docker", "systemd"),
                             f"inferred a stop lever for {backend['engine']}")

    def test_a_verified_lever_says_nothing_alarming(self):
        # Ollama's was exercised against a live instance (see engines.py), so it
        # must NOT carry the warning — a caveat on everything is a caveat on
        # nothing.
        s = self._spec(self._OLLAMA)
        self.assertTrue(s.unload_verified)
        self.assertFalse(any("NOT VERIFIED" in n for n in s.notes))

    def test_an_unverified_lever_is_offered_but_flagged(self):
        """The mechanism, driven by a synthetic engine rather than a real one.

        Pinned against a stand-in so it keeps testing the RULE after every shipped
        engine has been verified — otherwise this quietly stops asserting anything
        the day the last `unload_verified=False` is flipped.
        """
        from ava_bridge import engines
        fake = engines.Engine(
            key="madeup", label="Made Up", tier=engines.GENERIC,
            health_path="/models", usage=False, usage_reason="n/a",
            unload_path="/free", unload_body={"model": "{model}"},
            unload_verified=False, unload_reason="never exercised")
        with mock.patch.object(engines, "get",
                               lambda k: fake if k == "madeup" else None):
            s = self._spec({**self._OLLAMA, "engine": "madeup"})
        self.assertEqual(s.driver, "http-unload")
        self.assertFalse(s.unload_verified)
        self.assertTrue(any("NOT VERIFIED" in n for n in s.notes),
                        "an unexercised endpoint must say so, not imply it works")

    def test_an_explicit_declaration_is_never_overwritten(self):
        s = self._spec(self._OLLAMA, {"alloc": {"models": {"b1": {
            "driver": "docker", "driver_config": {"container": "mine"}}}}})
        self.assertEqual(s.driver, "docker")
        self.assertEqual(s.driver_config, {"container": "mine"})

    def test_a_remote_backend_is_never_given_a_lever(self):
        s = self._spec({**self._OLLAMA, "id": "b1"},
                       {"alloc": {"models": {"b1": {"host": "gpu-box"}}}})
        self.assertNotEqual(s.driver, "http-unload")

    def test_an_undeclared_engine_is_not_guessed_from_the_url(self):
        """:8080 is llama.cpp or MLX depending on the box, and freeing the wrong
        engine's weights is a real thing to be wrong about."""
        s = self._spec({"id": "b1", "engine": "",
                        "base_url": "http://127.0.0.1:11434/v1", "model": "m"})
        self.assertEqual(s.driver, "observe")

    def test_the_switch_restores_the_previous_behaviour_exactly(self):
        s = self._spec(self._OLLAMA, {"alloc": {"infer_levers": False}})
        self.assertEqual(s.driver, "observe")


class AdmissionTests(unittest.TestCase):
    """The consumer model_fit.fits_now(assume_loaded=False) never had."""

    def setUp(self):
        for p in _no_ingest():
            p.start()
            self.addCleanup(p.stop)

    def _admit(self, free, **model):
        from ava_bridge import alloc
        with _cfg({"alloc": {"models": {"m": model}}}):
            return alloc.admit("m", free_gib=free)

    def test_cold_load_requires_room_for_weights_not_just_headroom(self):
        # 30 free is plenty to SERVE a 24 GiB model, but not to LOAD it.
        ok, _ = self._admit(30.0, weight_gb=24, min_free_gb=10)
        self.assertFalse(ok)
        ok, _ = self._admit(40.0, weight_gb=24, min_free_gb=10)
        self.assertTrue(ok)

    def test_unknown_memory_never_gates(self):
        ok, reason = self._admit(None, weight_gb=999)
        self.assertTrue(ok)
        self.assertIn("unknown", reason)

    def test_unknown_weight_never_gates(self):
        ok, reason = self._admit(1.0)
        self.assertTrue(ok)
        self.assertIn("unknown", reason)

    def test_remote_model_is_not_memory_gated(self):
        ok, reason = self._admit(0.5, weight_gb=999, host="other")
        self.assertTrue(ok)
        self.assertIn("host", reason)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        # Redirect the learned-baseline file, not just AVA_HOME: `settings` freezes
        # AVA_HOME at first import, so in a shared pytest process the env var alone is a
        # silent no-op and these samples would be written into the real data dir.
        self._dir = tempfile.mkdtemp(prefix="cap-baseline-")
        p = mock.patch.object(capacity, "_baseline_path",
                              lambda: os.path.join(self._dir, "baseline.json"))
        p.start()
        self.addCleanup(p.stop)
        capacity.reset_baseline()
        self.addCleanup(capacity.reset_baseline)

    def _mem(self, free, total, src="system-psutil"):
        return mock.patch.object(hwinfo, "fit_memory",
                                 return_value=hwinfo.MemInfo(free, total, src))

    def test_unreadable_pool_disables_gating_not_the_layer(self):
        with mock.patch.object(hwinfo, "fit_memory", return_value=hwinfo.MemInfo()):
            p = capacity.snapshot()
        self.assertFalse(p.readable)
        self.assertIsNone(p.free_gib)
        self.assertIsNone(p.unknown_gib)   # cannot compute a residual either

    def test_unknown_is_a_residual_of_what_declared_models_do_not_explain(self):
        holders = [capacity.Holder(id="a", gib=10.0, measured=True, declared=True)]
        with self._mem(20.0, 100.0):
            p = capacity.snapshot(holders)
        # 80 held, 10 explained by a declared model, baseline unlearned yet.
        self.assertEqual(p.declared_gib, 10.0)
        self.assertEqual(p.unknown_gib, 70.0)

    def test_baseline_ignores_samples_with_unmeasured_residency(self):
        # An estimated residency would leak the model's weight into the floor, and
        # since the floor is a running minimum the error would never wash out.
        holders = [capacity.Holder(id="a", gib=10.0, measured=False, declared=True)]
        with self._mem(20.0, 100.0):
            self.assertIsNone(capacity.baseline_gib(None, holders))

    def test_baseline_learns_from_measured_samples(self):
        holders = [capacity.Holder(id="a", gib=10.0, measured=True, declared=True)]
        with self._mem(70.0, 100.0):
            got = capacity.baseline_gib(None, holders)
        self.assertEqual(got, 20.0)          # 30 held - 10 declared

    def test_baseline_rejects_implausible_samples(self):
        holders = []
        with self._mem(1.0, 100.0):          # 99 held, all unattributed
            self.assertIsNone(capacity.baseline_gib(None, holders))

    def test_configured_baseline_wins_over_learning(self):
        with _cfg({"alloc": {"pool": {"baseline": 12.5}}}), self._mem(70.0, 100.0):
            self.assertEqual(capacity.baseline_gib(), 12.5)

    def test_wait_free_returns_true_when_memory_is_unreadable(self):
        # Nothing to wait on; blocking would stall a caller for no benefit.
        with mock.patch.object(hwinfo, "fit_memory", return_value=hwinfo.MemInfo()):
            self.assertTrue(capacity.wait_free(999.0, timeout=0.1))

    def test_wait_free_times_out_without_blocking_forever(self):
        with self._mem(1.0, 100.0):
            self.assertFalse(capacity.wait_free(50.0, timeout=0.3, poll=0.1))


class ReadinessTests(unittest.TestCase):
    """Port-open must never read as ready. This is the six-day-outage regression."""

    def _probe(self, body: bytes, status: int = 200):
        class _Resp:
            status = None
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *_a): return body
        r = _Resp()
        r.status = status
        return mock.patch("urllib.request.urlopen", return_value=r)

    def test_listening_but_model_absent_is_not_ready(self):
        # The exact shape the voice sidecar served for six days while systemd
        # reported the unit active.
        with self._probe(b'{"ok": false, "stt_ready": false}'):
            got = probe_ready({"url": "http://x/health", "require": ["ok", "stt_ready"]})
        self.assertIs(got, False)

    def test_all_required_keys_true_is_ready(self):
        with self._probe(b'{"ok": true, "stt_ready": true}'):
            got = probe_ready({"url": "http://x/health", "require": ["ok", "stt_ready"]})
        self.assertIs(got, True)

    def test_one_false_key_is_enough_to_fail(self):
        with self._probe(b'{"ok": true, "stt_ready": false}'):
            got = probe_ready({"url": "http://x/health", "require": ["ok", "stt_ready"]})
        self.assertIs(got, False)

    def test_unreachable_is_unknown_not_false(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            self.assertIsNone(probe_ready({"url": "http://x/health"}))

    def test_no_probe_declared_is_unknown(self):
        self.assertIsNone(probe_ready({}))
        self.assertIsNone(probe_ready(None))

    def test_models_contains_checks_the_served_id(self):
        body = b'{"data": [{"id": "org/model-a"}]}'
        with self._probe(body):
            self.assertIs(probe_ready({"url": "http://x", "expect": "models_contains",
                                       "model": "org/model-a"}), True)
        with self._probe(body):
            self.assertIs(probe_ready({"url": "http://x", "expect": "models_contains",
                                       "model": "org/other"}), False)


class PortabilityTests(unittest.TestCase):
    """A second user's hardware differs from the dev box — verify it adapts."""

    def _ctx(self, **cfg):
        return DriverContext(model_id="m", weight_gb=8.0, config=cfg,
                             free_gib=lambda: 50.0,
                             wait_free=lambda *a, **k: True)

    def test_missing_container_runtime_degrades_to_observe(self):
        with mock.patch("shutil.which", return_value=None):
            drv = for_spec("docker", self._ctx(container="c"))
        self.assertTrue(drv.observe_only)
        self.assertIn("docker", drv.unusable_reason())

    def test_missing_service_manager_degrades_to_observe(self):
        with mock.patch("shutil.which", return_value=None):
            drv = for_spec("systemd", self._ctx(unit="u.service"))
        self.assertTrue(drv.observe_only)

    def test_unknown_driver_name_degrades_to_observe(self):
        drv = for_spec("some-engine-we-never-heard-of", self._ctx())
        self.assertTrue(drv.observe_only)
        self.assertIn("unknown driver", drv.unusable_reason())

    def test_observe_floor_never_reports_success_on_release(self):
        # A driver that claimed success would let the planner count memory it never
        # received and start a model into a pool that was never freed.
        drv = for_spec("observe", self._ctx())
        res = drv.release(ReleaseMode.STOP)
        self.assertFalse(res.ok)
        self.assertIsNone(res.freed_gib)

    def test_observe_floor_reports_unknown_residency_not_absent(self):
        r = for_spec("observe", self._ctx()).residency()
        self.assertIsNone(r.resident)
        self.assertFalse(r.known)
        self.assertFalse(r.measured)

    def test_driver_missing_required_config_degrades(self):
        with mock.patch("shutil.which", return_value="/usr/bin/docker"):
            drv = for_spec("docker", self._ctx())      # no container declared
        self.assertTrue(drv.observe_only)

    def test_every_registered_driver_declares_a_name_and_modes(self):
        from ava_bridge.alloc.drivers import registry
        for key, cls in registry().items():
            self.assertTrue(getattr(cls, "name", ""), f"{key} has no name")
            self.assertIsInstance(cls.RELEASE_MODES, tuple)

    def test_self_restoring_driver_is_not_scolded_for_no_acquire(self):
        # An engine that reloads on next use SHOULD inherit the no-op acquire().
        drv = for_spec("http-unload", self._ctx(base="http://x", rss_unit="u.service"))
        self.assertTrue(drv.SELF_RESTORING)
        self.assertFalse([p for p in drv.validate() if "acquire()" in p])

    def test_a_reversible_driver_without_acquire_is_flagged(self):
        class _Bad(ModelDriver):
            name = "bad"
            RELEASE_MODES = (ReleaseMode.STOP,)
            def residency(self):
                return Residency(resident=True, gib=1.0, measured=True)
            def release(self, mode, **kw):
                raise AssertionError("not called")
        problems = _Bad(self._ctx()).validate()
        self.assertTrue(any("acquire()" in p for p in problems))


class LifecycleConventionTests(unittest.TestCase):
    """A static scan, in the style of tests/test_feature_convention.py.

    Memory coordination used to be expressed at each *caller* that needed it. That
    works until someone adds a caller. The new one starts a container without the
    hold, no test notices (every existing caller still holds it), and nothing
    detects the gap at runtime — the engine simply starts losing its memory to an
    uncoordinated process. Putting the launch behind one driver makes bypassing it
    a build failure rather than a discovery.
    """

    def test_gpu_containers_are_not_launched_inline(self):
        offenders = []
        for path in _tracked("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in GPU_RUN_ALLOWED or rel.startswith(("tests/", "qa/")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue  # a comment describing the rule is not a breach of it
                if _GPU_RUN.search(line):
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
        self.assertFalse(
            offenders,
            "these launch a GPU container outside the allocator:\n  "
            + "\n  ".join(offenders) + _FIX_GPU_RUN)


if __name__ == "__main__":
    unittest.main()
