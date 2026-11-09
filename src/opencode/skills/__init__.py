"""M6-skills (CORE-CONTRACTS §6.3): the P2-S1 discovery face + the P2-S2 activation gate.

``SkillCatalog.list`` and its wire shapes live in
:mod:`opencode.skills.catalog`; ``SkillCatalog.activate`` (the fixed
大小→路径→信任→hash check order and the §6.3 L332 audit receipt) is the
P2-S2 gate. Committing the ``py.skill.activated`` durable event, the
instructions snapshot write and the re-activation confirmation gate are
the §7 host command face (:class:`opencode.session.service.SessionService`).
"""

from opencode.skills.catalog import (
    SKILLS_MANIFEST_NAME,
    SkillActivationError,
    SkillActivationReceipt,
    SkillCatalog,
    SkillDiscoveryError,
    SkillEntry,
)

__all__ = [
    "SKILLS_MANIFEST_NAME",
    "SkillActivationError",
    "SkillActivationReceipt",
    "SkillCatalog",
    "SkillDiscoveryError",
    "SkillEntry",
]
