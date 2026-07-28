"""Setup -> Cost panel: spend budgets and their thresholds.

Local inference is free; a cloud backend and the code-change agent are not.
These routes read and set the caps the dashboard warns against.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import settings
from .. import dashboard

router = APIRouter()

@router.get("/cost")
def cost_get():
    """Current electricity rate, currency, and spend/energy budgets + live
    daily totals (for the Setup hub Budgets editor + the Vitals budget bar)."""
    settings_ = dashboard.cost_settings()
    day = dashboard.perf_cost("1d")
    settings_["daily_spend_usd"] = day["spend_usd"]
    settings_["daily_energy_kwh"] = day["energy_kwh"]
    settings_["power_measured"] = day["power_measured"]
    return settings_

@router.post("/cost")
async def cost_set(request: Request):
    """Persist cost/budget settings to ava.yaml (cost.*) — no source edits."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    patch: dict = {}
    if "electricity_rate_per_kwh" in body:
        try:
            patch["electricity_rate_per_kwh"] = max(0.0, float(body["electricity_rate_per_kwh"]))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "rate must be a number"}, status_code=400)
    if body.get("currency"):
        patch["currency"] = str(body["currency"])[:3]
    if isinstance(body.get("budgets"), dict):
        b = {}
        for k in ("daily_usd", "monthly_usd", "daily_kwh"):
            v = body["budgets"].get(k)
            if v in (None, "", 0):
                b[k] = None                       # clear the budget
            else:
                try:
                    b[k] = round(max(0.0, float(v)), 2)
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "error": f"{k} must be a number"},
                                        status_code=400)
        patch["budgets"] = b
    if not patch:
        return JSONResponse({"ok": False, "error": "nothing to set"}, status_code=400)
    try:
        settings.save_patch({"cost": patch})
        from .. import dashboard
        dashboard.invalidate_cost_cache()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True}

