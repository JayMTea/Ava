# Connect your apps

<ul class="ava-steps" markdown="1">
<li markdown="span">[<b>Step 1</b>Install](../deploy/README.md)</li>
<li markdown="span">[<b>Step 2</b>Pick a model](CHOOSE_A_MODEL.md)</li>
<li markdown="span">[<b>Step 3</b>Set up the agent](AGENT_RUNTIME.md)</li>
<li class="is-now"><span><b>Step 4</b>Connect your apps</span></li>
</ul>

**At the end of this step:** an app you already run becomes something you can ask
Ava for in plain language, behind an auto-generated security policy. If it serves
a web page, it also gets its own tile in Ava's sidebar.

**Time:** the walkthrough below runs about two minutes; the connecting itself
takes well under one. It happens in the browser. No files, no terminal.

Connecting an app wires it into everything Ava does: its tools become things you
can ask for in plain language, each behind an auto-generated security policy. If
it serves a web UI or a health endpoint, it also gets a tile in the left rail and
a row on Operations → Service health.

There are two ways to do it. Most people should use the first one; the second is
for developers building their own connector, and its full reference is the
[Connector SDK](CONNECTOR_SDK.md).

---

## The no-code way: from the browser

Everything happens in the app, on the **Setup → Connectors** page. Here it is end
to end, narrated (sound on):

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Screen recording: connecting an app from the Setup hub, end to end">
  <source src="../assets/connect-app-tour.mp4" type="video/mp4">
  <track kind="captions" srclang="en" label="English"
         src="../assets/connect-app-tour.vtt">
  Your browser can't play video. <a href="../assets/connect-app-tour.mp4">Download the walkthrough</a>.
</video>

*(The app in the recording is a real one - a personal training log with its own
codebase, its own SQLite file and an MCP server, which knows nothing about Ava.
The six tools Ava discovers are the ones its server actually advertises, and the
figures it reports at the end come out of its own database. Ava's surrounding
numbers are sample data. Any app you already run connects the same way - to
follow along with one you can start in a terminal right now, use
[`examples/device-app/`](../examples/device-app/).)*

### Step 1: Open Setup, then Connectors

In Ava, open **Setup** (the sliders icon - in the sidebar when it's expanded, in
the flyout at the foot of the icon rail when it's collapsed) and pick the
**Connectors** tab. You'll see every app already wired in, grouped into
**Devices**, **Apps** and **Tools**, each row carrying its transport, action
count, deploy state and credential state.

![The Connectors tab: a Connect an app or device button above the list, which is split into Apps and Tools - each row shows its transport, action count and deployed state, with Permissions and Preview buttons](assets/connect-app-1-connectors.png)

### Step 2: Click "Connect an app or device", name it, paste its address

Give the app a name, then paste **where it is** - and be precise, because Ava
tries the address you give it and nothing else:

| What you are connecting | What to paste |
|---|---|
| An **MCP** server over HTTP (MCP is the open protocol apps use to advertise their tools to an AI) | the **MCP endpoint**, not the app's home page - usually `http://127.0.0.1:9000/mcp` |
| An MCP server you start yourself | its start command, like `npx -y @modelcontextprotocol/server-filesystem ~/notes` |
| A plain web app or REST API | its base address, `http://127.0.0.1:9000` |

Pasting the bare origin of an app whose MCP server lives at `/mcp` reports
*"No tools to auto-discover"*.

Then click **Detect**. Ava works out how to talk to it; you don't have to know
which of those it is.

??? note "What Detect can and cannot recognise"

    Detect recognizes MCP servers over Streamable HTTP, Ava's `ava-tools/1`
    facade, and OpenAPI. For a plain web app it looks for
    `/.well-known/ava.json`, a `/tools` facade, then `/openapi.json`.

    A server that only speaks MCP's deprecated HTTP+SSE transport is *not*
    auto-detected - Detect only ever tries Streamable HTTP - so wire it by hand
    with `mcp: {url: …/sse}`, which selects the legacy transport from the `/sse`
    suffix. See the [Connector SDK](CONNECTOR_SDK.md).

![The Connect an app form after Detect: the address http://127.0.0.1:8481 with the Detect button beside it, optional Access token, Health check URL and Environment variable name fields, and below them a green result panel reading "Found 6 tools via MCP (http)" listing today_summary, sleep_last_night, week_summary, recent_workouts, weight_trend and log_workout, with an unticked "Ask me before Ava uses these" box and a Connect app button](assets/connect-app-2-detected.png)

!!! warning "If you pasted a start command, Detect will run it. Check the isolation switch first."

    One switch appears **before** you click Detect, and it has to appear first,
    because Detect *runs* that command:

    **Run it in an isolated container** *(recommended, ticked by default whenever
    Docker is available)*. The server runs inside a locked-down, read-only
    container so it can't touch your files or read Ava's environment.

    **Untick it and Detect runs the command straight on this host, with Ava's
    environment.** Only do that for a command you trust. If Docker isn't
    installed the box is greyed out and Detect refuses until you explicitly
    accept running it on the host.

**After** detection succeeds you'll see the app's tools listed by name
(*"Found 6 tools"*), with one more switch:

- **Ask me before Ava uses these.** Every call waits for your one-tap approval,
  with no "always allow". Good for anything that spends money, sends messages,
  or deletes data.

Tick that, and this is what you get when Ava tries to use one of them. The call
does not go through until you answer:

![The approvals banner: "Approve action? Ava wants to run home-assistant.lock on Home Assistant", the argument entity=lock.front_door, the tier labelled "physical action - moves something in the real world; asks every time", and Approve and Deny buttons](assets/approvals-banner.png)

If Ava can't auto-detect the app (a plain web app with no tool list), the form
lets you describe its actions manually: what it does, the method, and the path.
If it *did* read an OpenAPI spec, those actions are pre-filled for you to review.

**If your app needs a login,** paste its token or API key in the **Access token /
API key** box: Ava saves it once to its own private secret store, never into the
app's config file, and never shows it to the AI.

??? note "Where that token is kept, how to reuse one you already have, and how it keeps you signed in"

    Ava writes it to `$AVA_HOME/secrets/env/<NAME>`, mode 0600. It is **never**
    written into the app's config file and is never shown to the AI. You won't be
    asked for it again: it's reused automatically every time you deploy or
    redeploy the app.

    Already keep the token in an environment variable? Name it in the optional
    *Environment variable name* field instead, and Ava reads it from there - a
    real environment variable always wins.

    On the Connectors list an app that needs auth shows **credential saved** once
    it has one, or **needs a token** until you add it. The row's ⋯ menu offers
    **Add credential** (or **Update credential** once one is saved) any time.

    That one saved token also **keeps you signed in to the app itself.** If the
    app has its own password screen, Ava presents the token to its embedded page
    for you, so you connect once and are never asked to log in again when you
    open it inside Ava. This needs the app to accept that token - apps built to
    Ava's Connector SDK do. If you're building the app, see
    [Single sign-on](CONNECTOR_SDK.md#single-sign-on-apps-with-their-own-login).

### Step 3: Click "Connect app"

That's it. The app appears in the Connectors list - **enabled**, labelled with the
transport Ava will speak to it (**MCP**, **tool facade** or **REST**), and marked
**needs deploy** until its tools and egress policy are generated into the agent.

![The Connectors list after connecting: the new app enabled, with its transport and deploy state](assets/connect-app-3-connected.png)

!!! note "Why some rows show an action count and some don't"

    A REST app shows its action count. An MCP or facade app doesn't: its tools
    are discovered live at call time, so there is no fixed number to print. The
    **Permissions** sheet lists them.

### Step 4: Preview, then Deploy

The row offers three buttons. Everything else - the push token, appearance,
manifest editor, and remove - lives in the row's **⋯** menu.

| Button | What it does |
|---|---|
| **Permissions** | Lists every action the app exposes, each with its access tier. The tier decides the consent (see below). |
| **Preview** | Shows exactly what will be generated: the agent tools, and the *egress policy* - the list of addresses this app is allowed to reach, and nothing else. |
| **Deploy** | Regenerates both and loads them into the agent's sandbox. The row then reads **deployed**. It appears while the tools or policy are out of date; redeploy any time from the ⋯ menu. |

The tier decides how Ava asks, and you cannot be talked past it:

| Access tier | What happens on every call |
|---|---|
| `read` | runs silently |
| `sensitive`, `write` | asks once, and offers *always allow* |
| `destructive`, `physical` | asks every single time, and can never be silenced |

??? note "Why a freshly connected MCP app lists every tool as `write`, including its reads"

    A caveat worth knowing: tiers for an MCP or facade app come from the
    manifest's `dynamic_access` patterns, and the browser flow doesn't write any
    - so a freshly connected MCP app lists **every** tool as `write`, including
    its pure reads, and each one asks the first time. That is deliberate (Ava
    will not guess that a tool named `today_summary` is safe), but if you want
    reads to run silently, add a `dynamic_access:` block via the row's **Edit
    manifest** - see [Connector SDK §5](CONNECTOR_SDK.md).

If the sandbox isn't reachable from the browser, the page shows the one command
that loads them (`cd agent && ./install.sh`); run it once and you're done.

### Step 5: If it has a web UI, it gets its own place in the sidebar

A connected app doesn't have to be just a set of tools. When it serves its own
web interface, it appears under **Apps** in the left rail carrying its own accent
colour, and Ava reverse-proxies that interface same-origin - so the app's own
pages render inside Ava, already signed in, with no second front end to build.

Which field turns this on depends on what Detect found:

- **Detect found a web page at the address you gave** (a plain web app, or an app
  whose facade and UI share an origin): an **Add it to Ava's sidebar** switch
  appears, ticked by default. Leave it on.
- **Detect found an MCP server** (you pasted `…/mcp`, or a start command): Ava
  has only seen a tool endpoint, so it does *not* assume there's a UI and the
  switch doesn't appear. Put the app's own web address into the **Web UI
  address** field instead. Skip it and you get tools with no tile, and the app
  lands under **Tools** rather than **Apps** - which is the right answer for a
  headless MCP server.

You can add or change this later from the row's **Edit manifest** (a `ui:` block)
or **Appearance** (icon and accent):

![The connected app's own web page embedded in Ava: its stat cards for steps, resting heart rate, sleep, weight and this week's sessions, above a table of recent workouts](assets/connect-app-4-embedded.png)

### Step 6: Ask

Now just ask. Ava picks the tool, calls it, and answers out of the app's own
data. The call shows up in the chat's tool chips and on the Operations page like
everything else Ava does:

![A chat turn: the question "How did my training go this week?" answered with six sessions, 261 active minutes and 22.8 kilometres, noting the figures came from the connected app on this machine, with a Tools used chip beneath](assets/connect-app-5-asked.png)

---

## What you get, automatically

From one connection, with no edits to Ava's code:

| You get | Where it shows up |
|---|---|
| A health row (whenever you gave a health check URL) | Operations → Service health |
| Live agent tools | chat ("create a note…"), tool chips, Operations → Tool usage |
| An egress security policy | the agent can reach this app's routes and nothing else |
| Approval gates on anything but a `read` | a one-tap prompt before the action runs |
| A full audit trail | Data → History (request, approval, denial, call) |
| A left-rail tile (embedded UI) - *when the app has a web UI you pointed Ava at* | the app's own page inside Ava |

## The developer way: CLI and manifest

The GUI writes a **connector manifest** for you. You can write one yourself
instead: `ava connector new <id>`, declare its actions, then generate the agent
tools and the egress policy from it.

- Full manifest reference, with a runnable worked example:
  [App connectors](CONNECTOR_SDK.md)
- Sensing devices (Arduino, ESP32, smart-home hubs):
  [Device connectors](DEVICE_CONNECTORS.md)

---

**Next:** [What Ava does](capabilities/index.md) - the capabilities you just
finished wiring up, one page each.
