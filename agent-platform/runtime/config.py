"""Load, interpolate and validate every configuration file in the platform.

`load_platform(root)` is the single entry point. It returns a fully cross-checked
PlatformConfig or raises ConfigError listing *every* problem found, so one run of
`python scripts/check.py` shows the whole list instead of the first failure.

Conventions enforced here:
- every YAML file may use ${VAR} / ${VAR:-default} environment references;
- every file with a schema in schemas/ is validated against it;
- every cross-reference (agent -> route -> model -> provider, agent -> tool -> policy,
  workflow -> agent, ...) must resolve.
"""

from __future__ import annotations

import fnmatch
import importlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .policy_engine import PolicyEngine
from .types import AgentSpec, ConfigError, ModelSpec, SkillSpec, ToolSpec

ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")
TEMPLATE_REF = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
BUNDLED_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

POLICY_FILES = {
    "tool_permissions": "tool-permissions.yaml",
    "approvals": "approvals.yaml",
    "data_access": "data-access.yaml",
    "network": "network.yaml",
    "safety": "safety.yaml",
}
MEMORY_FILES = {
    "memory": "memory.yaml",
    "retrieval": "retrieval.yaml",
    "consolidation": "consolidation.yaml",
    "namespaces": "namespaces.yaml",
}
BUILTIN_MOCK_PROVIDER = {
    "schema_version": 1,
    "name": "mock",
    "type": "mock",
    "max_data_classification": "restricted",
}


def interpolate_env(value: Any) -> Any:
    """Replace ${VAR} and ${VAR:-default} in every string of a loaded YAML document."""
    if isinstance(value, str):
        return ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [interpolate_env(v) for v in value]
    if isinstance(value, dict):
        return {k: interpolate_env(v) for k, v in value.items()}
    return value


def schema_validator(schemas_dir: Path, name: str, definition: str | None = None) -> Draft202012Validator:
    """Validator for schemas/<name>.schema.json (optionally one of its $defs), with every
    schema in the directory registered so cross-file $refs (task -> artifact) resolve."""
    resources = []
    for path in sorted(schemas_dir.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resources.append((contents.get("$id", path.name), Resource.from_contents(contents)))
    schema = json.loads((schemas_dir / f"{name}.schema.json").read_text(encoding="utf-8"))
    if definition:
        schema = {**schema, "$ref": f"#/$defs/{definition}"}
    return Draft202012Validator(schema, registry=Registry().with_resources(resources))


def load_yaml(path: Path) -> Any:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return interpolate_env(data) if data is not None else {}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown file with YAML frontmatter into (frontmatter, body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing YAML frontmatter (file must start with '---')")
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            if not isinstance(meta, dict):
                raise ValueError("frontmatter must be a YAML mapping")
            return meta, "\n".join(lines[i + 1 :]).strip()
    raise ValueError("unterminated YAML frontmatter (no closing '---')")


@dataclass
class PlatformConfig:
    root: Path
    manifest: dict[str, Any]
    instructions: str
    agents: dict[str, AgentSpec]
    skills: dict[str, SkillSpec]
    tools: dict[str, ToolSpec]
    workflows: dict[str, dict[str, Any]]
    models: dict[str, ModelSpec]
    routes: dict[str, list[str]]
    fallback_on: list[str]
    providers: dict[str, dict[str, Any]]
    policies: dict[str, dict[str, Any]]
    memory: dict[str, dict[str, Any]]
    observability: dict[str, dict[str, Any]]
    sandbox: dict[str, Any]
    mcp: dict[str, Any]
    schemas_dir: Path = BUNDLED_SCHEMAS
    warnings: list[str] = field(default_factory=list)


def resolve_schemas_dir(root: Path) -> Path:
    """A platform root's own schemas/ wins; otherwise use the ones next to this runtime.
    That lets secondary roots (test fixtures, the template's reference example) omit them."""
    own = root / "schemas"
    return own if (own / "agent.schema.json").exists() else BUNDLED_SCHEMAS


class _Loader:
    def __init__(self, root: Path, check_imports: bool):
        self.root = root
        self.schemas_dir = resolve_schemas_dir(root)
        self.check_imports = check_imports
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self._validators: dict[tuple[str, str | None], Draft202012Validator] = {}

    # -- helpers ---------------------------------------------------------------------------

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def schema(self, name: str, definition: str | None = None) -> Draft202012Validator:
        key = (name, definition)
        if key not in self._validators:
            self._validators[key] = schema_validator(self.schemas_dir, name, definition)
        return self._validators[key]

    def check(self, instance: Any, validator: Draft202012Validator, where: str) -> bool:
        errors = sorted(validator.iter_errors(instance), key=str)
        for err in errors:
            loc = "/".join(str(p) for p in err.absolute_path)
            self.problems.append(f"{where}: {loc + ': ' if loc else ''}{err.message}")
        return not errors

    def read_yaml(self, path: Path, *, required: bool = True) -> Any:
        if not path.exists():
            if required:
                self.problems.append(f"{self.rel(path)}: file is missing")
            return {}
        try:
            return load_yaml(path)
        except yaml.YAMLError as exc:
            self.problems.append(f"{self.rel(path)}: invalid YAML: {exc}")
            return {}

    # -- sections --------------------------------------------------------------------------

    def manifest(self) -> tuple[dict[str, Any], str]:
        manifest = self.read_yaml(self.root / "agent.yaml")
        self.check(manifest, self.schema("platform"), "agent.yaml")
        instructions_path = self.root / manifest.get("instructions", "AGENT.md")
        if not instructions_path.exists():
            self.problems.append(f"agent.yaml: instructions file {self.rel(instructions_path)} is missing")
            return manifest, ""
        return manifest, instructions_path.read_text(encoding="utf-8")

    def policies(self) -> dict[str, dict[str, Any]]:
        out = {}
        for key, filename in POLICY_FILES.items():
            path = self.root / "policies" / filename
            data = self.read_yaml(path)
            self.check(data, self.schema("policy", key), self.rel(path))
            out[key] = data
        return out

    def tools(self) -> dict[str, ToolSpec]:
        path = self.root / "tools" / "registry.yaml"
        registry = self.read_yaml(path)
        specs: dict[str, ToolSpec] = {}
        for i, entry in enumerate(registry.get("tools", [])):
            where = f"tools/registry.yaml: tools[{i}]"
            if not self.check(entry, self.schema("tool"), where):
                continue
            if entry["id"] in specs:
                self.problems.append(f"{where}: duplicate tool id {entry['id']!r}")
                continue
            spec = ToolSpec(
                id=entry["id"],
                description=entry["description"].strip(),
                implementation=entry["implementation"],
                side_effects=entry["side_effects"],
                input_schema=entry["input_schema"],
                timeout_s=entry.get("timeout_s", 30),
                sandbox=entry.get("sandbox", "none"),
                max_output_chars=entry.get("max_output_chars", 20000),
            )
            if self.check_imports:
                module_name, _, attr = spec.implementation.partition(":")
                try:
                    if not callable(getattr(importlib.import_module(module_name), attr, None)):
                        self.problems.append(f"{where}: {spec.implementation} is not a callable")
                except ImportError as exc:
                    self.problems.append(f"{where}: cannot import {module_name}: {exc}")
            specs[spec.id] = spec
        if not specs:
            self.problems.append("tools/registry.yaml: no tools defined")
        return specs

    def skills(self) -> dict[str, SkillSpec]:
        skills: dict[str, SkillSpec] = {}
        base = self.root / "skills"
        for directory in sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []:
            path = directory / "SKILL.md"
            where = self.rel(path)
            if not path.exists():
                self.problems.append(f"{where}: every skill directory needs a SKILL.md")
                continue
            try:
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                self.problems.append(f"{where}: {exc}")
                continue
            if not self.check(meta, self.schema("skill"), where):
                continue
            if meta["name"] != directory.name:
                self.problems.append(f"{where}: name {meta['name']!r} must equal its directory name")
            skills[meta["name"]] = SkillSpec(
                name=meta["name"],
                description=meta["description"].strip(),
                body=body,
                path=directory,
                metadata=meta.get("metadata", {}),
            )
        return skills

    def models(self) -> tuple[dict[str, ModelSpec], dict[str, list[str]], list[str], dict[str, dict]]:
        providers: dict[str, dict[str, Any]] = {"mock": dict(BUILTIN_MOCK_PROVIDER)}
        for path in sorted((self.root / "models" / "providers").glob("*.yaml")):
            cfg = self.read_yaml(path)
            if not self.check(cfg, self.schema("models", "provider"), self.rel(path)):
                continue
            if cfg["name"] != path.stem:
                self.problems.append(f"{self.rel(path)}: name {cfg['name']!r} must equal the file name")
            providers[cfg["name"]] = cfg

        registry = self.read_yaml(self.root / "models" / "registry.yaml")
        self.check(registry, self.schema("models", "registry"), "models/registry.yaml")
        models: dict[str, ModelSpec] = {}
        for key, entry in (registry.get("models") or {}).items():
            if not isinstance(entry, dict):
                continue
            if entry.get("provider") not in providers:
                self.problems.append(
                    f"models/registry.yaml: {key}: unknown provider {entry.get('provider')!r} "
                    f"(add models/providers/{entry.get('provider')}.yaml)"
                )
                continue
            models[key] = ModelSpec(
                key=key,
                provider=entry["provider"],
                model=entry["model"],
                max_output_tokens=entry.get("max_output_tokens", 16000),
                context_window=entry.get("context_window"),
                supports_effort=entry.get("supports_effort", False),
                default_effort=entry.get("default_effort"),
                options=entry.get("options", {}),
            )

        routing = self.read_yaml(self.root / "models" / "routing.yaml")
        self.check(routing, self.schema("models", "routing"), "models/routing.yaml")
        routes = routing.get("routes") or {}
        for route, chain in routes.items():
            for key in chain or []:
                if key not in models:
                    self.problems.append(f"models/routing.yaml: route {route!r} references unknown model {key!r}")
        return models, routes, routing.get("fallback_on", []), providers

    def memory(self) -> dict[str, dict[str, Any]]:
        out = {}
        for key, filename in MEMORY_FILES.items():
            path = self.root / "memory" / filename
            data = self.read_yaml(path)
            self.check(data, self.schema("memory", key), self.rel(path))
            out[key] = data
        return out

    def agents(self) -> dict[str, AgentSpec]:
        agents: dict[str, AgentSpec] = {}
        base = self.root / "agents"
        for directory in sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []:
            path = directory / "agent.yaml"
            where = self.rel(path)
            if not path.exists():
                self.problems.append(f"{where}: every agent directory needs an agent.yaml")
                continue
            data = self.read_yaml(path)
            if not self.check(data, self.schema("agent"), where):
                continue
            if data["name"] != directory.name:
                self.problems.append(f"{where}: name {data['name']!r} must equal its directory name")
            instructions_path = directory / data["instructions"]
            if not instructions_path.exists():
                self.problems.append(f"{where}: instructions file {self.rel(instructions_path)} is missing")
                continue
            model = data["model"]
            limits = data.get("limits", {})
            memory = data.get("memory", {})
            agents[data["name"]] = AgentSpec(
                name=data["name"],
                description=data["description"].strip(),
                instructions=instructions_path.read_text(encoding="utf-8").strip(),
                route=model["route"],
                effort=model.get("effort"),
                max_output_tokens=model.get("max_output_tokens"),
                tools=data.get("tools", []),
                skills=data.get("skills", []),
                mcp_servers=data.get("mcp_servers", []),
                delegates_to=data.get("delegates_to", []),
                memory_read=memory.get("read", []),
                memory_write=memory.get("write", []),
                max_turns=limits.get("max_turns"),
                max_tool_calls=limits.get("max_tool_calls"),
                data_classification=data.get("data_classification", "internal"),
                output=data.get("output", {}),
                path=directory,
            )
        if not agents:
            self.problems.append("agents/: no agents defined")
        return agents

    def workflows(self) -> dict[str, dict[str, Any]]:
        workflows = {}
        for path in sorted((self.root / "workflows").glob("*.yaml")):
            data = self.read_yaml(path)
            if not self.check(data, self.schema("workflow"), self.rel(path)):
                continue
            if data["name"] != path.stem:
                self.problems.append(f"{self.rel(path)}: name {data['name']!r} must equal the file name")
            workflows[data["name"]] = data
        return workflows

    def plain_dir(self, *parts: str) -> dict[str, Any]:
        """Load every YAML file in a directory into {stem: data} without a schema."""
        directory = self.root.joinpath(*parts)
        return {p.stem: self.read_yaml(p) for p in sorted(directory.glob("*.yaml"))}

    # -- cross references ------------------------------------------------------------------

    def cross_check(self, cfg: PlatformConfig) -> None:
        p = self.problems
        if cfg.manifest.get("default_agent") not in cfg.agents:
            p.append(f"agent.yaml: default_agent {cfg.manifest.get('default_agent')!r} is not an agent in agents/")

        policy = PolicyEngine(cfg.policies, cfg.root)
        namespaces = cfg.memory.get("namespaces", {}).get("namespaces", {})
        mcp_servers = (cfg.mcp.get("servers") or {}).get("servers") or {}

        for name, agent in cfg.agents.items():
            where = f"agents/{name}/agent.yaml"
            if agent.route not in cfg.routes:
                p.append(f"{where}: model.route {agent.route!r} is not defined in models/routing.yaml")
            for skill in agent.skills:
                if skill not in cfg.skills:
                    p.append(f"{where}: skill {skill!r} does not exist in skills/")
            if agent.skills and "skill.load" not in agent.tools:
                p.append(f"{where}: agents with skills need the 'skill.load' tool to read them")
            for target in agent.delegates_to:
                if target not in cfg.agents:
                    p.append(f"{where}: delegates_to {target!r} is not an agent")
                if target == name:
                    p.append(f"{where}: an agent cannot delegate to itself")
            if agent.delegates_to and "task.delegate" not in agent.tools:
                p.append(f"{where}: agents with delegates_to need the 'task.delegate' tool")
            for server in agent.mcp_servers:
                if server not in mcp_servers:
                    p.append(f"{where}: mcp server {server!r} is not defined in mcp/servers.yaml")
            for pattern in agent.tools:
                if pattern.startswith("mcp."):
                    server = pattern.split(".")[1] if pattern.count(".") >= 1 else ""
                    if server not in agent.mcp_servers:
                        p.append(f"{where}: tool {pattern!r} needs {server!r} in mcp_servers")
                    continue
                matches = [t for t in cfg.tools if fnmatch.fnmatchcase(t, pattern)]
                if not matches:
                    p.append(f"{where}: tool {pattern!r} is not in tools/registry.yaml")
                for tool_id in matches:
                    decision = policy.check_tool(name, tool_id)
                    if not decision.allowed:
                        p.append(f"{where}: tool {tool_id!r} is listed but not granted: {decision.reason}")
            for mode, wanted in (("read", agent.memory_read), ("write", agent.memory_write)):
                for ns in wanted:
                    spec = namespaces.get(ns)
                    if spec is None:
                        p.append(f"{where}: memory namespace {ns!r} is not in memory/namespaces.yaml")
                    elif not ({"*", name} & set(spec.get(mode, []))):
                        p.append(f"{where}: memory/namespaces.yaml does not let {name!r} {mode} {ns!r}")
            if (agent.memory_read or agent.memory_write) and not (
                {"memory.recall", "memory.remember"} & set(agent.tools)
            ):
                self.warnings.append(f"{where}: memory is configured but no memory.* tool is listed")

        for wf_name, wf in cfg.workflows.items():
            where = f"workflows/{wf_name}.yaml"
            seen: set[str] = set()
            inputs = set((wf.get("inputs") or {}).keys())
            for step in wf.get("steps", []):
                sid = step["id"]
                if sid in seen:
                    p.append(f"{where}: duplicate step id {sid!r}")
                if "agent" in step and step["agent"] not in cfg.agents:
                    p.append(f"{where}: step {sid!r} uses unknown agent {step['agent']!r}")
                if "tool" in step:
                    if step["tool"] not in cfg.tools:
                        p.append(f"{where}: step {sid!r} uses unknown tool {step['tool']!r}")
                    elif not policy.check_tool(step["as_agent"], step["tool"]).allowed:
                        p.append(f"{where}: step {sid!r}: {step['as_agent']!r} is not granted {step['tool']!r}")
                    if step["as_agent"] not in cfg.agents:
                        p.append(f"{where}: step {sid!r} as_agent {step['as_agent']!r} is not an agent")
                self._check_refs(json.dumps(step), seen, inputs, f"{where}: step {sid!r}")
                seen.add(sid)
            if "output" in wf:
                self._check_refs(json.dumps(wf["output"]), seen, inputs, f"{where}: output")

        granted_ids = {
            t
            for section in ("grants", "denies")
            for rule in cfg.policies.get("tool_permissions", {}).get(section, [])
            for t in rule.get("tools", [])
        }
        for tool_id in sorted(granted_ids):
            if not tool_id.startswith("mcp.") and not any(fnmatch.fnmatchcase(t, tool_id) for t in cfg.tools):
                self.warnings.append(f"policies/tool-permissions.yaml: {tool_id!r} matches no registered tool")

        profiles = cfg.sandbox.get("profiles", {})
        tool_profiles = (cfg.sandbox.get("permissions") or {}).get("tool_profiles", {}) or {}
        for tool_id, profile in tool_profiles.items():
            if profile not in profiles:
                p.append(f"sandbox/permissions.yaml: {tool_id}: profile {profile!r} not in sandbox/profiles/")
        for spec in cfg.tools.values():
            if spec.sandbox == "required" and spec.id not in tool_profiles:
                p.append(f"tools/registry.yaml: {spec.id} requires a sandbox but has no tool_profiles entry")

        for name, server in mcp_servers.items():
            transport = server.get("transport")
            if transport == "stdio" and not server.get("command"):
                p.append(f"mcp/servers.yaml: {name}: stdio servers need a command")
            if transport == "http" and not server.get("url"):
                p.append(f"mcp/servers.yaml: {name}: http servers need a url")
            if transport not in ("stdio", "http"):
                p.append(f"mcp/servers.yaml: {name}: transport must be stdio or http")

        self._check_a2a(cfg)

    def _check_refs(self, text: str, earlier: set[str], inputs: set[str], where: str) -> None:
        for ref in TEMPLATE_REF.findall(text):
            head, _, rest = ref.partition(".")
            if head == "inputs" and rest not in inputs:
                self.problems.append(f"{where}: references undeclared input {rest!r}")
            elif head == "steps" and rest.split(".")[0] not in earlier:
                self.problems.append(f"{where}: references step {rest.split('.')[0]!r} before it runs")
            elif head not in ("inputs", "steps"):
                self.problems.append(f"{where}: unknown template reference {{{{ {ref} }}}}")

    def _check_a2a(self, cfg: PlatformConfig) -> None:
        card_path = self.root / "protocols" / "a2a" / "agent-card.json"
        routes_path = self.root / "protocols" / "a2a" / "routes.yaml"
        if card_path.exists():
            try:
                json.loads(card_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self.problems.append(f"protocols/a2a/agent-card.json: invalid JSON: {exc}")
        if routes_path.exists():
            routes = self.read_yaml(routes_path)
            for skill_id, target in (routes.get("routes") or {}).items():
                if "agent" in target and target["agent"] not in cfg.agents:
                    self.problems.append(f"protocols/a2a/routes.yaml: {skill_id}: unknown agent {target['agent']!r}")
                if "workflow" in target and target["workflow"] not in cfg.workflows:
                    self.problems.append(
                        f"protocols/a2a/routes.yaml: {skill_id}: unknown workflow {target['workflow']!r}"
                    )


def load_platform(root: str | Path = ".", *, check_imports: bool = True) -> PlatformConfig:
    """Load and cross-validate the whole platform rooted at `root` (the dir holding agent.yaml)."""
    root = Path(root).resolve()
    if not (root / "agent.yaml").exists():
        raise ConfigError([f"{root}: no agent.yaml - run commands from the platform root"])
    loader = _Loader(root, check_imports)
    manifest, instructions = loader.manifest()
    policies = loader.policies()
    tools = loader.tools()
    skills = loader.skills()
    models, routes, fallback_on, providers = loader.models()
    memory = loader.memory()
    agents = loader.agents()
    workflows = loader.workflows()
    cfg = PlatformConfig(
        root=root,
        manifest=manifest,
        instructions=instructions,
        agents=agents,
        skills=skills,
        tools=tools,
        workflows=workflows,
        models=models,
        routes=routes,
        fallback_on=fallback_on,
        providers=providers,
        policies=policies,
        memory=memory,
        # Optional sections: absent means "not used" (sandbox mode none, no MCP servers).
        observability=loader.plain_dir("observability"),
        sandbox={
            "permissions": loader.read_yaml(root / "sandbox" / "permissions.yaml", required=False),
            "profiles": loader.plain_dir("sandbox", "profiles"),
        },
        mcp={
            "servers": loader.read_yaml(root / "mcp" / "servers.yaml", required=False),
            "permissions": loader.read_yaml(root / "mcp" / "permissions.yaml", required=False),
            "adapters": loader.plain_dir("mcp", "adapters"),
        },
        schemas_dir=loader.schemas_dir,
    )
    if not loader.problems:
        loader.cross_check(cfg)
    if loader.problems:
        raise ConfigError(loader.problems)
    cfg.warnings = loader.warnings
    return cfg


def validate_platform(root: str | Path = ".") -> tuple[list[str], list[str]]:
    """Return (problems, warnings) without raising. Used by scripts/check.py and the CLI."""
    try:
        cfg = load_platform(root)
    except ConfigError as exc:
        return exc.problems, []
    return [], cfg.warnings
