"""Tool runtime public face (R2-S5): registry + invoke pipeline.

``opencode.tools.registry`` owns registration, per-session catalog snapshots
and the single :meth:`~opencode.tools.registry.ToolRegistry.invoke` entry;
``opencode.tools.invoker`` drives the §5.1 L257 event sequence and result
normalization. Builtin adapters are deliberately *not* re-exported here:
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
from opencode.tools.registry import (
    RegistrationError,
    ToolAdapter,
    ToolCatalog,
    ToolRegistry,
)

__all__ = [
    "EventSink",
    "RegistrationError",
    "ResolvedTool",
    "ToolAdapter",
    "ToolCatalog",
    "ToolContext",
    "ToolInvokeError",
    "ToolLimits",
    "ToolOutcome",
    "ToolRegistry",
]
