# Contributing to Ava

Ava is a single-host, on-premise system. This guide covers the development
workflow and — most importantly — the **single-source-of-truth (SSOT) rules**
that keep the docs and diagrams 1:1 with the running system.

## 1. Local setup

```bash
cd ~/projects/Ava
source .venv/bin/activate              # Python venv (pyyaml, fastapi, etc.)
export PATH="$HOME/.local/bin:$PATH"    # d2 + d2plugin-tala live here
```

Prerequisites already installed on this host:

- **D2** (`~/.local/bin/d2`) with the **TALA** layout plugin
  (`~/.local/bin/d2plugin-tala`). TALA needs a token in
  `~/.config/tstruct/auth.json` (see `SECURITY.md`).
- Python venv at `.venv`.

## 2. The SSOT rule (read this first)

[`agent/docs/architecture.yaml`](agent/docs/architecture.yaml) **is the truth.**
Everything else is generated from it or validated against it:

- `agent/docs/diagrams/{system,network,policy,security}.{d2,svg}` — generated
- `README.md` §7 services table — generated (between `<!-- ARCH:... -->` markers)
- drift-check vs the running system (systemd units, ports, MCP tools, policies)

**Never** hand-edit a generated `.d2`/`.svg` or the services table. Edit the
manifest, then regenerate:

```bash
python agent/docs/arch.py sync     # regenerate diagrams + tables
python agent/docs/arch.py check    # validate manifest vs reality (--strict to fail)
```

The generator is `agent/docs/arch.py`. Diagram styling (theme, layout engine,
palettes, padding) is centralized in the manifest's `diagram_style` block — change
it there and every diagram updates consistently.

## 3. Common changes

### Add an MCP tool
1. Create the module under `agent/mcp_server/<category>/<name>.mjs` (exports
   `name: '<tool_name>'`).
2. Add the tool name to that category in `capabilities:` in the manifest.
3. If it needs network egress, add/extend a **narrow** policy (see below).
4. `python agent/docs/arch.py sync` → the capability boxes regenerate.
5. Re-run `agent/install.sh` to redeploy into the sandbox.

### Add an egress policy
1. Create `agent/policies/<name>.yaml` (least privilege — only the host:port and
   methods the tool needs).
2. Add a matching entry under `policies:` in the manifest (`name`, `purpose`,
   `tools`, `egress`). The drift check enforces a 1:1 mapping between manifest
   entries and policy files.
3. `sync` regenerates the policy-trace diagram.

### Add a service
Add it under `services:` (id, unit, command, bind, port, public). The drift check
verifies the systemd unit exists and (warns if) the port isn't listening.

## 4. Automation (do not bypass)

- **Git pre-commit hook** (`.git/hooks/pre-commit`): regenerates diagrams, stages
  them, and **blocks the commit** on `arch.py check --strict`.
- **systemd path-watcher** (`ava-arch-sync.path` → `ava-arch-sync.service`):
  watches the manifest/tools/policies and auto-runs `arch.py sync --commit`.
  Auto-commits are authored `Ava (auto-sync) <ava@localhost>`.

Because of the path-watcher, a manifest edit may be committed for you. That's
expected — verify with `git log --oneline` and keep your working tree clean.

## 5. Commit conventions

Use Conventional Commits:

```
feat(arch): add <thing>
fix(bridge): <thing>
docs(security): <thing>
style(arch): <visual-only change>
chore: <housekeeping>
```

Significant or hard-to-reverse decisions get an **ADR** under
[`agent/docs/adr/`](agent/docs/adr/) (copy `0000-template.md`).

## 6. After a sandbox rebuild

The OpenClaw sandbox config is ephemeral. After any rebuild, redeploy Ava's
tools, skills, and policies:

```bash
agent/install.sh
```

The MCP server logical id stays **`ava-tools`** — never rename it.

## 7. Before you push (sanity checklist)

- [ ] `python agent/docs/arch.py check --strict` passes (0 drift errors)
- [ ] Generated diagrams reflect your change (`git status` shows them updated)
- [ ] New secrets are `0600` and `.gitignore`d (never committed)
- [ ] New network access is expressed as a narrow policy, not a broad allow-rule
- [ ] `CHANGELOG.md` updated for user-facing changes
