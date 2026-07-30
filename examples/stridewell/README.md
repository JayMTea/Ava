# Stridewell: an example health & fitness connector

**Stridewell is fictional.** It was invented for Ava's examples and screenshots —
there is no such product, and nothing here contacts a real service. What is real is
the integration contract it implements, and the consent tiers in its manifest.

It exposes steps, resting heart rate, sleep and workout logging, and it exists to demonstrate one thing: **health data is `sensitive`, not `read`**.

## Run it

```bash
# 1. Start the app's own web server (its UI + /health + /tools + /call)
python examples/stridewell/server.py           # serves http://127.0.0.1:8481

# 2. Register it with Ava by dropping the folder into your data root
cp -r examples/stridewell "$AVA_HOME/connectors/stridewell"

# 3. Restart Ava — "Stridewell" is now in the left rail
```

No edits to Ava's code at any point. You get a rail tile with its own icon and
accent, its UI embedded same-origin under `/apps/stridewell/`, a health row in the Ops
dashboard, and its tools available to the agent.

## The part worth reading

Every discovered tool defaults to `sensitive` in `dynamic_access`, and only
`log_*` is `write`. That is deliberate: the tier answers *what does a disclosure
cost*, not *does this call mutate anything*. A resting heart rate is not made
harmless by being read-only, so classifying it `read` — which is what an author
reaches for first — would be the mistake.

Change `PORT` with `STRIDEWELL_PORT` if 8481 is taken. Full contract:
[docs/CONNECTOR_SDK.md](../../docs/CONNECTOR_SDK.md).
