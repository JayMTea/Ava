# Ledgerline: an example personal finance connector

**Ledgerline is fictional.** It was invented for Ava's examples and screenshots —
there is no such product, and nothing here contacts a real service. What is real is
the integration contract it implements, and the consent tiers in its manifest.

It exposes account balances, spending summaries and statement export, and it exists to demonstrate one thing: **a read-only surface can still be sensitive end to end**.

## Run it

```bash
# 1. Start the app's own web server (its UI + /health + /tools + /call)
python3 examples/ledgerline/server.py           # serves http://127.0.0.1:8482

# 2. Register it with Ava by dropping the folder into your data root
mkdir -p "${AVA_HOME:-$PWD}/connectors"          # ava setup does not create this
cp -r examples/ledgerline "$AVA_HOME/connectors/ledgerline"

# 3. Restart Ava — "Ledgerline" is now in the left rail
```

No edits to Ava's code at any point. You get a rail tile with its own icon and
accent, its UI embedded same-origin under `/apps/ledgerline/`, a health row in the Ops
dashboard, and its tools available to the agent.

## The part worth reading

There is no `read` pattern in this manifest at all. Balances and transaction
history are sensitive whether or not the call changes something, so the catch-all
is `sensitive` and nothing overrides it.

`export_statements` additionally appears under `confirm:`. It only reads — but it
is the step that turns "Ava can see my accounts" into a file that can leave the
box, so Ava asks the owner first.

Change `PORT` with `LEDGERLINE_PORT` if 8482 is taken. Full contract:
[docs/CONNECTOR_SDK.md](../../docs/CONNECTOR_SDK.md).
