"""The P3-S1 orchestrator face: the jobs domain (PHASE3-ORCHESTRATOR §5.4/§6/§7/§8).

Slice one lands the job registry only: the pure fold state machine, the
jobs read-model projector, the service (submit/run/cancel), the fake
executor and the restart-recovery scan, over the jobs-domain writer in
:mod:`opencode.storage.jobs_store`. The engine, workers, brain-driver and
router are later slices -- this package carries no stubs for them.
"""

from opencode.orchestrator.jobs import (
    FakeJobExecutor,
    JobCheckpoint,
    JobCheckpointReader,
    JobExecutor,
    JobHandle,
    JobRecoveryAction,
    JobService,
    JobsProjector,
    JobState,
    JobStatus,
    JobTransitionError,
    fold_job_event,
)

__all__ = [
    "FakeJobExecutor",
    "JobCheckpoint",
    "JobCheckpointReader",
    "JobExecutor",
    "JobHandle",
    "JobRecoveryAction",
    "JobService",
    "JobState",
    "JobStatus",
    "JobTransitionError",
    "JobsProjector",
    "fold_job_event",
]
