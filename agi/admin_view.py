"""Render the text-only admin participant and read marker from existing conversations."""

import json

from agi import store


_PARTICIPANTS = (store.USER, *store.AGENTS)


def _participant_message(message):
    return message["kind"] == "msg" and message["sender"] in _PARTICIPANTS


def _explicit_mention(message):
    return bool(store.mentioned(message["text"], (store.ADMIN,)))


def addressed_to_admin(message, *, lookup=None):
    """Apply the admin inbox scope using an optional local reply lookup."""
    if not _participant_message(message):
        return False
    if _explicit_mention(message):
        return True
    # A typed reply target was registered at send time. Older reply prefixes
    # did not give admin inbox access under the policy at the time.
    if "reply_to" not in message:
        return False
    target = store.reply_target(message, lookup=lookup)
    return target is not None and target["sender"] == store.ADMIN


def _cursor_path():
    return store.DATA_DIR / "admin-mentions" / "cursor.json"


def _cursor(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return store._integer(value["acked"], "admin acknowledgment")
    except FileNotFoundError:
        return 0
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise ValueError("Invalid admin acknowledgment") from error


def get_ack():
    """The admin cursor, read-only (the screen's read marks); None until the admin has acknowledged once."""
    with store._locked():
        path = _cursor_path()
        return _cursor(path) if path.exists() else None
