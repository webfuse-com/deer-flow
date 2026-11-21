"""Run status, disconnect modes and host-owned evidence provenance."""

from enum import StrEnum

# Shared by Gateway admission and RunManager; values remain wire-format strings.
EVIDENCE_ORIGINS = frozenset({"interactive", "scheduled", "extension_evaluation", "unknown"})
# [argus patch #66] RunDeadlineMiddleware keeps the run start across goal
# continuations, so a time_capped run would re-enter an expired deadline.
GOAL_RUN_HARD_STOP_REASONS = frozenset({"token_capped", "loop_capped", "time_capped"})


class ThreadOperationKind(StrEnum):
    """Kind of operation holding exclusive admission for a thread."""

    run = "run"
    checkpoint_write = "checkpoint_write"
    artifact_write = "artifact_write"
    artifact_archive = "artifact_archive"
    branch = "branch"
    delete = "delete"


class RunStatus(StrEnum):
    """Lifecycle status of a single run."""

    pending = "pending"
    running = "running"
    success = "success"
    waiting_for_input = "waiting_for_input"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"


class DisconnectMode(StrEnum):
    """Behaviour when the SSE consumer disconnects."""

    cancel = "cancel"
    continue_ = "continue"
