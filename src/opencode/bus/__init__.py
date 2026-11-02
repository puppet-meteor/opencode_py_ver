"""Volatile bus package (R1-S5): live-only subscriptions, no persistence.

:mod:`opencode.bus.bus` implements the CORE-CONTRACTS §4 ``Bus.subscribe``
surface over bounded per-subscription queues; this package re-exports the
public surface. The S6 EventLog (durable backfill/replay) will join this
package as a sibling module -- the bus itself never persists anything.
"""

from opencode.bus.bus import DEFAULT_QUEUE_LIMIT, Event, SlowConsumerError, VolatileBus

__all__ = [
    "DEFAULT_QUEUE_LIMIT",
    "Event",
    "SlowConsumerError",
    "VolatileBus",
]
