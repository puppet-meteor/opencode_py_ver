"""Config package (R3-S6, CORE-CONTRACTS §6): the ConfigLoader face.

``opencode.config.loader`` owns layered discovery/merge, runtime resolution,
and the recovery face; this package init only re-exports the public names
(the contracts events module owns the v2 snapshot wire model itself).
"""

from opencode.config.loader import (
    HOST_ONLY_KEYS,
    MAX_INSTRUCTION_FILE_BYTES,
    MAX_TOTAL_INSTRUCTION_BYTES,
    CliOverrides,
    ConfigError,
    ConfigLayer,
    ConfigLoader,
    InstructionLimitError,
    LayeredConfig,
    RecoveryStoppedError,
    ResolvedRuntime,
)

__all__ = [
    "HOST_ONLY_KEYS",
    "MAX_INSTRUCTION_FILE_BYTES",
    "MAX_TOTAL_INSTRUCTION_BYTES",
    "CliOverrides",
    "ConfigError",
    "ConfigLayer",
    "ConfigLoader",
    "InstructionLimitError",
    "LayeredConfig",
    "RecoveryStoppedError",
    "ResolvedRuntime",
]
