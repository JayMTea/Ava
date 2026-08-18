"""Step 2 of the wizard must READ what the install declared, not infer it.

The report: a fresh Docker install on Windows, whose own deploy/.env carried
``AVA_BACKEND_URL=http://ollama:11434/v1``, reached "Set up Ava" step 2 and was
shown — and offered to save — ``http://127.0.0.1:11434/v1``. Inside the ava
container that is the CONTAINER's own loopback; it reaches no sibling service on
any profile. The maintainer, who wrote the installer, could not tell from the
form which engine to pick.

The backend was not at fault. ``setup_wizard.api_backends`` ships the declared
backend as a first-class ``blind:false`` candidate with ``note:"from
AVA_BACKEND_URL"``. The page received it and threw it away, because ``chosen``
was ``live[0]||null`` — so with nothing answering there was no prefill, and the
static markup stood. That made IDENTITY a function of LIVENESS, which is exactly
the split ``models.effective_brain`` is written to defend: what is configured
"says nothing about whether the thing is up, which is a separate, observed
question". Accepting the form then wrote ``inference.backends.local``, and
``router_app.load_backends`` stops building the env backend once that exists —
so a wrong answer shown for one second shadowed the installer permanently.

Two decisions carry that, and both are pure, so this evaluates them in node
against the payload shapes ``api_backends`` really emits — no browser, no bridge.
They are bounded by ``WIZARD-CHOICE-BEGIN/END`` in the page; the markers are the
contract, the same way tests/test_install_detection.py bounds its region.

Run: .venv/bin/python -m pytest tests/test_setup_wizard_prefill.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "ava_bridge" / "web" / "setup_wizard.html"
BEGIN, END = "// WIZARD-CHOICE-BEGIN", "// WIZARD-CHOICE-END"

# The shapes setup_wizard.api_backends really emits, in its real order:
# configured first, then the environment (both blind:false), then the compose
# service names and loopback probes it appends for a box that declared neither.
ENV_OLLAMA = {"id": "env", "base_url": "http://ollama:11434/v1", "engine": "ollama",
              "engine_label": "Ollama", "note": "from AVA_BACKEND_URL",
              "locality": "compose", "blind": False, "up": False, "model": ""}
ENV_VLLM = {"id": "env", "base_url": "http://vllm:8002/v1", "engine": "vllm",
            "engine_label": "vLLM", "note": "from AVA_BACKEND_URL",
            "locality": "compose", "blind": False, "up": False, "model": ""}
GUESS_VLLM = {"id": "vllm-service", "base_url": "http://vllm:8002/v1", "engine": "vllm",
              "engine_label": "vLLM", "note": "compose service",
              "locality": "compose", "blind": True, "up": False, "model": ""}
GUESS_OLLAMA = {"id": "ollama-service", "base_url": "http://ollama:11434/v1",
                "engine": "ollama", "engine_label": "Ollama", "note": "compose service",
                "locality": "compose", "blind": True, "up": False, "model": ""}
HOST_LIVE = {"id": "ollama-host", "base_url": "http://172.17.0.1:11434/v1",
             "engine": "ollama", "engine_label": "Ollama", "note": "this machine",
             "locality": "host", "blind": True, "up": True, "model": "llama3.2"}


def _decisions() -> str:
    text = PAGE.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise AssertionError(
            f"{BEGIN}/{END} are gone from {PAGE.relative_to(ROOT)}. They bound the two "
            "pure functions this file tests. If the decision moved, move the markers "
            "with it — do not delete this test.")
    return text.split(BEGIN, 1)[1].split(END, 1)[0]


@unittest.skipUnless(shutil.which("node"), "needs node to evaluate the page's JS")
class WizardPrefillTests(unittest.TestCase):

    def _run(self, backends: list[dict], **top) -> dict:
        payload = {"backends": backends, "in_container": True,
                   "host_gateway": "172.17.0.1", "host_reach": "refused", **top}
        script = (_decisions() + "\nconst b=" + json.dumps(payload) + ";\n"
                  "const r=chooseBackend(b);\n"
                  "console.log(JSON.stringify({chosen:r.chosen, declared:r.declared,\n"
                  "  live:r.live.length, text:nothingYetText(b, r.declared)}));\n")
        p = subprocess.run(["node", "--input-type=module", "-e", script],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    # --- the reported defect ------------------------------------------------ #

    def test_a_declared_backend_is_used_even_when_nothing_answers(self):
        """The whole bug, in one assertion."""
        r = self._run([ENV_OLLAMA, GUESS_VLLM, GUESS_OLLAMA])
        self.assertIsNotNone(r["chosen"], "nothing to prefill from, so the form keeps "
                                          "its 127.0.0.1 markup — the reported defect")
        self.assertEqual(r["chosen"]["base_url"], "http://ollama:11434/v1")
        self.assertEqual(r["chosen"]["engine"], "ollama")

    def test_the_declared_engine_is_named_even_when_it_is_not_ollama(self):
        """On the gpu profile the untouched form read "Ollama" over a vLLM install,
        because "Ollama" is merely the first <option> in the markup."""
        r = self._run([ENV_VLLM, GUESS_VLLM, GUESS_OLLAMA])
        self.assertEqual(r["chosen"]["engine"], "vllm", r)

    def test_a_live_engine_still_outranks_a_declared_one_that_is_down(self):
        """Live first. A dead compose URL must not displace something answering."""
        r = self._run([ENV_OLLAMA, GUESS_OLLAMA, HOST_LIVE])
        self.assertEqual(r["chosen"]["base_url"], "http://172.17.0.1:11434/v1", r)
        self.assertEqual(r["live"], 1)

    def test_a_guess_is_never_offered_as_a_declaration(self):
        """With nothing declared and nothing up, the markup literal is CORRECT —
        it is the bare-metal default. Prefilling `backends[0]` instead would hand
        a bare-metal box vllm:8002, which is worse than the honest default."""
        r = self._run([GUESS_VLLM, GUESS_OLLAMA])
        self.assertIsNone(r["chosen"], r)
        self.assertIsNone(r["declared"], r)

    # --- and the copy has to describe THIS machine -------------------------- #

    def test_a_down_compose_service_is_not_blamed_on_a_host_ollama(self):
        r = self._run([ENV_OLLAMA, GUESS_VLLM, GUESS_OLLAMA])
        self.assertNotIn("OLLAMA_HOST", r["text"],
                         "told to reconfigure a host Ollama they never installed")
        self.assertIn("ollama", r["text"])
        self.assertIn("docker compose logs ollama", r["text"], r["text"])

    def test_a_real_host_ollama_still_gets_the_host_advice(self):
        """The compose branch must key on the DECLARED candidate, not on "any
        compose candidate" — api_backends appends ollama-service/vllm-service
        unconditionally, so this box has them too."""
        host_down = {**HOST_LIVE, "up": False, "blind": False}
        r = self._run([host_down, GUESS_VLLM, GUESS_OLLAMA])
        self.assertIn("OLLAMA_HOST", r["text"], r["text"])

    def test_a_blocked_host_is_not_told_to_change_a_bind_address(self):
        """A firewall eating packets is not fixed by any bind address."""
        host_down = {**HOST_LIVE, "up": False, "blind": False}
        r = self._run([host_down], host_reach="dropped")
        self.assertNotIn("OLLAMA_HOST", r["text"], r["text"])
        self.assertIn("Firewall", r["text"])

    def test_a_bare_metal_box_is_not_told_about_containers(self):
        r = self._run([GUESS_OLLAMA], in_container=False, host_gateway="")
        self.assertIn("install Ollama", r["text"], r["text"])
        self.assertNotIn("OLLAMA_HOST", r["text"])


@unittest.skipUnless(shutil.which("node"), "needs node")
class SaveWarningsTests(unittest.TestCase):
    """api_save probes the engine before writing the address down; save() dropped
    the result. A setup saved against an address nothing answers on reported
    plain success and failed at the first message."""

    def test_the_save_path_renders_the_warnings_it_is_given(self):
        text = PAGE.read_text(encoding="utf-8")
        save = text.split("async function save(skip)", 1)[1].split("\nfunction finish", 1)[0]
        self.assertIn("d.warnings", save,
                      "api_save returns `warnings` (setup_wizard.py) and save() must "
                      "show them — computing a probe and discarding it is how a "
                      "broken setup reports success")


if __name__ == "__main__":
    unittest.main()
