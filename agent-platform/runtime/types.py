"""Provider-neutral data types shared by every runtime module.

Nothing in this file may import a model SDK. Providers translate to and from these
types at the edge (runtime/providers/), so the rest of the runtime never sees a
vendor-specific shape.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# Ordered from least to most dangerous. Approval policy is expressed in these terms.
SIDE_EFFECTS = ["none", "read_external", "local_write", "external_write", "destructive"]

# Ordered from least to most sensitive. A provider may only see data up to its ceiling.
DATA_CLASSES = ["public", "internal", "confidential", "restricted"]

# Normalized stop reasons every provider maps onto.
STOP_REASONS = ["end_turn", "tool_use", "max_tokens", "refusal", "other"]


class PlatformError(Exception):
    """Base class for every error the platform raises on purpose."""


class ConfigError(PlatformError):
    """One or more configuration files are invalid. Carries every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid platform configuration:\n  - " + "\n  - ".join(problems))


class PolicyViolation(PlatformError):
    """A hard policy (policies/*.yaml) refused an action."""


class ToolError(PlatformError):
    """Raised by a tool implementation. The message is returned to the model as an error result."""


class ProviderError(PlatformError):
    """A model provider call failed. `kind` drives the router's fallback decision."""

    def __init__(self, message: str, *, kind: str = "other", provider: str = ""):
        self.kind = kind
        self.provider = provider
        super().__init__(f"[{provider or 'provider'}:{kind}] {message}")


@dataclass
class ToolCall:
    id: str
    name: str  # internal tool id (e.g. "filesystem.read"), never the wire name, once inside the runtime
    arguments: dict[str, Any]
    parse_error: str | None = None  # set when the provider returned arguments that were not valid JSON


@dataclass
class Message:
    """One entry in an append-only conversation history."""

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    # Provider-native content blocks, replayed verbatim to the same provider. Current Claude
    # models bind thinking blocks to the conversation that produced them, so assistant turns
    # must be echoed back unchanged - never rebuilt from `content`.
    raw: list[dict[str, Any]] | None = None
    raw_provider: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        calls = [ToolCall(**c) for c in data.get("tool_calls", [])]
        return cls(**{**data, "tool_calls": calls})


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # one of STOP_REASONS
    usage: Usage
    model: str
    provider: str
    raw: list[dict[str, Any]] | None = None
    refusal_category: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    key: str  # registry key, e.g. "claude-opus" - what routes refer to
    provider: str  # provider name, e.g. "anthropic" - a file in models/providers/
    model: str  # the provider's model id, e.g. "claude-opus-5-5"
    max_output_tokens: int
    context_window: int | None = None
    supports_effort: bool = False
    default_effort: str | None = None
    options: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


def wire_name(tool_id: str) -> str:
    """Map an internal tool id to a name every provider accepts (^[a-zA-Z0-9_-]{1,64}$)."""
    name = tool_id.replace(".", "__")
    if len(name) > 64:
        digest = hashlib.sha1(tool_id.encode()).hexdigest()[:8]
        name = f"{name[:55]}_{digest}"
    return name


@dataclass
class ToolSpec:
    id: str
    description: str
    implementation: str | Callable[..., Any]  # "module:function", or a callable (MCP tools)
    side_effects: str
    input_schema: dict[str, Any]
    timeout_s: float = 30
    sandbox: str = "none"  # none | required
    max_output_chars: int = 20000
    source: str = "registry"  # registry | mcp:<server>

    @property
    def wire_name(self) -> str:
        return wire_name(self.id)


@dataclass
class AgentSpec:
    name: str
    description: str
    instructions: str
    route: str
    effort: str | None = None
    max_output_tokens: int | None = None
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    delegates_to: list[str] = field(default_factory=list)
    memory_read: list[str] = field(default_factory=list)
    memory_write: list[str] = field(default_factory=list)
    max_turns: int | None = None
    max_tool_calls: int | None = None
    data_classification: str = "internal"
    output: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None


@dataclass
class SkillSpec:
    name: str
    description: str
    body: str
    path: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    """Everything a tool implementation may touch. Tools get nothing else."""

    workspace: Path
    agent: str
    task_id: str
    session_id: str
    policy: Any  # runtime.policy_engine.PolicyEngine (Any avoids an import cycle)
    services: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolOutcome:
    tool_id: str
    content: str
    is_error: bool
    duration_ms: int = 0


@dataclass
class RunResult:
    task_id: str
    agent: str
    status: str  # completed | failed | cancelled
    output: str
    usage: Usage
    turns: int
    tool_calls: int
    error: str | None = None
    messages: list[Message] = field(default_factory=list)
