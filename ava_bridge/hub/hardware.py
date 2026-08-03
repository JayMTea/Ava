"""Setup -> Hardware: the operator-stated memory pool.

Ava measures what it can and refuses to guess at what it cannot. Two boxes it
genuinely cannot size:

  * an engine on the HOST while Ava runs in a container - it draws on the
    machine's memory and the machine's GPU, and Ava can see it answering
    without being able to measure the box it lives on;
  * any pool behind a probe that does not answer here.

In both, Ava withholds a tier rather than sizing one from the wrong machine.
That is right, and it left an owner who KNOWS the answer with no way to say it.
This route is that door, and `hwinfo.stated_fit_gb` is where precedence lives.

**It sets advice, not actuation.** The value reaches `hwinfo.fit_pool()`, which
recommends a model tier and nothing else. It deliberately never reaches
`fit_memory()`, the oracle `alloc/capacity.py` governs on - see the boundary
note in `hwinfo.fit_pool`.

`restart_required` is False, and that is engineered rather than hoped for:
`settings.save_patch` updates the in-process config, `stated_fit_gb()` reads it
per call, and this route drops the fit cache so the next read is the new value.
Same guarantee hub/branding.py makes, for the same reason - a setting an owner
is expected to tune is a setting that must not cost them a restart to tune.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import hwinfo, settings

router = APIRouter()


@router.get("/hardware/pool")
def pool_get():
    """What is stated, what was measured, and whether this form may change it."""
    import os
    stated = hwinfo.stated_fit_gb()
    env = os.environ.get(hwinfo._STATED_ENV)
    return {
        "stated_gb": stated,
        "source": ("env" if env else "config") if stated is not None else "",
        "env_var": hwinfo._STATED_ENV,
        # An env override cannot be edited away from a web form - the process
        # would go on ignoring ava.yaml and the owner would be left changing a
        # number that does nothing. Say so instead.
        "editable": stated is None or not env,
        "min_gb": hwinfo._STATED_MIN_GB,
        "max_gb": hwinfo._STATED_MAX_GB,
    }


@router.post("/hardware/pool")
async def pool_set(request: Request):
    """Set or clear the stated pool. `{"gb": null}` restores what Ava measures."""
    import os
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    if os.environ.get(hwinfo._STATED_ENV):
        return JSONResponse(
            {"ok": False, "error": f"{hwinfo._STATED_ENV} is set in this "
                                   "environment and takes precedence — change it "
                                   "there, or unset it to manage this here",
             "field": "gb"}, status_code=409)

    raw = body.get("gb")
    if raw in (None, "", 0):
        gb = None                                   # back to measured
    else:
        try:
            gb = round(float(raw), 1)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "memory must be a number",
                                 "field": "gb"}, status_code=400)
        # Refused, not clamped. A silently corrected value is one the owner
        # cannot see is wrong, and this number's whole job is to be believed.
        if not (hwinfo._STATED_MIN_GB <= gb <= hwinfo._STATED_MAX_GB):
            return JSONResponse(
                {"ok": False, "field": "gb",
                 "error": f"must be between {hwinfo._STATED_MIN_GB:g} and "
                          f"{hwinfo._STATED_MAX_GB:g} GB"}, status_code=400)

    try:
        settings.save_patch({"hardware": {"fit_memory_gb": gb}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    # The pool is TTL-cached, so without this the owner saves a value and watches
    # the old one for another three seconds — which reads as the save not working.
    hwinfo.reset_cache()
    return {"ok": True, "stated_gb": gb, "restart_required": False}
