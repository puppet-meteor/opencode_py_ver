"""Session package (R1-S4 + R1-S7): the deterministic fold, the projector, the service.

:mod:`opencode.session.state` holds the pure fold (§3.3 -- no clock, no
randomness, no external calls); :mod:`opencode.session.projector` implements
the §3.2 ``Projector`` Protocol injected into
:class:`opencode.storage.SessionStore` (the single write path -- no global
bus registration). :mod:`opencode.session.service` is the §7
:class:`SessionService` application command surface (R1-S7) with its
``SessionRef``/``RunRef``/``RunStatus`` models. The R1-S8 rebuild will reuse
the fold and projector.
"""

from opencode.session.projector import SessionProjector
from opencode.session.service import (
    RunRef,
    RunStatus,
    RunStatusValue,
    SessionBusyError,
    SessionRef,
    SessionService,
)
from opencode.session.state import (
    IllegalTransitionError,
    SessionState,
    SessionStatus,
    apply,
)

__all__ = [
    "IllegalTransitionError",
    "RunRef",
    "RunStatus",
    "RunStatusValue",
    "SessionBusyError",
    "SessionProjector",
    "SessionRef",
    "SessionService",
    "SessionState",
    "SessionStatus",
    "apply",
]
