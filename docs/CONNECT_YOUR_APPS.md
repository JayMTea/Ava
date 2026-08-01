# Connect your apps

Connecting an app wires it into everything Ava does: its tools become things
you can ask for in plain language, each behind an auto-generated security
policy — and if it serves a web UI or a health endpoint, it also gets a tile
in the left rail and a row on Operations → Service health.

There are two ways to do it. Most people should use the first one; the second
is for developers building their own connector, and its full reference is the
[Connector SDK](CONNECTOR_SDK.md).

---

## The no-code way: from the browser

Everything happens in the app, on the **Setup → Connectors** page. No files, no
terminal. Here it is end to end, narrated (sound on) — about two minutes, and the
connecting itself takes well under one of them:

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Screen recording: connecting an app from the Setup hub, end to end">
  <source src="../assets/connect-app-tour.mp4" type="video/mp4">
  <track kind="captions" srclang="en" label="English"
         src="../assets/connect-app-tour.vtt">
  Your browser can't play video. <a href="../assets/connect-app-tour.mp4">Download the walkthrough</a>.
</video>

*(The app in the recording is a real one — Stridewell, a personal training log with
its own codebase, its own SQLite file and an MCP server, which knows nothing about
Ava. The six tools Ava discovers are the ones its server actually advertises, and the
figures it reports at the end come out of its own database. Ava's surrounding numbers
are sample data.)*

### Step 1: Open Setup, then Connectors

In Ava, open **Setup** (the sliders icon — in the sidebar when it's expanded, in
the flyout at the foot of the icon rail when it's collapsed) and pick the
**Connectors** tab. You'll see every app already wired in, grouped into
**Devices**, **Apps** and **Tools**, each row carrying its transport, action
count, deploy state and credential state.

![The Connectors tab: a Connect an app or device button above the list, which is split into Apps and Tools — each row shows its transport, action count and deployed state, with Permissions and Preview buttons](assets/connect-app-1-connectors.png)

### Step 2: Click "Connect an app or device", name it, paste its address

Give the app a name, then paste **where it is** — and be precise, because Ava
tries the address you give it and nothing else:

- **An MCP server over HTTP:** paste the **MCP endpoint**, not the app's home
  page — usually `http://127.0.0.1:9000/mcp`. Pasting the bare origin of an app
  whose MCP server lives at `/mcp` reports *"No tools to auto-discover"*.
- **An MCP server you start yourself:** paste its start command (like
  `npx -y @modelcontextprotocol/server-filesystem ~/notes`).
- **A plain web app / REST API:** paste its base address
  (`http://127.0.0.1:9000`). Ava looks for `/.well-known/ava.json`, a
  `/tools` facade, then `/openapi.json`.

Then click **Detect**. Ava works out how to talk to it; you don't have to know
which of those it is. (Detect recognizes MCP servers over Streamable HTTP,
Ava's `ava-tools/1` facade, and OpenAPI. A server that only speaks MCP's
deprecated HTTP+SSE transport is *not* auto-detected — Detect only ever tries
Streamable HTTP — so wire it by hand with `mcp: {url: …/sse}`, which selects the
legacy transport from the `/sse` suffix. See the
[Connector SDK](CONNECTOR_SDK.md).)

![The Connect an app form after Detect: the address http://127.0.0.1:8481 with the Detect button beside it, optional Access token, Health check URL and Environment variable name fields, and below them a green result panel reading "Found 6 tools via MCP (http)" listing today_summary, sleep_last_night, week_summary, recent_workouts, weight_trend and log_workout, with an unticked "Ask me before Ava uses these" box and a Connect app button](assets/connect-app-2-detected.png)

**Before** you click Detect, if what you pasted is a start command, one switch
appears — and it has to appear first, because Detect *runs* that command:

- **Run it in an isolated container** *(recommended, ticked by default whenever
  Docker is available)*. The server runs inside a locked-down, read-only
  container so it can't touch your files or read Ava's environment. **Untick it
  and Detect runs the command straight on this host, with Ava's environment** —
  only do that for a command you trust. If Docker isn't installed the box is
  greyed out and Detect refuses until you explicitly accept running it on the
  host.

**After** detection succeeds you'll see the app's tools listed by name
(*"Found 6 tools"*), with one more switch:

- **Ask me before Ava uses these.** Every call waits for your one-tap approval,
  with no "always allow". Good for anything that spends money, sends messages,
  or deletes data.

If Ava can't auto-detect the app (a plain web app with no tool list), the form
lets you describe its actions manually: what it does, the method, and the path.
If it *did* read an OpenAPI spec, those actions are pre-filled for you to review.

**If your app needs a login,** paste its token / API key in the **Access token /
API key** box. Ava saves it once to its own private secret store
(`$AVA_HOME/secrets/env/<NAME>`, mode 0600) — it's **never** written into the
app's config file and is never shown to the AI. You won't be asked for it again:
it's reused automatically every time you deploy or redeploy the app. (Already
keep the token in an environment variable? Name it in the optional *Environment
variable name* field instead, and Ava reads it from there — a real environment
variable always wins.) On the Connectors list an app that needs auth shows
**credential saved** once it has one, or **needs a token** until you add it — the
row's ⋯ menu offers **Add credential** (or **Update credential** once one is
saved) any time.

That one saved token also **keeps you signed in to the app itself.** If the app
has its own password screen, Ava presents the token to its embedded page for you,
so you connect once and are never asked to log in again when you open it inside
Ava. (This needs the app to accept that token — apps built to Ava's Connector SDK
do; if you're building the app, see
[Single sign-on](CONNECTOR_SDK.md#single-sign-on-apps-with-their-own-login).)

### Step 3: Click "Connect app"

That's it. The app appears in the Connectors list — **enabled**, labelled with the
transport Ava will speak to it (**MCP**, **tool facade** or **REST**), and marked
**needs deploy** until its tools and egress policy are generated into the agent.
(A REST app also shows its action count. An MCP or facade app doesn't: its tools
are discovered live at call time, so there's no fixed number to print — the
**Permissions** sheet lists them.)

![The Connectors list after connecting: the new app enabled, with its transport and deploy state](assets/connect-app-3-connected.png)

### Step 4: Preview, then Deploy

The row offers **Permissions** and **Preview**, and — while its tools or egress
policy are out of date — a **Deploy** button (everything else, like the push
token, appearance, manifest editor, and remove, lives in the row's **⋯** menu):

- **Permissions** lists every action the app exposes with its access tier, and
  the tier decides the consent: `read` runs silently, `sensitive`/`write` ask
  once and offer *always allow*, `destructive`/`physical` ask every single time
  and can never be silenced.
  **A caveat worth knowing:** tiers for an MCP or facade app come from the
  manifest's `dynamic_access` patterns, and the browser flow doesn't write any —
  so a freshly connected MCP app lists **every** tool as `write`, including its
  pure reads, and each one asks the first time. That is deliberate (Ava will not
  guess that a tool named `today_summary` is safe), but if you want reads to run
  silently, add a `dynamic_access:` block via the row's **Edit manifest** — see
  [Connector SDK §5](CONNECTOR_SDK.md).
- **Preview** shows exactly what will be generated: the agent tools and the
  egress security policy (what the app is allowed to reach, and nothing else).
- **Deploy** regenerates them and loads them into the agent's sandbox; the row
  then reads **deployed** (redeploy any time from the ⋯ menu). If the sandbox
  isn't reachable from the browser, the page shows the one command that loads
  them (`cd agent && ./install.sh`); run it once and you're done.

### Step 5: If it has a web UI, it gets its own place in the sidebar

A connected app doesn't have to be just a set of tools. When it serves its own
web interface, it appears under **Apps** in the left rail carrying its own accent
colour, and Ava reverse-proxies that interface same-origin — so the app's own
pages render inside Ava, already signed in, with no second front end to build.

Which field turns this on depends on what Detect found:

- **Detect found a web page at the address you gave** (a plain web app, or an app
  whose facade and UI share an origin): an **Add it to Ava's sidebar** switch
  appears, ticked by default. Leave it on.
- **Detect found an MCP server** (you pasted `…/mcp`, or a start command): Ava
  has only seen a tool endpoint, so it does *not* assume there's a UI and the
  switch doesn't appear. Put the app's own web address into the **Web UI
  address** field instead. Skip it and you get tools with no tile, and the app
  lands under **Tools** rather than **Apps** — which is the right answer for a
  headless MCP server.

You can add or change this later from the row's **Edit manifest** (a `ui:` block)
or **Appearance** (icon and accent):

![Stridewell's own web page embedded in Ava: its stat cards for steps, resting heart rate, sleep, weight and this week's sessions, above a table of recent workouts](assets/connect-app-4-embedded.png)

### Step 6: Ask

Now just ask. Ava picks the tool, calls it, and answers out of the app's own
data. The call shows up in the chat's tool chips and on the Operations page like
everything else Ava does:

![A chat turn: the question "How did my training go this week?" answered with six sessions, 261 active minutes and 22.8 kilometres, noting the figures came from Stridewell on this machine, with a Tools used chip beneath](assets/connect-app-5-asked.png)

---

## The developer way: CLI and manifest

The GUI writes a **connector manifest** for you; you can also write it yourself.
Each connector declares its health probe, metrics source, egress policy, and
agent actions, and the dashboard, charts, and agent tools all update
automatically:

```bash
ava connector new myapp                 # scaffold $AVA_HOME/connectors/myapp/connector.yaml
$EDITOR "$AVA_HOME/connectors/myapp/connector.yaml"   # see the note below
ava connector tools    myapp --write    # generate the agent tools
ava connector policies myapp --write    # generate its egress policy
(cd agent && ./install.sh)              # load them into the agent sandbox
```

**Don't skip the edit.** The scaffold is the annotated reference template with
every optional block commented out, so it declares a health probe and nothing
else. Until you uncomment an `actions:` list or an `mcp:` block there is no agent
surface to render, and both generate commands say so quietly rather than failing:

```
$ ava connector tools myapp --write

0 tool(s) written — run `cd agent && ./install.sh` to deploy into the sandbox.
$ echo $?
0
```

Once the manifest declares something, the same commands write real files
(`agent/mcp_server_connectors/apps/myapp/…` and
`agent/policies/generated/myapp.yaml`). `./install.sh` needs the agent runtime
provisioned first — with no NemoClaw CLI or sandbox it stops with the exact next
command (`ava agent provision --install`, or `nemoclaw onboard`) rather than
half-deploying.

(Or click **Deploy** on the connector's row in Setup → Connectors, which runs
all three for you.)

- Full manifest reference, with a runnable worked example:
  [App connectors](CONNECTOR_SDK.md)
- Sensing devices (Arduino, ESP32, smart-home hubs):
  [Device connectors](DEVICE_CONNECTORS.md)

---

## What you get, automatically

From one connection, with no edits to Ava's code:

| You get | Where it shows up |
|---|---|
| A health row (whenever you gave a health check URL) | Operations → Service health |
| Live agent tools | chat ("create a note…"), tool chips, Operations → Tool usage |
| An egress security policy | the agent can reach this app's routes and nothing else |
| Approval gates on anything but a `read` | a one-tap prompt before the action runs |
| A full audit trail | Setup → History (request, approval, denial, call) |
| A left-rail tile (embedded UI) — *when the app has a web UI you pointed Ava at* | the app's own page inside Ava |
