"""M6-skills (CORE-CONTRACTS §6.3): the P2-S1 discovery face.

``SkillCatalog.list`` and its wire shapes live in
:mod:`opencode.skills.catalog`; activation (the audit command, the
instructions snapshot write, durable activation, recovery semantics) is the
P2-S2 slice and lands separately.
"""

from opencode.skills.catalog import (
    SKILLS_MANIFEST_NAME,
    SkillCatalog,
    SkillDiscoveryError,
    SkillEntry,
)

__all__ = [
    "SKILLS_MANIFEST_NAME",
    "SkillCatalog",
    "SkillDiscoveryError",
    "SkillEntry",
]
