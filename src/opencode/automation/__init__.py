"""Automation: scheduled unattended prompt actions (T-C3-003).

First-period slice of upstream 2.3.2 ``automation.ts`` (prompt actions,
at/every/cron triggers) -- see ``service`` for the semantics, ``cron`` for
the DST-safe next-slot calculator and ``scheduler`` for the leader loop.
"""

from opencode.automation.cron import CronSpec, InvalidCronError, next_after, parse
from opencode.automation.scheduler import AutomationScheduler
from opencode.automation.service import (
    AutomationNotFoundError,
    AutomationService,
    AutomationSessions,
    InvalidAutomationError,
    is_run_session,
    run_session_id,
)

__all__ = [
    "AutomationNotFoundError",
    "AutomationScheduler",
    "AutomationService",
    "AutomationSessions",
    "CronSpec",
    "InvalidAutomationError",
    "InvalidCronError",
    "is_run_session",
    "next_after",
    "parse",
    "run_session_id",
]
