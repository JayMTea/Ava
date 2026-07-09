# Home Assistant connector

Connect **your** Home Assistant to Ava — Ava becomes the private, local AI brain
over the devices HA already manages, with every physical action governed:
reads run silently, actuation asks you **every time** (the `physical` tier is
never grantable).

Ava stays fully decoupled: this folder *is* the integration. No HA code,
assumption, or dependency lives in Ava core. Delete the folder to disconnect.

## Connect (three steps)

```bash
# 1. In Home Assistant: add the "MCP Server" integration
#    (Settings -> Devices & services -> Add integration -> MCP Server),
#    and create a long-lived access token (your profile -> Security).

# 2. Register the connector and point it at your HA:
cp -r examples/home-assistant "$AVA_HOME/connectors/home-assistant"
cat >> .env <<'EOF'
HASS_URL=http://homeassistant.local:8123
HASS_TOKEN=<your long-lived token>
EOF

# 3. Restart Ava (or `ava up`), then render its egress policy:
ava connector policies home-assistant --write && (cd agent && ./install.sh)
```

Ask Ava: *"what's the temperature in the living room?"* (silent read via
`GetLiveContext`) or *"turn off the kitchen lights"* (an approval prompt
appears — every time, by design).

Only entities you've [exposed to Assist](https://www.home-assistant.io/voice_control/voice_remote_expose_devices/)
are visible to Ava. See [docs/CONNECT_HOME_ASSISTANT.md](../../docs/CONNECT_HOME_ASSISTANT.md)
for the full walkthrough, the trust model, and how to relax specific tools.
