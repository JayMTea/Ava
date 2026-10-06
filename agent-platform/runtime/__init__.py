"""Reference agent runtime. App code should import from here and nowhere deeper.

    from runtime import Platform
    result = Platform.load(".").run("your task")

See runtime/README.md for the module map and docs/architecture.md for the design.
"""

from .approval_manager import AllowListApprover, DenyAllApprover, InteractiveApprover
from .config import PlatformConfig, load_platform, validate_platform
from .orchestrator import Platform, WorkflowResult
from .providers import MockProvider
from .types import ConfigError, Message, PolicyViolation, ProviderError, RunResult, ToolError

__all__ = [
    "AllowListApprover",
    "ConfigError",
    "DenyAllApprover",
    "InteractiveApprover",
    "Message",
    "MockProvider",
    "Platform",
    "PlatformConfig",
    "PolicyViolation",
    "ProviderError",
    "RunResult",
    "ToolError",
    "WorkflowResult",
    "load_platform",
    "validate_platform",
]
