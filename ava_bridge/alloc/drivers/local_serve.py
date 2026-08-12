"""Start and stop a local vLLM server through `deploy/local-serve.sh`.

The one driver that can bring an engine up from nothing. Every other adapter
actuates something an operator already arranged - a container, a unit, an
endpoint that unloads on request - so Ava could stop a model it had not started
but never start one it had not been handed. That gap is why swapping the brain
meant editing ava.yaml and restarting a shell by hand.

**The launcher is `deploy/local-serve.sh`, not a command assembled here.** That
script already resolves the tool-call and reasoning parsers per model family
(deploy/model-flags.conf), clamps context, and picks the port. Rebuilding any of
that in Python would be a second source of truth for flags whose whole failure
mode is silence: a wrong `--tool-call-parser` returns no tool calls at all,
looking exactly like a model that chose not to use one.

**A dry run happens BEFORE anything is spawned.** `AVA_SERVE_DRY_RUN=1` makes the
script resolve and print the arguments without touching a live server, so a
model whose family has no parser, or whose id is misspelled, fails here - in
under a second, with the reason - instead of after a 40 GB load and a crash loop.

**The process is detached and its logs go to a file.** Ava's bridge is a web
worker; a child sharing its lifetime would die on the next reload and take the
brain with it. `start_new_session` puts the engine in its own process group so a
restart of Ava does not kill it, and so stopping it can signal the whole group
rather than leaking the vLLM workers the launcher forks.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time

from ... import settings
from ..base import ActionResult, DriverContext, ModelDriver, ReleaseMode, Residency

_SCRIPT = "deploy/local-serve.sh"

# One state file, so a bridge restart can still find (and stop) the engine it
# started. Keyed by port: that is what makes an engine reachable, and what a
# second start would collide with.
_STATE = "local_serve.json"


def _state_path() -> str:
    return os.path.join(settings.logs_dir(), _STATE)


def _read_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 - absent or corrupt both mean "nothing known"
        return {}


def _write_state(obj: dict) -> None:
    os.makedirs(settings.logs_dir(), exist_ok=True)
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, _state_path())   # atomic: a torn state file is unreadable


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ProcessLookupError, ValueError):
        return False
    return True


def _serving(port: int, model_id: str, timeout: float = 2.0):
    """Is the engine on `port` serving THIS model? None = could not ask.

    Asks what it holds rather than whether the port is open: a port answering
    with a different model is exactly what a half-finished swap leaves behind,
    and treating "something is listening" as success is what would let Ava
    report a brain change that never happened.
    """
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=timeout)
        if not r.ok:
            return None
        ids = [str(m.get("id") or "") for m in (r.json().get("data") or [])]
    except Exception:  # noqa: BLE001
        return None
    return any(i == model_id for i in ids) if ids else False


def _tail(path: str, lines: int = 3) -> str:
    """The last few log lines, for a failure the owner has to act on."""
    try:
        with open(path, "rb") as f:
            data = f.read()[-4000:]
        out = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
        return " / ".join(out[-lines:])[:300] or "no output"
    except Exception:  # noqa: BLE001
        return "no output"


class LocalServeDriver(ModelDriver):
    """A vLLM server this box owns, started and stopped by Ava."""

    name = "local-serve"
    RELEASE_MODES: tuple[ReleaseMode, ...] = ("stop",)
    SELF_RESTORING = False

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        cfg = ctx.config or {}
        self.port = int(cfg.get("port") or os.environ.get("AVA_SERVE_PORT") or 8002)
        self.script = os.path.join(settings.CODE_ROOT, cfg.get("script") or _SCRIPT)

    # ---- can this run here at all -------------------------------------------
    def usable(self) -> bool:
        return os.path.isfile(self.script) and bool(shutil.which("bash"))

    def unusable_reason(self) -> str:
        if not os.path.isfile(self.script):
            return f"{_SCRIPT} is not in this checkout"
        return "bash is not available"

    def validate(self) -> list[str]:
        if not os.path.isfile(self.script):
            return [f"driver 'local-serve' needs {_SCRIPT}; it is not here"]
        return []

    # ---- the two required questions -----------------------------------------
    def residency(self) -> Residency:
        entry = _read_state().get(str(self.port)) or {}
        if entry.get("model") != self.ctx.model_id:
            # A different model on our port is somebody else's residency, not
            # ours. Reporting it as ours would offer its memory as reclaimable.
            return Residency(resident=None, gib=self.ctx.weight_gb, measured=False)
        if not _alive(entry.get("pid")):
            return Residency(resident=False, gib=0.0, measured=False)
        live = _serving(self.port, self.ctx.model_id)
        if live is None:
            # Up but not answering yet (or no longer). UNKNOWN, never False:
            # memory we cannot see is memory we cannot promise to free.
            return Residency(resident=None, gib=self.ctx.weight_gb, measured=False)
        return Residency(resident=bool(live), gib=self.ctx.weight_gb, measured=False)

    def release(self, mode: ReleaseMode, *, need_gib: float | None = None,
                abort: threading.Event | None = None,
                timeout: float = 180.0) -> ActionResult:
        st = _read_state()
        entry = st.get(str(self.port)) or {}
        pid = entry.get("pid")
        if not _alive(pid):
            return ActionResult(ok=True, acted=False, detail="already stopped")

        before = self.ctx.free_gib()
        # Signal the GROUP. The launcher forks vLLM workers; killing only the
        # shell leaves them holding every byte of the pool.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(OSError, ProcessLookupError, ValueError):
                os.killpg(os.getpgid(int(pid)), sig)
            deadline = time.time() + (timeout if sig is signal.SIGTERM else 15)
            while time.time() < deadline and _alive(pid):
                if abort is not None and abort.is_set():
                    return ActionResult(ok=False, acted=True, detail="aborted")
                time.sleep(0.5)
            if not _alive(pid):
                break

        st.pop(str(self.port), None)
        _write_state(st)

        # `ok` is "did the pool actually come back", not "did the signal send".
        after = self.ctx.free_gib()
        freed = (round(after - before, 2)
                 if after is not None and before is not None else None)
        if need_gib:
            got = self.ctx.wait_free(need_gib, timeout=timeout)
            return ActionResult(ok=bool(got), acted=True, freed_gib=freed,
                                detail=f"stopped vLLM on :{self.port}")
        return ActionResult(ok=not _alive(pid), acted=True, freed_gib=freed,
                            detail=f"stopped vLLM on :{self.port}")

    # ---- the one thing no other driver can do -------------------------------
    def acquire(self, *, abort: threading.Event | None = None,
                timeout: float = 600.0) -> ActionResult:
        if not self.usable():
            return ActionResult(ok=False, acted=False, detail=self.unusable_reason())

        model_id = self.ctx.model_id
        st = _read_state()
        entry = st.get(str(self.port)) or {}
        if _alive(entry.get("pid")) and entry.get("model") == model_id:
            return ActionResult(ok=True, acted=False, detail="already running")
        if _alive(entry.get("pid")):
            return ActionResult(ok=False, acted=False,
                                detail=(f"port {self.port} is serving "
                                        f"{entry.get('model')}; stop it first"))

        # PREFLIGHT. Resolve the launcher's arguments without launching, so a bad
        # model id or an unresolvable parser costs a second instead of a load.
        env = {**os.environ, "AVA_MODEL": model_id,
               "AVA_SERVE_PORT": str(self.port), "AVA_SERVE_DRY_RUN": "1"}
        try:
            dry = subprocess.run(["bash", self.script], env=env, cwd=settings.CODE_ROOT,
                                 capture_output=True, text=True, timeout=60)
        except Exception as e:  # noqa: BLE001
            return ActionResult(ok=False, acted=False, detail=f"preflight failed: {e}")
        if dry.returncode != 0:
            why = (dry.stderr or dry.stdout or "").strip().splitlines()
            return ActionResult(ok=False, acted=False,
                                detail=(f"cannot serve {model_id}: "
                                        f"{why[-1] if why else 'preflight failed'}"))

        log_path = os.path.join(settings.logs_dir(), f"vllm-{self.port}.log")
        os.makedirs(settings.logs_dir(), exist_ok=True)
        env.pop("AVA_SERVE_DRY_RUN")
        try:
            with open(log_path, "ab", buffering=0) as log:
                proc = subprocess.Popen(
                    ["bash", self.script], env=env, cwd=settings.CODE_ROOT,
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    start_new_session=True)   # own group; survives a bridge reload
        except Exception as e:  # noqa: BLE001
            return ActionResult(ok=False, acted=False, detail=f"could not start: {e}")

        st[str(self.port)] = {"pid": proc.pid, "model": model_id,
                              "log": log_path, "started_at": time.time()}
        _write_state(st)
        self.ctx.log(f"starting vLLM on :{self.port} for {model_id} (log: {log_path})")

        # HEALTH GATE. "Started" is not "serving": a vLLM boot takes minutes and
        # can still fail on OOM or a bad parser. Ava must not report the brain
        # swapped until the engine says it holds this model.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if abort is not None and abort.is_set():
                self.release("stop")
                return ActionResult(ok=False, acted=True, detail="aborted during start")
            if proc.poll() is not None:
                tail = _tail(log_path)
                st.pop(str(self.port), None)
                _write_state(st)
                return ActionResult(ok=False, acted=True,
                                    detail=f"engine exited during start: {tail}")
            if _serving(self.port, model_id):
                return ActionResult(ok=True, acted=True,
                                    detail=f"serving {model_id} on :{self.port}")
            time.sleep(2.0)

        return ActionResult(ok=False, acted=True,
                            detail=(f"still not serving after {int(timeout)}s - "
                                    f"see {log_path}"))
