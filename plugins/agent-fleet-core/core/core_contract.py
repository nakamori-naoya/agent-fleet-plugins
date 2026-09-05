"""Fleet Core vocabulary and clock, independent of any runtime adapter."""
from datetime import datetime, timezone

COMMAND_TYPES = frozenset(
    {
        "fleet.provision",
        "context.sync",
        "task.assign",
        "message.send",
        "task.report",
        "fleet.reconcile",
    }
)
EVENT_SOURCE_TYPES = frozenset({"fleet", "member", "task", "runtime", "view", "provider", "system"})
TASK_TRANSITIONS = {
    "pending": frozenset({"assigned"}),
    "assigned": frozenset({"running"}),
    "running": frozenset({"blocked", "reported", "failed"}),
    "blocked": frozenset({"running"}),
    "reported": frozenset({"running", "accepted"}),
    "accepted": frozenset(),
    "failed": frozenset(),
}


class FleetError(RuntimeError):
    """A user-actionable control-plane error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


