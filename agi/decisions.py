"""Recognise explicit choices on a marked question and project their source links."""

import re
from collections import Counter


_CODE = r"[1-9][0-9]{0,2}[a-z]"
_COMPACT = re.compile(rf"{_CODE}(?:[\s,]+{_CODE})*[.!]?", re.ASCII)
_NUMBERED = re.compile(r"([1-9][0-9]{0,2})[.)]\s*([a-z])\b(.*)", re.ASCII)
_AMBIGUOUS = re.compile(r"^(?:or\b|maybe\b|probably\b|no\b|not\b|[/+?])", re.IGNORECASE)


def validate_prompt(prompt):
    if not isinstance(prompt, dict) or set(prompt) != {"project", "choices"}:
        raise ValueError("A decision needs a project and choice codes")
    project, choices = prompt["project"], prompt["choices"]
    if not isinstance(project, str) or not project.strip() or len(project) > 80:
        raise ValueError("Decision project must contain 1–80 characters")
    if (not isinstance(choices, list) or not 2 <= len(choices) <= 30
            or any(not isinstance(code, str) or not re.fullmatch(_CODE, code) for code in choices)
            or len(set(choices)) != len(choices)):
        raise ValueError("Use 2–30 distinct choice codes such as 1a and 1b")
    if min(Counter(code[:-1] for code in choices).values()) < 2:
        raise ValueError("Each numbered decision needs at least two choices")


def parse_choices(body, allowed):
    """Return explicit selections, or None when the answer needs review."""
    text = body.strip()
    if _COMPACT.fullmatch(text):
        picks = [(code, False) for code in re.findall(_CODE, text)]
    else:
        picks = []
        for line in text.splitlines():
            if not line.strip():
                continue
            match = _NUMBERED.fullmatch(line.strip())
            if not match:
                return None
            number, letter, note = match.groups()
            note = note.strip()
            if _AMBIGUOUS.match(note) or re.search(r"\bor\s+[a-z]\b", note, re.IGNORECASE):
                return None
            picks.append((number + letter, note not in ("", ".", "!")))
    numbers = {code[:-1] for code, _ in picks}
    if not picks or len(numbers) != len(picks) or any(code not in allowed for code, _ in picks):
        return None
    return picks


def apply_reply(row, body, reply_id):
    choices = parse_choices(body, row["choices"])
    if choices is None:
        return dict(row, review_answer_id=reply_id)
    selected = dict(row["selections"])
    for code, qualified in choices:
        selected[code[:-1]] = {"choice": code, "answer_id": reply_id, "qualified": qualified}
    return dict(row, selections=selected, review_answer_id=None)
