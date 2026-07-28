"""The driver template in docs/ALLOCATION.md must produce a driver that WORKS.

It did not. The documented snippet declared

    RELEASE_MODES = (ReleaseMode.UNLOAD, ReleaseMode.STOP)

and implemented only `residency()` and `release()`. That is exactly the shape
`base.ModelDriver.validate()` flags — a reversible release option with neither an
`acquire()` override nor `SELF_RESTORING` — and the consequence is not cosmetic:
the restore path calls the ABC's default `acquire()`, which returns ok=True
unconditionally, so the model is stopped, recorded as restored, and never comes
back. The word `acquire` appeared nowhere in that section.

(Prose here deliberately avoids writing the broker's module-qualified name:
tests/test_alloc_isolation.py scans for that to decide which tests touch the
ledger, and this one only exec's a doc snippet against the ABC.)

Prose cannot be trusted to stay true, so this executes the documented code
against the real ABC and asserts the class it defines passes validate(). Follows
the docs-must-match-code convention of tests/test_diagram_sync.py.
"""
import os
import pathlib
import re
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-drvtmpl-test-"))

from ava_bridge.alloc.base import (
    ActionResult, DriverContext, ModelDriver, ReleaseMode, Residency,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "ALLOCATION.md"
SECTION = "Adding support for your engine"


def _template_source() -> str:
    body = DOC.read_text(encoding="utf-8")
    idx = body.find(SECTION)
    assert idx != -1, f"docs/ALLOCATION.md no longer has an '{SECTION}' section"
    blocks = re.findall(r"```python\n(.*?)```", body[idx:], re.S)
    assert blocks, "no python block under the driver-authoring section"
    return blocks[0]


class _FakeEngine:
    """Stands in for the author's own control plane, so the snippet can RUN."""

    def resident_gib(self):
        return 4.0

    def weights_loaded(self):
        return True

    def stop(self):
        pass

    def unload(self):
        pass

    def start(self):
        pass

    def wait_ready(self, timeout=0):
        return True


def _exec_template():
    ns = {
        "ModelDriver": ModelDriver, "ReleaseMode": ReleaseMode,
        "Residency": Residency, "ActionResult": ActionResult,
        "my_engine": _FakeEngine(),
    }
    src = _template_source()
    # The snippet's own import line names the real module; executing it is the
    # point, so leave it in and let it resolve for real.
    exec(compile(src, "docs/ALLOCATION.md", "exec"), ns)
    return ns


class DriverTemplateTests(unittest.TestCase):
    def setUp(self):
        self.ns = _exec_template()
        self.cls = self.ns.get("DRIVER")

    def test_the_template_exports_a_driver(self):
        self.assertIsNotNone(self.cls, "the snippet must end with `DRIVER = ...`")
        self.assertTrue(issubclass(self.cls, ModelDriver))

    def test_the_template_driver_has_a_name(self):
        """Without one, register() drops it and `driver: <name>` can never
        select it — silently."""
        self.assertTrue((getattr(self.cls, "name", "") or "").strip())

    def test_the_template_driver_passes_validate(self):
        drv = self.cls(DriverContext(model_id="t", weight_gb=1.0))
        problems = drv.validate()
        self.assertEqual(problems, [], (
            "the documented driver template does not satisfy the ABC's own "
            "contract, so anyone following the docs gets a broken driver: "
            f"{problems}"))

    def test_a_reversible_release_without_acquire_is_still_caught(self):
        """The guard is only meaningful if validate() would fail the OLD shape."""
        class Broken(ModelDriver):
            name = "broken"
            RELEASE_MODES = (ReleaseMode.UNLOAD, ReleaseMode.STOP)

            def residency(self):
                return Residency(resident=True, gib=1.0)

            def release(self, mode, *, need_gib=None, abort=None, timeout=180.0):
                return ActionResult(ok=True)

        problems = Broken(DriverContext(model_id="t")).validate()
        self.assertTrue(problems, (
            "validate() no longer flags a reversible release with no acquire() "
            "override — the check this whole test relies on has gone"))

    def test_the_template_release_honours_wait_free(self):
        """`ok` must reflect whether the pool actually came back."""
        drv = self.cls(DriverContext(
            model_id="t", weight_gb=1.0,
            free_gib=lambda: 10.0,
            wait_free=lambda target, **kw: False))     # the memory never returns
        res = drv.release(ReleaseMode.STOP, need_gib=4.0)
        self.assertFalse(res.ok, "a release whose memory never came back reported ok")
        self.assertTrue(res.acted, "it IS down, so Ava must still owe the restore")


if __name__ == "__main__":
    unittest.main()
