"""Hub API — cookie-gated `/api/hub/*` routes powering the setup & control portal.

Thin wrappers over existing machinery (runtime status/provision, the connector
generators, settings.save_patch). Mounted into the bridge, so `auth_gate` covers
these automatically (they are NOT in auth._PUBLIC_PATHS — no middleware change).
Everything writable is written to $AVA_HOME/ava.yaml via settings.save_patch;
no source edits, ever. Model configuration reuses the proven /api/setup/* routes
(setup_wizard.py); this module adds the agent, connector, and system surfaces.
"""
from __future__ import annotations


from fastapi import APIRouter

# The ONE place the /api/hub prefix is stated. Panels expose prefix-less routers
# and are included below, so every hub path is this prefix plus the panel's own.
router = APIRouter(prefix="/api/hub")

# Panels live in ava_bridge/hub/, one module per Setup tab. They expose
# prefix-less routers so the /api/hub prefix is declared exactly once, here.
from .hub import (agent as _hub_agent, branding as _hub_branding,  # noqa: E402
                  connectors as _hub_connectors,
                  cost as _hub_cost, governance as _hub_governance,
                  memory as _hub_memory, models as _hub_models,
                  persona as _hub_persona, system as _hub_system,
                  voice as _hub_voice)
for _panel in (_hub_agent, _hub_branding, _hub_connectors, _hub_cost,
               _hub_governance, _hub_memory, _hub_models, _hub_persona,
               _hub_system, _hub_voice):
    router.include_router(_panel.router)


