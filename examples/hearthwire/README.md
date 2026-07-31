# Hearthwire: an example home control connector

**Hearthwire is fictional.** It was invented for Ava's examples and screenshots —
there is no such product, and nothing here contacts a real service. What is real is
the integration contract it implements, and the consent tiers in its manifest.

It exposes thermostat, lights, scenes and a door lock, and it exists to demonstrate one thing: **`physical` is the one tier Ava will never infer for you**.

## Run it

```bash
# 1. Start the app's own web server (its UI + /health + /tools + /call)
python examples/hearthwire/server.py           # serves http://127.0.0.1:8483

# 2. Register it with Ava by dropping the folder into your data root
mkdir -p "${AVA_HOME:-$PWD}/connectors"          # ava setup does not create this
cp -r examples/hearthwire "$AVA_HOME/connectors/hearthwire"

# 3. Restart Ava — "Hearthwire" is now in the left rail
```

No edits to Ava's code at any point. You get a rail tile with its own icon and
accent, its UI embedded same-origin under `/apps/hearthwire/`, a health row in the Ops
dashboard, and its tools available to the agent.

## The part worth reading

`_infer_access` derives `read`, `write` and `destructive` from the HTTP method
and the action name. It never derives `physical`, because nothing in a request
shape tells you a relay is attached — so it has to be declared, and once declared
it cannot be granted away.

Without the `dynamic_access` block, `unlock_front_door` would fall back to `write`
and be handled like renaming a scene. It is also the single entry under `confirm:`:
lights and thermostats are reversible by whoever is standing in the room, while a
lock is the call that can let a stranger in.

Change `PORT` with `HEARTHWIRE_PORT` if 8483 is taken. Full contract:
[docs/CONNECTOR_SDK.md](../../docs/CONNECTOR_SDK.md).
