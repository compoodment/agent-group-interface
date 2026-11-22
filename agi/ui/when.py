"""When a to-do of yours is due, read from the end of what you typed (/todo add, #6054): "check the release tomorrow
9:00" → ("check the release", Mon 23 Nov 09:00). Only these forms count, so nothing is guessed:

    30m · 2h · 3d · in 2h           from now
    9:00 · at 17:30                 today, or tomorrow when that time has passed
    today 9 · tomorrow 9:00         that day, at that hour
    mon 9:00 · friday 17:00         the next such weekday (a week on when it's today and the time has passed)
    2026-11-25 · 2026-11-25 9:00    that date (9:00 when no time is given)

No time at the end: the to-do has no due time. The caller shows the time read before saving it.
"""

import re
from datetime import datetime, timedelta

NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TIME = r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?"
FORMS = (
    ("span", re.compile(r"(?:^|\s)(?:in\s+)?(\d+)\s*(m|min|h|d)\Z", re.I)),
    ("date", re.compile(r"(?:^|\s)(\d{4}-\d{2}-\d{2})(?:\s+" + TIME + r")?\Z", re.I)),
    ("day", re.compile(r"(?:^|\s)(today|tomorrow|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*)\s+" + TIME + r"\Z",
                       re.I)),
    ("clock", re.compile(r"(?:^|\s)(?:at\s+)?(\d{1,2}):(\d{2})\Z", re.I)),
)


def _at(day, hour, minute, zone=None):
    """That day at that time, in the offset local time has **on that date** (Codex, #6086: an April date typed in
    November must not borrow winter time), checked by reading the stored instant back."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Use a time like 9:00 or 17:30.")
    naive = day.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
    due = naive.replace(tzinfo=zone) if zone else naive.astimezone()
    back = datetime.fromtimestamp(due.timestamp(), zone or due.tzinfo) if zone else \
        datetime.fromtimestamp(due.timestamp()).astimezone()
    if (back.hour, back.minute) != (hour, minute):   # a time the clock skips that day (summer time starts)
        raise ValueError(f"{hour}:{minute:02d} doesn't exist that day (the clocks change); pick another time.")
    return due


def parse(text, now=None, zone=None):
    """(title, due datetime or None). A ValueError says what to type when a time is there but doesn't read.
    `zone`: a tzinfo for tests; by default the system's local time, with its summer and winter offsets."""
    now = now or datetime.now().astimezone()
    text = " ".join(text.split())
    for kind, form in FORMS:
        found = form.search(text)
        if not found:
            continue
        word = found[1].lower() if kind == "day" else ""
        names = [name for name in NAMES if name.startswith(word)] if word not in ("", "today", "tomorrow") else []
        if word not in ("", "today", "tomorrow") and not names:   # "sunset 9" isn't a day: keep looking
            continue
        title = text[:found.start()].strip()
        if not title:
            raise ValueError("Say what the to-do is, then when: /todo add check the release tomorrow 9:00")
        if kind == "span":
            amount, unit = int(found[1]), found[2].lower()
            step = {"m": timedelta(minutes=1), "min": timedelta(minutes=1), "h": timedelta(hours=1),
                    "d": timedelta(days=1)}[unit]
            try:
                return title, now + amount * step
            except OverflowError:
                raise ValueError("That's further ahead than a date can go; use something like 2h or 3d.") from None
        if kind == "date":
            try:
                day = datetime.strptime(found[1], "%Y-%m-%d")
            except ValueError:
                raise ValueError("Use a date like 2026-11-25.") from None
            return title, _at(day, int(found[2] or 9), int(found[3] or 0), zone)
        if kind == "clock":
            due = _at(now, int(found[1]), int(found[2]), zone)
            return title, due if due > now else _at(now + timedelta(days=1), int(found[1]), int(found[2]), zone)
        hour, minute = int(found[2]), int(found[3] or 0)
        if word in ("today", "tomorrow"):
            return title, _at(now + timedelta(days=word == "tomorrow"), hour, minute, zone)
        ahead = (NAMES.index(names[0]) - now.weekday()) % 7
        due = _at(now + timedelta(days=ahead), hour, minute, zone)
        return title, due if due > now else _at(now + timedelta(days=ahead + 7), hour, minute, zone)
    return text, None


def say(due):
    """The due time as the confirmation shows it: "Mon 23 Nov 09:00"."""
    return due.strftime("%a %-d %b %H:%M")
