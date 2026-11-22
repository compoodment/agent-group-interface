"""Drive task reminders and timed to-do checks from the programs already listening."""

import time

from agi import store


def wait_limit(default):
    """Deliver due reminders and cap the caller's existing wait at the next deadline."""
    store.dispatch_todo_answers()
    store.dispatch_gate_reminders()
    store.dispatch_todo_waiters()
    now = time.time()
    deadline = store.next_reminder_deadline(now)
    if deadline is not None and deadline <= now:
        store.dispatch_due_reminders(now)
        now = time.time()
        deadline = store.next_reminder_deadline(now)
    return default if deadline is None else min(default, max(0.05, deadline - now))
