# finance/ — money (read-only first)

Financial tools. **Read-only by default.** Anything that *moves money* is a
separate, higher-risk decision: require a human confirmation step, least-privilege
scopes, and prefer aggregators with read-only tokens over raw bank credentials.
Keep secrets in `.env` / a vault — never in code or the sandbox.

**Planned ideas:**
- `get_quote` — stock/ETF/crypto price snapshot for a ticker (free quote source).
- `get_balances` — read-only account balances (aggregator, read-only token).
- `budget_status` — spend-vs-budget from a local ledger in `data/`.

Each network tool gets its own egress policy in `agent/policies/`. Add one:
`agent/new-tool.sh get_quote --category finance --with-policy --with-skill`
