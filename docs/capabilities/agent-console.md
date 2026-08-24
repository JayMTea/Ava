# The Agent console

**What the agent has open, what it already did, and what it runs on its own.**

Ava's other five tabs are about Ava. This one is about the thing that does the
work: its sessions, its runs, and its schedule. It appears on every install —
when there is no agent gateway to talk to it says so plainly rather than
disappearing, because a tab that comes and goes is a capability people conclude
they do not have.

## Three sections, and why not fifteen

OpenClaw's own Control UI has roughly fifteen screens, and about half of them
are **configuration** — models, secrets, MCP servers, devices, plugins, voice.
Ava already owns a home for every one of those, and the rule in `CLAUDE.md` is
explicit: *a surface has exactly one home; when two places would show the same
thing, one owns it and the other links to it.*

So the split is:

> **This tab owns everything about a live session. Anything that outlives a
> session goes to the Setup page that already owns that job.**

Everything is still rebuilt in Ava's own design system. It is simply not all
rebuilt in one tab.

| Section | What it answers |
|---|---|
| **Sessions** | What is open and what it is doing — your own chats included, labeled as such |
| **Activity** | What already happened — grouped by day, with a run inspector |
| **Automations** | What runs when you are not watching |

## Watch here, talk in Chats

**Chats is the one place you talk to the agent; this console is where you watch
it work.** There is no composer anywhere in this tab, and no "New session"
button: sessions come from giving the agent work — a chat, an automation, a
channel — not from an empty shell opened here.

The sessions the Chats tab creates are keyed `<prefix>-<chat id>` (the prefix is
`AVA_OC_SESSION`, reported as `session_prefix` on `/api/gateway/status`), and the
console shows them rather than hiding them — under their own **Your chats**
heading, wearing the chat's real title and the Chats icon. A chat deleted from
Chats keeps its row (the agent-side transcript is a server fact worth auditing),
titled by its id with the note *deleted from Chats*. The bare prefix session,
where voice warm-ups and turns without a chat land, is titled **Background**.
Opening a chat-origin session offers **Reply in Chats**, which jumps to that
conversation; the thread itself stays a window.

There is deliberately **no fourth "Health" section**. A fresh install does need
somewhere that says "here is your gateway, here is what is wrong" — and that
place is the console's own empty state, which is where you already are. A route
for it would be a page you visit once and a tab that reads as dead weight
forever after. The status chip in the section bar covers the glance and links to
[Setup → Agent → Runtime](agent.md), which stays the page that owns it.

## Where the rest of it went

| Upstream screen | Home in Ava |
|---|---|
| Models & providers | **Setup → Agent → Providers** — quota and spend are a recurring check, not a one-time model pick |
| Secrets | **Setup → Agent → Secrets** — beside Skills, which are what consume them |
| Connection / gateway status | **Setup → Agent → Runtime** |
| Talk settings | **Setup → Agent → Voice** |
| Diagnostics & guided fixes | `lib/fixes.ts`, which resolves a fix link from an error code **by pattern** — so a new gateway diagnostic gets a working link with no frontend change |

Deliberately **not** rebuilt, each for a stated reason: three themes (Ava has
two, and `tokens.css` has exactly one override block); 24 locales (Ava has no
i18n layer at all, and a picker that changes nothing is worse than no picker);
a text-size control (browser zoom already does it); a profile page (Ava is
single-owner); and the plugin Workshop (Ava already has skill authoring, and two
authoring surfaces for one artifact is the worst available outcome).

## The session panel

Attached to a session and **addressable as a segment on it** —
`#agent/s/<id>/review` — because "this session, Review open" is a thing you send
somebody. It is also what makes the phone layout free: below the breakpoint the
same address becomes a full-screen sheet that Back dismisses.

| Panel | Built |
|---|---|
| Files, Tasks | Native |
| Review | Native, on a hand-built editor (see below) |
| Terminal | Native, on xterm — it rides the same socket as every other panel, so it inherits Ava's session auth, Ava's theme and the audit ledger |
| Browser | **Embedded.** The one exception |

A read-only **Side chat** panel used to be listed here and is retired: as
read-only it duplicated the thread it sat beside, and the place you talk is
Chats. Its address (`#agent/s/<id>/side`) still resolves — to the session, with
no panel — because an address that once worked must never become a dead one.

### Why Review is not CodeMirror

Line numbers, search, jump-to-line, edit, Cmd-S and compare-and-swap conflict
detection are all met by `lib/CodeArea.tsx`. CodeMirror is not one dependency —
realistically 6–10 packages and 350–500 kB — against a frontend that ships
**five** runtime dependencies and a CI job that byte-compares the built bundle.
The only thing lost is syntax highlighting, which was never in the requirement.

If highlighting becomes non-negotiable, the one acceptable addition is a single
zero-dependency highlighter inside Review's own chunk, with a ceiling of
**40 kB gzipped**. A number makes that decision testable;
`tests/test_bundle_budget.py` is where it would be enforced.

### Why the browser panel is the exception

A browser panel is a live page with snapshots, freehand annotation and element
inspection. Rebuilding it would cost more bundle than the entire rest of the
console, and it is the upstream's job. Ava draws the container, the tab strip
and the reload action; the frame supplies content only.

## What the Run Inspector will and will not tell you

Each run shows its provenance — trust domain, ingress, invoker — and its
decision receipts. One distinction is load-bearing:

- **enforced** — the decision actually gated something.
- **attribution only** — it was recorded, and it stopped nothing.

Rendering those identically would let a reader conclude a policy was *applied*
when it was merely *noted*, which is the most misleading thing an audit view can
do. They get different words and different tones.

The page also states its own limits: this is best-effort operational evidence,
not a compliance archive. Prompts, command bodies, arguments, paths and
credentials are never recorded here — so an absent decision means it was not
captured, **not** that it did not happen.

## What it costs to run

The console only reaches the gateway when `agent.runtime: openclaw_gw` is
selected and a token is configured. Without one it renders its empty states and
Ava keeps working on whichever runtime `agent.runtime` names — chat included.

Two things worth knowing before switching:

- The gateway token carries `operator.admin`, and it is gated by Ava's session
  cookie alone. Anyone with Ava's password can drive the agent's control plane,
  including its terminal. `SECURITY.md` §4a says so at length.
- Ava refuses to send that token anywhere but loopback unless
  `agent.gateway.allow_remote` is deliberately true.
