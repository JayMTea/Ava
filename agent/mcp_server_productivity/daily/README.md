# daily/ — everyday briefing tools

The "what's going on today" set Ava reaches for in normal conversation.

**Tools:** `get_weather` (Open-Meteo, no key).

**Planned ideas:**
- `get_news` — headlines by topic/region (e.g. GDELT or an RSS aggregator; no key).
- `get_markets` — index/ticker snapshot for a morning brief (see also `finance/`).
- `on_this_day` / `word_of_the_day` — light daily facts.

Each tool that hits the network needs its own least-privilege egress policy in
`agent/policies/`. Add one:
`agent/new-tool.sh get_news --category daily --with-policy --with-skill`
