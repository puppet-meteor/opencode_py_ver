"""Builtin tool adapters (R2-S5): the ``fs__read`` text file reader.

Adapters are registered by the runner (R2-S6) via
:meth:`opencode.tools.registry.ToolRegistry.register`; this package only
provides the implementations.
"""

from opencode.tools.builtin.read import (
    MAX_LINE_LENGTH,
    MAX_LINE_SUFFIX,
    MAX_READ_LINES,
    READ_DESCRIPTION,
    READ_INPUT_SCHEMA,
    READ_NAME,
    READ_NAMESPACE,
    READ_SPEC,
    READ_VERSION,
    FsReadAdapter,
)

__all__ = [
    "MAX_LINE_LENGTH",
    "MAX_LINE_SUFFIX",
    "MAX_READ_LINES",
    "READ_DESCRIPTION",
    "READ_INPUT_SCHEMA",
    "READ_NAME",
    "READ_NAMESPACE",
    "READ_SPEC",
    "READ_VERSION",
    "FsReadAdapter",
]
