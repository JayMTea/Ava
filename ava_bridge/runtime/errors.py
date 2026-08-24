"""Errors the agent-runtime seam raises, in their own module.

Deliberately NOT in `base.py`. `turns.py`, the gateway routes and the websocket
relay all need to catch these, and none of them should have to import the
abstract base class to do it — importing an ABC to name an exception is how a
module boundary quietly becomes a dependency. `tests/test_module_boundaries.py`
would not catch that, because the import would be perfectly public.

Every `code` here is an Ava `error_code`, in the family `features.preflight()`
and `frontend/src/lib/fixes.ts` already speak. That is the whole point of
mapping the gateway's vocabulary onto ours: `fixes.ts` resolves a fix link by
PATTERN, so a code in the right shape gets the owner a working link with no
frontend change at all.
"""
from __future__ import annotations


class GatewayError(RuntimeError):
    """A control-plane call failed.

    `code` is Ava's; `gw_code` is the gateway's own word for it, kept verbatim
    so a panel can print what the other side actually said rather than our
    paraphrase of it. Both matter: ours drives the fix link, theirs is what you
    search their docs for.
    """

    def __init__(self, message: str, code: str = "gateway_rpc_failed", *,
                 gw_code: str = "", detail: dict | None = None,
                 retryable: bool = False, retry_after_ms: int | None = None):
        super().__init__(message)
        self.code = code
        self.gw_code = gw_code
        self.detail = detail or {}
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms

    def as_body(self) -> dict:
        """The HTTP-200 failure body every coded route in this repo returns.

        `internal._told()` and `agent_runtime_server./run_turn` both answer a
        failure this way rather than with a status code, because the callers
        (`curl --fail` in the sandbox; `lib/api.ts`, which maps a bodyless 404
        to `bridge_outdated`) cannot see a body they were not given.
        """
        return {"ok": False, "error_code": self.code, "gw_code": self.gw_code,
                "message": str(self), "detail": self.detail,
                "retryable": self.retryable,
                **({"retry_after_ms": self.retry_after_ms}
                   if self.retry_after_ms is not None else {})}


class GatewayUnsupported(GatewayError):
    """This runtime has no control plane at all.

    Distinct from "the gateway refused the method": DirectRuntime and the CLI
    NemoClaw adapter simply do not have one, and telling the owner their gateway
    rejected a call it never received is a lie about which side is wrong.
    """

    def __init__(self, runtime_name: str = "this runtime"):
        super().__init__(
            f"{runtime_name} has no gateway control plane. Select the gateway "
            f"runtime with `agent.runtime: openclaw_gw` in ava.yaml.",
            code="agent_no_gateway")
