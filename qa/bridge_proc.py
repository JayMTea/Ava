"""Launch the real bridge (`serve.py` under uvicorn) as a subprocess for the
live tiers — the exact process a user runs, including startup hooks, real
sockets, SSE, and static file serving.

Each instance gets a brand-new AVA_HOME and its own ports; the fake LLM/app
servers from conftest are plain TCP so they serve subprocesses too.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from qa.env_recipe import free_port, hermetic_env, shims_pythonpath

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BridgeProc:
    def __init__(self, llm_url: str, home: str | None = None,
                 env_extra: dict | None = None):
        self.home = home or tempfile.mkdtemp(prefix="ava-qa-live-")
        self.port = free_port()
        self.router_port = free_port()
        self.llm_url = llm_url
        self.env_extra = dict(env_extra or {})
        self.proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _env(self) -> dict:
        env = dict(os.environ)
        env.update(hermetic_env(self.home, self.llm_url,
                                port=self.port, router_port=self.router_port))
        # Block the optional voice stack so boot is fast and deterministic.
        env["PYTHONPATH"] = shims_pythonpath() + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self.env_extra)
        return env

    def start(self, timeout: float = 60.0) -> "BridgeProc":
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(_REPO, "serve.py")],
            cwd=_REPO, env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.wait_health(timeout)
        return self

    def wait_health(self, timeout: float = 60.0) -> float:
        """Block until /api/health answers; returns seconds it took."""
        t0 = time.time()
        deadline = t0 + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                out = (self.proc.stdout.read() or b"").decode(errors="replace")
                raise AssertionError(f"bridge exited rc={self.proc.returncode}:\n{out[-4000:]}")
            try:
                with urllib.request.urlopen(self.base_url + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        return time.time() - t0
            except OSError:
                pass
            time.sleep(0.25)
        self.stop()
        raise AssertionError(f"bridge not healthy on :{self.port} after {timeout}s")

    def restart(self, timeout: float = 60.0) -> "BridgeProc":
        """Stop and boot again on the SAME home and ports — the persistence test."""
        self.stop()
        return self.start(timeout)

    def stop(self) -> None:
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None
