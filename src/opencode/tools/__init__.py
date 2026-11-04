"""Tool runtime public face (R2-S5 + R3-S2): registry + invoke + permission policy.

``opencode.tools.registry`` owns registration, per-session catalog snapshots
and the single :meth:`~opencode.tools.registry.ToolRegistry.invoke` entry;
``opencode.tools.invoker`` drives the §5.1 L257 event sequence and result
normalization; ``opencode.tools.policy`` is the §5.2 permission engine and
the §7 L316 approval channel (contracts-only imports, so re-exporting it
here adds no new dependency edge -- ``registry`` already imports it). Builtin
adapters are deliberately *not* re-exported here:
``opencode.tools.builtin.read`` consumes these runtime types, and an eager
import would make every ``opencode.tools`` import pull the filesystem
builtin (the runner registers the adapters it wants).
"""

from opencode.tools.invoker import (
    EventSink,
    ResolvedTool,
    ToolContext,
    ToolInvokeError,
    ToolLimits,
    ToolOutcome,
)
from opencode.tools.policy import (
    DEFAULT_APPROVAL_TIMEOUT_S,
    ApprovalCoordinator,
    ApprovalOutcome,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    ReplyResolution,
    SavedDecision,
    extract_resources,
)
from opencode.tools.registry import (
    RegistrationError,
    ToolAdapter,
    ToolCatalog,
    ToolRegistry,
)

__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT_S",
    "ApprovalCoordinator",
    "ApprovalOutcome",
    "EventSink",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionRule",
    "RegistrationError",
    "ReplyResolution",
    "ResolvedTool",
    "SavedDecision",
    "ToolAdapter",
    "ToolCatalog",
    "ToolContext",
    "ToolInvokeError",
    "ToolLimits",
    "ToolOutcome",
    "ToolRegistry",
    "extract_resources",
]
