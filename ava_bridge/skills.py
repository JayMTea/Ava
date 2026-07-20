"""Skill registry — the agent's SKILL.md capability files, surfaced to the UI.

A *skill* is a folder with a ``SKILL.md``: YAML frontmatter (``name`` +
``description``, plus optional ``title`` / ``summary`` / ``category`` / ``icon``
/ ``tools`` / ``app``) and a markdown body that coaches the model on WHEN and
HOW to use its tools. ``app`` names the connector id the skill drives — for
skills whose tools are discovered dynamically (``actions.discover`` / ``mcp``)
the tool names carry no connector prefix, so this is how the UI knows to mark
the skill with that app's identity accent. ``agent/install.sh`` deploys every discovered skill INTO the nemoclaw
sandbox (``nemoclaw skill install``), so the filesystem is the single source of
truth: drop a folder → it appears here and in the Agent tab, no registration.
Forks and the private overlay work the same way.

Discovered from (both included; overlay adds private skills):
  1. core     <repo>/agent/skills/<id>/SKILL.md
  2. overlay  <overlay>/skills/<id>/SKILL.md   (AVA_OVERLAY, default overlay/agent)

Deploy state mirrors ``connectors.undeployed()``: a skill present in the repo
but absent from the sandbox deploy manifest (or edited since it was installed)
shows as "not deployed / stale → re-provision", so the UI never claims a
capability the agent doesn't actually have yet.
"""
import hashlib
import os
import re
import time
from typing import Dict, List

from . import config, settings

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

CORE_DIR = os.path.join(config.ROOT, "agent", "skills")


def _overlay_dir() -> str:
    base = os.environ.get("AVA_OVERLAY") or os.path.join(config.ROOT, "overlay", "agent")
    return os.path.join(base, "skills")


def manifest_path() -> str:
    """Where install.sh records what it deployed into the sandbox."""
    return os.path.join(settings.data_dir(), "skills_deployed.json")


# Tool names in these skills are always snake_case with an underscore and are
# named in the DESCRIPTION ("by calling her get_weather tool"); single-word
# argument names in the body's call tables (prompt, seed, days) are not. So we
# read tools from the description first and only fall back to backticked body
# tokens when the description names none — that kills the arg-name noise.
_TOOL_DENYLIST = {"tool_search_code", "canvas", "code", "max_chars"}
_TOOL_IN_TEXT = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_TOOL_IN_BODY = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
_CACHE: Dict[str, object] = {"ts": 0.0, "list": None, "errors": []}


def _humanize(skill_id: str) -> str:
    """"ava-self-coding" → "Self Coding" (drop the brand prefix, title-case)."""
    name = re.sub(r"^ava[-_]", "", skill_id)
    return re.sub(r"[-_]+", " ", name).strip().title() or skill_id


def _first_sentence(desc: str) -> str:
    """Owner-facing one-liner from a model-facing description: cut the verbose
    "Use whenever…/Trigger keywords…" tail that's written for the classifier."""
    if not desc:
        return ""
    for marker in ("Use whenever", "Use when", "Trigger keywords", "Trigger:"):
        i = desc.find(marker)
        if i > 0:
            desc = desc[:i]
            break
    return desc.strip().rstrip(".") + "." if desc.strip() else ""


def _extract_tools(description: str, body: str) -> List[str]:
    def _collect(tokens) -> list[str]:
        seen: list[str] = []
        for tok in tokens:
            if tok not in _TOOL_DENYLIST and tok not in seen:
                seen.append(tok)
        return seen
    # Description first (precise); body backticks only when the description
    # names no tools — so argument names in call tables never leak in.
    tools = _collect(_TOOL_IN_TEXT.findall(description or ""))
    if not tools:
        tools = _collect(_TOOL_IN_BODY.findall(body or ""))
    return tools[:8]


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into (frontmatter dict, body). Tolerates a missing or
    malformed frontmatter block — a skill is still shown, just with fallbacks."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw = text[3:end]
            body = text[end + 4:]
            if yaml is not None:
                try:
                    fm = yaml.safe_load(raw) or {}
                    if isinstance(fm, dict):
                        return fm, body
                except Exception:  # noqa: BLE001
                    pass
            return {}, body
    return {}, text


def _load_dir(base: str, source: str, errors: list) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        if name.startswith((".", "_")):
            continue
        path = os.path.join(base, name, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            errors.append({"id": name, "path": path, "error": str(e)})
            continue
        fm, body = _parse_frontmatter(text)
        if not fm.get("name") and not fm.get("description"):
            errors.append({"id": name, "path": path,
                           "error": "SKILL.md has no name/description frontmatter"})
        skill_id = str(fm.get("name") or name)
        desc = str(fm.get("description") or "").strip()
        tools = fm.get("tools")
        if not isinstance(tools, list):
            tools = _extract_tools(desc, body)
        out[name] = {
            "id": skill_id,
            "dir": name,
            "title": str(fm.get("title") or _humanize(skill_id)),
            "summary": str(fm.get("summary") or _first_sentence(desc)),
            "description": desc,
            "category": (str(fm["category"]) if fm.get("category") else None),
            "icon": (str(fm["icon"]) if fm.get("icon") else None),
            "app": (str(fm["app"]) if fm.get("app") else None),
            "tools": [str(t) for t in tools],
            "source": source,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return out


def _deployed_map() -> dict | None:
    """{dir_name: sha256} of what install.sh last deployed, or None if the agent
    has never been provisioned (no manifest) — in which case deploy state is
    genuinely unknown rather than "not deployed"."""
    import json
    path = manifest_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    out = {}
    for row in data if isinstance(data, list) else []:
        if isinstance(row, dict) and row.get("name"):
            out[str(row["name"])] = str(row.get("sha256") or "")
    return out


def _deploy_state(skill: dict, deployed: dict | None) -> str:
    """deployed | stale | undeployed | unknown — the honest per-skill signal the
    Agent tab paints, so a just-added skill reads "re-provision", not "active"."""
    if deployed is None:
        return "unknown"
    have = deployed.get(skill["dir"])
    if have is None:
        return "undeployed"
    if have != skill["sha256"]:
        return "stale"
    return "deployed"


def _category_overrides() -> dict:
    """Owner-owned categorization, from ava.yaml `skills.categories: {id: Label}`
    (keyed by skill id or directory). This is where categories live — NOT baked
    into shipped SKILL.md files — so every fork defines its OWN taxonomy without
    editing shipped skills, and the product imposes none. Per-instance
    (ava.yaml is gitignored). Mirrors the no-shipped-eval-data philosophy."""
    cats = settings.get("skills.categories", {}) or {}
    if not isinstance(cats, dict):
        return {}
    return {str(k): str(v) for k, v in cats.items() if v}


def _skills_cfg(cfg: dict) -> dict:
    """The `skills:` block of a mutable config dict, coerced to a dict."""
    block = cfg.setdefault("skills", {})
    if not isinstance(block, dict):
        block = cfg["skills"] = {}
    return block


def _categories_map(cfg: dict) -> dict:
    """The `skills.categories` override map of a mutable config dict."""
    block = _skills_cfg(cfg)
    cats = block.setdefault("categories", {})
    if not isinstance(cats, dict):
        cats = block["categories"] = {}
    return cats


def category_order() -> List[str]:
    """The owner's category ORDER — and the registry of owner-created
    categories: a label in ava.yaml `skills.category_order` is a real category
    even while it holds no skills, so "make a category, then fill it" works.
    The UI renders these first (in list order), then unlisted labels
    alphabetically, with the implicit "General" bucket pinned last."""
    order = settings.get("skills.category_order", []) or []
    if not isinstance(order, list):
        return []
    out: List[str] = []
    for v in order:
        label = str(v).strip()
        if label and label not in out:
            out.append(label)
    return out


def set_category_order(order: list) -> List[str]:
    """Persist the owner's category order (drag-to-reorder in the Hub). Labels
    are trimmed/deduped; "General" is the implicit uncategorized bucket the UI
    always pins last, so it never enters the stored list."""
    clean: List[str] = []
    for v in order:
        label = str(v).strip()
        if label and label != "General" and label not in clean:
            clean.append(label)
    cfg = settings.current_config()
    _skills_cfg(cfg)["category_order"] = clean
    settings.save_config(cfg)
    return clean


def create_category(name: str) -> bool:
    """Create an owner category (it may start empty): register it in the order
    list so it exists — and survives — with no skills filed under it yet."""
    label = (name or "").strip()
    if not label or label == "General":
        return False
    order = category_order()
    if label not in order:
        set_category_order([*order, label])
    return True


def delete_category(name: str) -> bool:
    """Remove a category: drop it from the order list and clear any skill
    overrides pointing at it (those skills fall back to their author hint or
    the General bucket). The UI only offers this on empty groups. False when
    nothing referenced the label."""
    label = (name or "").strip()
    if not label:
        return False
    cfg = settings.current_config()
    cats = _categories_map(cfg)
    members = [k for k, v in cats.items() if str(v) == label]
    order = category_order()
    if label not in order and not members:
        return False
    _skills_cfg(cfg)["category_order"] = [c for c in order if c != label]
    for k in members:
        cats.pop(k)
    settings.save_config(cfg)
    _CACHE["ts"] = 0.0
    return True


def set_category(skill_id: str, category: str | None) -> bool:
    """Recategorize one skill from the UI (drag-and-drop): write the owner-owned
    `skills.categories` map in ava.yaml — the same map _category_overrides
    reads — keyed by the skill's directory (stable across frontmatter renames).
    An empty/None category clears the override, so any author frontmatter hint
    shows again. False if the skill is unknown."""
    target = next((s for s in catalog() if skill_id in (s["id"], s["dir"])), None)
    if target is None:
        return False
    cfg = settings.current_config()
    cats = _categories_map(cfg)
    # One canonical key per skill: drop an id-keyed duplicate so it can't shadow.
    cats.pop(target["id"], None)
    label = (category or "").strip()
    if label:
        cats[target["dir"]] = label
    else:
        cats.pop(target["dir"], None)
    settings.save_config(cfg)
    _CACHE["ts"] = 0.0  # overrides changed — next catalog() re-reads them
    return True


def rename_category(old: str, new: str) -> int:
    """Rename a category wherever it's in effect: every skill whose EFFECTIVE
    category is `old` gets an owner override `new` — so renaming a group that
    only exists via author frontmatter hints (or the implicit "General" bucket)
    works the same as renaming an owner-defined one. The order list follows the
    rename, so the group keeps its position (and an empty owner-created
    category is renamable too). Returns skills updated."""
    old_label, new_label = old.strip(), new.strip()
    if not old_label or not new_label or old_label == new_label:
        return 0
    cfg = settings.current_config()
    cats = _categories_map(cfg)
    count = 0
    for s in catalog():
        if (s["category"] or "General") == old_label:
            cats.pop(s["id"], None)
            cats[s["dir"]] = new_label
            count += 1
    order = category_order()
    if old_label in order:
        new_order: List[str] = []
        for c in order:
            c = new_label if c == old_label else c
            if c != "General" and c not in new_order:
                new_order.append(c)
        _skills_cfg(cfg)["category_order"] = new_order
    if count or old_label in order:
        settings.save_config(cfg)
        _CACHE["ts"] = 0.0
    return count


def catalog(force: bool = False) -> List[dict]:
    """Every discovered skill (core + overlay), each carrying its deploy state.
    Cached ~30s; the filesystem is the source of truth so this stays automatic."""
    if not force and _CACHE["list"] is not None and time.time() - float(_CACHE["ts"]) < 30:
        return _CACHE["list"]  # type: ignore[return-value]
    errors: list = []
    merged = _load_dir(CORE_DIR, "core", errors)
    # Overlay can add private skills; an overlay id also shadows a core one.
    merged.update(_load_dir(_overlay_dir(), "overlay", errors))
    deployed = _deployed_map()
    overrides = _category_overrides()
    items = list(merged.values())
    for s in items:
        s["deployed"] = _deploy_state(s, deployed)
        # Effective category: owner ava.yaml override > author frontmatter hint.
        s["category"] = overrides.get(s["dir"]) or overrides.get(s["id"]) or s["category"]
    items.sort(key=lambda s: (0 if s["source"] == "core" else 1, s["title"].lower()))
    _CACHE.update(ts=time.time(), list=items, errors=errors)
    return items


def load_errors() -> List[dict]:
    """Skills whose SKILL.md couldn't be read/parsed on the last catalog()."""
    if _CACHE["list"] is None:
        catalog()
    return list(_CACHE["errors"])  # type: ignore[arg-type]


def summary() -> dict:
    """Compact rollup for the Agent status/panel: counts + deploy freshness."""
    items = catalog()
    return {
        "total": len(items),
        "deployed": sum(1 for s in items if s["deployed"] == "deployed"),
        "stale": sum(1 for s in items if s["deployed"] in ("stale", "undeployed")),
        "unknown": sum(1 for s in items if s["deployed"] == "unknown"),
    }


def body(skill_id: str) -> dict | None:
    """The full rendered-source of one skill: its SKILL.md markdown body (sans
    frontmatter), lazy-loaded when the UI expands a card. Matched by frontmatter
    name or directory. None if unknown. Kept out of catalog() so the list stays
    light as the skill count grows."""
    for s in catalog():
        if skill_id in (s["id"], s["dir"]):
            base = CORE_DIR if s["source"] == "core" else _overlay_dir()
            path = os.path.join(base, s["dir"], "SKILL.md")
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                return None
            _, md = _parse_frontmatter(text)
            return {"id": s["id"], "title": s["title"], "body": md.strip()}
    return None
