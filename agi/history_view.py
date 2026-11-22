"""Read-only search and a catch-up view assembled from the conversation journal."""

from bisect import bisect_right
import re

from agi import store


SENDER_FILTER = re.compile(r"(?<!\S)from:(\S+)")


def find(query, *, before=None, limit=30):
    """Return the newest matching messages with an optional sender filter."""
    if not isinstance(query, str):
        raise ValueError("Enter words to find or add a from: name.")
    matches = list(SENDER_FILTER.finditer(query))
    if len(matches) > 1:
        raise ValueError("Use one from: filter.")
    sender = None
    if matches:
        match = matches[0]
        sender = match[1].casefold()
        if sender not in store.SENDERS:
            raise ValueError("Use from:claude, from:codex, from:user, from:admin or from:system.")
        query = " ".join(part.strip() for part in (query[:match.start()], query[match.end():]) if part.strip())
    query = query.strip().casefold()
    if not query and sender is None:
        raise ValueError("Enter words to find or add a from: name.")
    store._integer(limit, "limit")
    if before is not None:
        store._integer(before, "before")

    with store._locked():
        rows = store._records()
        stop = len(rows) if before is None else bisect_right(rows, before - 1, key=lambda row: row["id"])
        found = []
        for row in reversed(rows[:stop]):
            if row["kind"] not in ("msg", "system") or (sender is not None and row["sender"] != sender):
                continue
            if query in row["text"].casefold():
                if len(found) == limit:
                    return {"messages": found, "more": True}
                found.append(store._copy_message(row))
        return {"messages": found, "more": False}


def catch_up(after):
    """Show items that can be established from records without moving a receipt."""
    store._integer(after, "read position")
    with store._locked():
        rows = store._records()
        summary = store._summary(rows)
        start = bisect_right(rows, after, key=lambda row: row["id"])
        unread = rows[start:]
        waiting, finished, active = [], [], []
        represented = set()

        def add(group, kind, ident, text, *, record_id=None):
            group.append({"kind": kind, "id": ident, "text": text})
            if record_id is not None:
                represented.add(record_id)

        for decision in summary["decisions"].values():
            missing = {choice[:-1] for choice in decision["choices"]} - decision["selections"].keys()
            if missing or decision["review_answer_id"] is not None:
                number = decision["question_id"]
                row = store._record_at(rows, number)
                add(waiting, "decision", number, row["text"], record_id=number)

        for item in summary["todos"].values():
            if item["result"] is None and item["next_actor"] == store.USER:
                add(waiting, "todo", item["id"], item["next_action"], record_id=item["revision"])

        for row in unread:
            if row["id"] not in represented and store.addressed_to(
                    row, store.USER, lookup=lambda ident: store._record_at(rows, ident)):
                add(waiting, "message", row["id"], row["text"], record_id=row["id"])

        for key, event in summary["tasks"].items():
            number = int(key[5:])
            if event.get("done") and event["id"] > after:
                add(finished, "task", number, event.get("result") or f"Completed request #{number}",
                    record_id=event["id"])
                continue
            owner = summary["claims"].get(key)
            if owner is not None:
                action = summary["task_actions"].get(key, {}).get("doing")
                scope = store._record_at(rows, number)
                if scope is not None:
                    add(active, "task", number, f"{owner} · {action or scope['text']}", record_id=event["id"])

        for group in (waiting, finished, active):
            group.sort(key=lambda item: item["id"], reverse=True)
        return {"waiting": waiting, "finished": finished, "active": active,
                "other_unread": sum(row["id"] not in represented for row in unread)}
