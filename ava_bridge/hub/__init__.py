"""Setup Hub API panels — one module per tab in the Setup UI.

ava_bridge/hub_api.py was 1,719 lines and 53 handlers covering eight unrelated
concerns. The split follows the boundary the frontend already draws: every
module here backs exactly one panel in frontend/src/components/hub/panels/, so
"where does this endpoint live" has the same answer on both sides.

Each panel exposes a prefix-less `router`; hub_api.py owns the single
APIRouter(prefix="/api/hub") and includes them, so paths are unchanged and the
prefix is stated once.
"""
