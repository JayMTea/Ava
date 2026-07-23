# Connect your apps

Connecting an app wires it into everything Ava does: the app gets a tile in the
left rail, a health row on the dashboard, and its tools become things you can
ask for in plain language, each behind an auto-generated security policy.

There are two ways to do it. Most people should use the first one; the second
is for developers building their own connector, and its full reference is the
[Connector SDK](CONNECTOR_SDK.md).

---

## The no-code way: from the browser

Everything happens in the app, on the **Setup → Connectors** page. No files, no
terminal. The whole flow takes under a minute; here it is end to end, narrated
(sound on):

<video controls playsinline preload="metadata"
       style="width:100%;border-radius:8px"
       aria-label="Screen recording: connecting an app from the Setup hub, end to end">
  <source src="../assets/connect-app-tour.mp4" type="video/mp4">
  Your browser can't play video. <a href="../assets/connect-app-tour.mp4">Download the walkthrough</a>.
</video>

*(The recording uses sample data; your instance will show your own apps.)*

### Step 1: Open Setup, then Connectors

In Ava, open **Setup** (the sliders icon in the bottom-left flyout) and pick the
**Connectors** tab. You'll see every app already wired in, each with its health,
tool, and policy status.

![The Connectors tab: every connected app with enabled, tools ok, and policy ok badges, plus a Connect an app button](assets/connect-app-1-connectors.png)

### Step 2: Click "Connect an app", name it, paste its address

Give the app a name, then paste **where it is**: its web address (like
`http://127.0.0.1:9000`), or a start command for an MCP server (like
`npx -y @modelcontextprotocol/server-github`). Click **Detect**. Ava checks
what kind of app it is; you don't have to know.

![The Connect an app form: name and address filled in, Detect clicked, and "Found 3 tools" listed with their names](assets/connect-app-2-detected.png)

When detection succeeds you'll see the app's tools listed by name. Two optional
safety switches appear with them:

- **Ask me before Ava uses these.** Every call waits for your one-tap approval.
  Good for anything that spends money, sends messages, or deletes data.
- **Run it in an isolated container** (shown for start-command servers, and
  recommended). The server runs inside a locked-down, read-only container so it
  can't touch your files.

If Ava can't auto-detect the app (a plain web app with no tool list), the form
lets you describe its actions manually: what it does, the method, and the path.

**If your app needs a login,** paste its token / API key in the **Access token**
box. Ava saves it once to its own private secret store (`$AVA_HOME/secrets/`) —
it's **never** written into the app's config file and is never shown to the AI.
You won't be asked for it again: it's reused automatically every time you deploy
or redeploy the app. (Already keep the token in an environment variable? Name it
in the optional *Environment variable name* field instead, and Ava reads it from
there — a real environment variable always wins.) On the Connectors list an app
that needs auth shows **credential saved** once it has one, or **needs a token**
until you add it — use **Add credential** in the row's ⋯ menu any time.

That one saved token also **keeps you signed in to the app itself.** If the app
has its own password screen, Ava presents the token to its embedded page for you,
so you connect once and are never asked to log in again when you open it inside
Ava. (This needs the app to accept that token — apps built to Ava's Connector SDK
do; if you're building the app, see
[Single sign-on](CONNECTOR_SDK.md#single-sign-on-apps-with-their-own-login).)

### Step 3: Click "Connect app"

That's it. The app appears in the Connectors list — enabled, with its actions
counted and a **needs deploy** marker until its tools and egress policy are
generated into the agent.

![The Connectors list after connecting: the new app at the top, enabled, with its action count and deploy state](assets/connect-app-3-connected.png)

### Step 4: Preview, then Deploy

The row offers **Preview**, and — while its tools or egress policy are out of
date — a **Deploy** button (everything else, like the push token, appearance,
manifest editor, and remove, lives in the row's **⋯** menu):

- **Preview** shows exactly what will be generated: the agent tools and the
  egress security policy (what the app is allowed to reach, and nothing else).
- **Deploy** regenerates them and loads them into the agent's sandbox; the row
  then reads **deployed** (redeploy any time from the ⋯ menu). If the sandbox
  isn't reachable from the browser, the page shows the one command that loads
  them (`cd agent && ./install.sh`); run it once and you're done.

Now ask Ava to use the app: *"create a note about tomorrow's demo"*. The tool
call shows up in the chat's tool chips and on the Operations page like
everything else Ava does.

---

## The developer way: CLI and manifest

The GUI writes a **connector manifest** for you; you can also write it yourself.
Each connector declares its health probe, metrics source, egress policy, and
agent actions, and the dashboard, charts, and agent tools all update
automatically:

```bash
ava connector new myapp                 # scaffold a manifest
# edit connector.yaml: health probe, perf log, actions
ava connector tools    myapp --write    # generate the agent tools
ava connector policies myapp --write    # generate its egress policy
```

- Full manifest reference, with a runnable worked example:
  [App connectors](CONNECTOR_SDK.md)
- Sensing devices (Arduino, ESP32, smart-home hubs):
  [Device connectors](DEVICE_CONNECTORS.md)

---

## What you get, automatically

From one connection, with no edits to Ava's code:

| You get | Where it shows up |
|---|---|
| A left-rail tile (embedded UI) | the app's own page inside Ava |
| A health row | Operations → Service health |
| Live agent tools | chat ("create a note…"), tool chips, Operations |
| An egress security policy | the agent can reach this app's routes and nothing else |
| Optional approval gates | a one-tap prompt before sensitive actions run |
