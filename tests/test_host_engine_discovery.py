"""An engine on the HOST machine is discoverable, and says so.

Reported from the same Windows laptop as tests/test_fit_pool_honesty.py. The
wizard's own fallback line tells a user with no engine to "install Ollama
(ollama.com)". On Windows that installs a NATIVE Ollama — which gets the real
GPU and the machine's full memory, neither of which the bridge container has.
Ava then could not find it: `_candidates()` probed the compose network and the
CONTAINER's own loopback, and nothing else. So the most likely path a Windows
tester takes ended at "nothing answering yet" on a machine where something was
answering, with better hardware than Ava was reporting.

Two halves, and the second is the one that is easy to forget:

  * find it — probe the runtime's host gateway, derived from the engine registry
    so a newly registered engine is discoverable and not merely recognisable.
  * do not then LIE about it — a host engine draws on the host's memory and the
    host's GPU, so the tier `/api/setup/hardware` measured from inside the
    container describes a different machine and must not be published as advice.

Run: .venv/bin/python -m pytest tests/test_host_engine_discovery.py -q
"""
import unittest
from unittest import mock

from ava_bridge import engines, setup_wizard


class RegistryPorts(unittest.TestCase):
    """The probe has to guess a port before anything is configured, so the port
    is registry data — not a ladder written twice in setup_wizard."""

    def test_every_local_engine_declares_a_port(self):
        missing = [e.key for e in engines.ENGINES if e.local and not e.default_port]
        self.assertEqual(missing, [], "a local engine with no port cannot be found")

    def test_a_cloud_engine_declares_none(self):
        self.assertIsNone(engines.get("openai").default_port)

    def test_a_shared_port_resolves_to_the_servable_engine(self):
        """llama.cpp and MLX both default to 8080. Resolving it to MLX would
        health-check the wrong path AND be refused by engine_servable_here
        anywhere but Apple Silicon."""
        self.assertEqual(engines.default_ports()[8080], "llamacpp")

    def test_engine_of_reads_the_registry(self):
        for url, want in (("http://127.0.0.1:11434/v1", "ollama"),
                          ("http://127.0.0.1:8002/v1", "vllm"),
                          ("http://127.0.0.1:1234/v1", "lmstudio"),
                          ("http://127.0.0.1:8080/v1", "llamacpp")):
            self.assertEqual(setup_wizard._engine_of(url), want, url)

    def test_a_cloud_url_is_still_not_an_unlabelled_vllm(self):
        """The port lookup must not shadow the remote-host fallback: calling
        api.openai.com "vllm" gets a perfectly good cloud backend refused for
        needing a GPU."""
        self.assertEqual(setup_wizard._engine_of("https://api.openai.com/v1"), "openai")


class HealthPathsAreTheRegistrys(unittest.TestCase):
    """`_HEALTH_PATH` was built from the registry and then never read, so
    llama.cpp declared /health and was probed at /v1/models anyway."""

    def test_a_native_path_hangs_off_the_root(self):
        self.assertEqual(setup_wizard._health_url("http://ollama:11434/v1", "ollama"),
                         "http://ollama:11434/api/tags")
        self.assertEqual(setup_wizard._health_url("http://127.0.0.1:8080/v1", "llamacpp"),
                         "http://127.0.0.1:8080/health")

    def test_the_openai_path_hangs_off_the_v1_base(self):
        self.assertEqual(setup_wizard._health_url("http://vllm:8002/v1", "vllm"),
                         "http://vllm:8002/v1/models")


class HostGatewayDiscovery(unittest.TestCase):
    def test_candidates_reach_the_host_when_a_gateway_resolves(self):
        with mock.patch.object(setup_wizard, "host_gateway",
                               lambda: "host.docker.internal"):
            bases = [c["base_url"] for c in setup_wizard._candidates()]
        self.assertIn("http://host.docker.internal:11434/v1", bases)
        self.assertIn("http://host.docker.internal:8002/v1", bases)

    def test_no_gateway_costs_no_candidates(self):
        """Bare metal must not pay a DNS timeout per probe for a path that
        cannot apply to it."""
        with mock.patch.object(setup_wizard, "host_gateway", lambda: None):
            bases = [c["base_url"] for c in setup_wizard._candidates()]
        self.assertFalse([b for b in bases if "host." in b])

    def test_the_compose_engine_still_outranks_the_host_one(self):
        """An engine Ava started is a better answer than one it merely found —
        the wizard preselects backends[0]."""
        with mock.patch.object(setup_wizard, "host_gateway",
                               lambda: "host.docker.internal"):
            bases = [c["base_url"] for c in setup_wizard._candidates()]
        self.assertLess(bases.index("http://ollama:11434/v1"),
                        bases.index("http://host.docker.internal:11434/v1"))

    def test_candidates_stay_deduplicated(self):
        with mock.patch.object(setup_wizard, "host_gateway",
                               lambda: "host.docker.internal"):
            bases = [c["base_url"] for c in setup_wizard._candidates()]
        self.assertEqual(len(bases), len(set(bases)))

    def test_gateway_resolution_is_asked_once_not_per_probe(self):
        calls = []

        def _fake(name, *a, **k):
            calls.append(name)
            if name == "host.docker.internal":
                return [(2, 1, 6, "", ("192.168.65.2", 0))]
            raise OSError("no such host")

        with mock.patch("socket.getaddrinfo", _fake):
            self.assertEqual(setup_wizard.host_gateway(), "host.docker.internal")
        self.assertEqual(calls, ["host.docker.internal"],
                         "resolution short-circuits on the first name that answers")


class Locality(unittest.TestCase):
    """WHERE an engine is, which is what decides whether the fit reading applies."""

    def test_a_host_gateway_is_the_host(self):
        self.assertEqual(
            setup_wizard._locality("http://host.docker.internal:11434/v1"), "host")
        self.assertEqual(
            setup_wizard._locality("http://host.containers.internal:11434/v1"), "host")

    def test_a_compose_service_name_is_the_compose_network(self):
        self.assertEqual(setup_wizard._locality("http://ollama:11434/v1"), "compose")

    def test_loopback_inside_a_container_is_the_container(self):
        """THE distinction. Inside a container 127.0.0.1 is the container's own
        loopback — a different machine from the user's."""
        with mock.patch("ava_bridge.auth.in_container", lambda: True):
            self.assertEqual(setup_wizard._locality("http://127.0.0.1:11434/v1"),
                             "container")

    def test_loopback_on_bare_metal_is_the_machine(self):
        with mock.patch("ava_bridge.auth.in_container", lambda: False):
            self.assertEqual(setup_wizard._locality("http://127.0.0.1:11434/v1"),
                             "host")

    def test_a_dotted_remote_is_remote(self):
        self.assertEqual(setup_wizard._locality("https://api.openai.com/v1"), "remote")


class BlindProbesAreStrict(unittest.TestCase):
    """A port Ava guessed at is not a port the owner named."""

    class _Resp:
        def __init__(self, code, ctype="application/json"):
            self.status_code, self.headers = code, {"content-type": ctype}

    def _probe(self, resp, **kw):
        with mock.patch.dict("sys.modules"):
            fake = mock.MagicMock()
            fake.get.return_value = resp
            with mock.patch.dict("sys.modules", {"requests": fake}):
                return setup_wizard._probe("http://x/health", **kw)

    def test_a_404_from_an_unrelated_server_is_not_an_engine(self):
        """:8080 is a very common port. Under the lenient rule any dev server
        answering 404 there was reported to the user as llama.cpp."""
        self.assertFalse(self._probe(self._Resp(404, "text/html"), strict=True))

    def test_an_html_200_is_not_an_engine_either(self):
        self.assertFalse(self._probe(self._Resp(200, "text/html"), strict=True))

    def test_a_json_200_is(self):
        self.assertTrue(self._probe(self._Resp(200), strict=True))

    def test_a_configured_backend_asking_for_a_key_is_still_up(self):
        """401 means "there, and wants credentials", not "gone" — so the owner's
        own backend keeps the lenient rule."""
        self.assertTrue(self._probe(self._Resp(401), strict=False))

    def test_owner_named_candidates_are_not_probed_blind(self):
        import os
        with mock.patch.dict(os.environ, {"AVA_BACKEND_URL": "http://named:9000/v1"}):
            env = [c for c in setup_wizard._candidates() if c["id"] == "env"]
        self.assertTrue(env)
        self.assertFalse(env[0]["blind"])

    def test_swept_candidates_are(self):
        swept = [c for c in setup_wizard._candidates() if c["id"] == "ollama-service"]
        self.assertTrue(swept and swept[0]["blind"])


class WhyTheHostCannotBeReached(unittest.TestCase):
    """Refused and dropped are different failures with different fixes.

    Found by running the real thing rather than a mock: a container on the
    maintainer's box, with a live engine on the host and `host.docker.internal`
    resolving correctly, still could not reach it - the box filters
    container->host traffic. The advice the wizard would have given was "set
    OLLAMA_HOST=0.0.0.0", which is useless against a firewall. Windows Defender
    produces exactly this shape on the WSL vEthernet adapter.
    """

    def _sock(self, exc):
        sock = mock.MagicMock()
        sock.connect.side_effect = exc
        return mock.patch("socket.socket", lambda *a, **k: sock)

    def test_a_reset_means_the_engine_is_bound_to_loopback(self):
        with self._sock(ConnectionRefusedError()):
            self.assertEqual(setup_wizard.host_reachability("host.docker.internal"),
                             "refused")

    def test_a_timeout_means_something_is_filtering(self):
        import socket as _s
        with self._sock(_s.timeout()):
            self.assertEqual(setup_wizard.host_reachability("host.docker.internal"),
                             "dropped")

    def test_no_route_is_also_filtered_not_refused(self):
        with self._sock(OSError(113, "No route to host")):
            self.assertEqual(setup_wizard.host_reachability("host.docker.internal"),
                             "dropped")

    def test_a_reachable_listener_is_not_a_failure_at_all(self):
        sock = mock.MagicMock()
        sock.connect.return_value = None
        with mock.patch("socket.socket", lambda *a, **k: sock):
            self.assertEqual(setup_wizard.host_reachability("host.docker.internal"), "")

    def test_the_question_is_only_asked_when_it_is_the_question(self):
        """One connect per engine port is cheap, but not free - and meaningless
        when an engine already answered."""
        called = []
        with mock.patch.object(setup_wizard, "_probe", lambda *a, **k: True), \
             mock.patch.object(setup_wizard, "host_gateway",
                               lambda: "host.docker.internal"), \
             mock.patch.object(setup_wizard, "_in_container", lambda: True), \
             mock.patch.object(setup_wizard, "host_reachability",
                               lambda *a, **k: called.append(1) or "dropped"):
            out = setup_wizard.api_backends()
        self.assertTrue(out["any_up"])
        self.assertEqual(out["host_reach"], "")
        self.assertEqual(called, [], "reachability was probed despite an engine answering")

    def test_and_is_asked_when_nothing_answered(self):
        with mock.patch.object(setup_wizard, "_probe", lambda *a, **k: False), \
             mock.patch.object(setup_wizard, "host_gateway",
                               lambda: "host.docker.internal"), \
             mock.patch.object(setup_wizard, "_in_container", lambda: True), \
             mock.patch.object(setup_wizard, "host_reachability",
                               lambda *a, **k: "dropped"):
            out = setup_wizard.api_backends()
        self.assertEqual(out["host_reach"], "dropped")


class TheRouteReportsWhatTheUiNeeds(unittest.TestCase):
    def test_backends_carry_locality_and_the_container_facts(self):
        with mock.patch.object(setup_wizard, "_probe", lambda *a, **k: False), \
             mock.patch.object(setup_wizard, "host_gateway",
                               lambda: "host.docker.internal"), \
             mock.patch.object(setup_wizard, "_in_container", lambda: True):
            out = setup_wizard.api_backends()
        self.assertTrue(out["in_container"])
        self.assertEqual(out["host_gateway"], "host.docker.internal")
        self.assertTrue(all("locality" in b for b in out["backends"]))

    def test_probe_order_survives_concurrency(self):
        """Probed in parallel for latency; `backends[0]` is still the most
        authoritative candidate, because the wizard preselects it."""
        seen = {}

        def _probe(url, timeout=1.5, strict=False):
            # Answer out of order: the last candidate resolves "first".
            seen[url] = len(seen)
            return url.startswith("http://ollama:")

        with mock.patch.object(setup_wizard, "_probe", _probe), \
             mock.patch.object(setup_wizard, "host_gateway", lambda: None):
            out = setup_wizard.api_backends()
        ids = [b["id"] for b in out["backends"]]
        self.assertEqual(ids[0], "vllm-service")
        self.assertTrue(out["any_up"])
        up = [b for b in out["backends"] if b["up"]]
        self.assertEqual([b["id"] for b in up], ["ollama-service"])


if __name__ == "__main__":
    unittest.main()
