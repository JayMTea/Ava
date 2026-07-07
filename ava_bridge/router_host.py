"""Embedded inference router — starts ava_bridge/router_app in-process.

Called from the bridge's startup hook so a fresh install gets a working
OpenAI-compatible control point on 127.0.0.1:8010 with zero extra services
(the same "in-process, not systemd" pattern as perf_store.start_scheduler and
hardware.start_sampler). If something already answers /healthz on the port —
e.g. an always-on standalone `ava-router` unit — the embedded starter yields
to it, so both deployment shapes share one code path.

Config (ava.yaml `inference.router.*` / env):
    embedded: auto | true | false   [AVA_ROUTER_EMBEDDED]  auto = start unless
                                    an external router already owns the port
    host: 127.0.0.1                 [AVA_ROUTER_HOST]
    port: 8010                      [AVA_ROUTER_PORT]
"""
from __future__ import annotations

import threading

import requests

from . import settings

_state = {"status": "idle", "thread": None, "detail": None}


def _cfg():
    host = str(settings.get("inference.router.host", "127.0.0.1",
                            env="AVA_ROUTER_HOST"))
    port = settings.get_int("inference.router.port", 8010, env="AVA_ROUTER_PORT")
    embedded = str(settings.get("inference.router.embedded", "auto",
                                env="AVA_ROUTER_EMBEDDED")).strip().lower()
    return host, port, embedded


def _external_alive(port: int) -> bool:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
        return r.ok
    except Exception:  # noqa: BLE001
        return False


def router_status() -> dict:
    """For doctor / the dashboard: how the router is being provided."""
    host, port, embedded = _cfg()
    return {"host": host, "port": port, "embedded_setting": embedded,
            "status": _state["status"], "detail": _state["detail"],
            "alive": _external_alive(port)}


def start_embedded() -> str:
    """Start the router in a daemon thread unless disabled or already served.

    Returns one of: "external" (something already owns the port),
    "embedded" (we started it), "disabled" (config says no), or
    "failed: <reason>". Never raises — chat degrades loudly elsewhere.
    """
    host, port, embedded = _cfg()
    if embedded in ("false", "0", "no", "off"):
        _state.update(status="disabled")
        return "disabled"
    if _external_alive(port):
        _state.update(status="external")
        return "external"

    try:
        import uvicorn
        from .router_app import create_app
        app = create_app(bind_host=host)
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        # Running outside the main thread: no signal handlers to install.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        def _run():
            try:
                server.run()
            except SystemExit:
                pass
            except Exception as e:  # noqa: BLE001 — e.g. lost a port race to
                # a systemd router that started after our probe; it now owns
                # the port and serves the same API, so just note and yield.
                _state.update(status="external", detail=f"bind failed: {e}")

        t = threading.Thread(target=_run, name="ava-router", daemon=True)
        t.start()
        _state.update(status="embedded", thread=t)
        return "embedded"
    except Exception as e:  # noqa: BLE001
        _state.update(status="failed", detail=str(e))
        return f"failed: {e}"
