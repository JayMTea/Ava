# productivity/ — calendar, tasks, notes

Tools that help Ava run your day. Start local-first (a journal/SQLite in `data/`)
before wiring external accounts; gate any account access behind `.env` secrets.

**Planned ideas:**
- `add_reminder` / `list_reminders` — local reminders store.
- `add_note` / `search_notes` — quick capture + recall.
- `calendar_today` / `add_event` — read-only first, then create with confirmation.
- `set_timer` — kitchen/pomodoro timer surfaced in the UI.

Add one: `agent/new-tool.sh add_reminder --category productivity`
