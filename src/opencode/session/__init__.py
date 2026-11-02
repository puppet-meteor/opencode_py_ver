"""Session package (R1-S4): the deterministic state fold + the real projector.

:mod:`opencode.session.state` holds the pure fold (§3.3 -- no clock, no
randomness, no external calls); :mod:`opencode.session.projector` implements
the §3.2 ``Projector`` Protocol injected into
:class:`opencode.storage.SessionStore` (the single write path -- no global
bus registration). The R1-S8 rebuild will reuse both.
"""

from opencode.session.projector import SessionProjector
from opencode.session.state import (
    IllegalTransitionError,
    SessionState,
    SessionStatus,
    apply,
)

__all__ = [
    "IllegalTransitionError",
    "SessionProjector",
    "SessionState",
    "SessionStatus",
    "apply",
]
