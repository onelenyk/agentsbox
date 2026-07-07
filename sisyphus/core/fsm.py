"""Finite State Machine for step transitions."""
from .state import update_step_status, get_step

ALLOWED_TRANSITIONS = {
    "pending": ["running"],
    "running": ["completed", "failed", "timeout", "cancelled", "error"],
    "failed": ["running"],
    "timeout": ["running"],
    "error": ["running"],
    "cancelled": ["running"],
}

TERMINAL_STATUSES = {"completed", "failed", "timeout", "cancelled", "error"}


def transition_step(
    step_id: str,
    new_status: str,
    result_json: str = None,
    error: str = None,
) -> bool:
    """Transition a step to a new status. Returns True if allowed and executed."""
    step = get_step(step_id)
    if not step:
        return False

    current_status = step.get("status", "pending")

    if current_status in TERMINAL_STATUSES and new_status != "running":
        return False

    # Check if transition is allowed
    allowed = ALLOWED_TRANSITIONS.get(current_status, [])
    if new_status not in allowed:
        return False

    # Auto-fail if no result_json for completed status
    if new_status == "completed" and not result_json:
        new_status = "failed"
        error = error or "no_output"

    update_step_status(step_id, new_status, result_json, error)
    return True
