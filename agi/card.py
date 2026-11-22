"""One screen of current requests, open to-dos and unread replies for an agent.

    python3 -P -m agi.card --as claude

Read-only and suitable for a local session-start hook.
"""
import argparse
import datetime
import re
import sys
import textwrap

from agi import store

REMINDERS = (
    "Claim before you start; answer as a reply (↪ #N); close with the outcome, the check and the skills.",
    "A promise for later is a to-do first (agi todo add), cited by its T number.",
    "Check before you cite: look the number up now. A fix in a running process isn't live until it restarts.",
    "No time guesses; say what's left in steps. Before closing, walk every numbered point.",
    "Load the skill that fits before starting, using the workspace's skill index.",
)
UNREAD_SHOWN = 8


def _when(seconds):
    return datetime.datetime.fromtimestamp(seconds).strftime("%a %d %b %H:%M")


def _first_line(text, width=80):
    line = " ".join(text.split())
    return line if len(line) <= width else line[:width - 1] + "…"


TODOS_SHOWN = 3                             # to-dos where this agent is up, with their next action
DECISIONS_SHOWN = 3
NEXT_ACTION_LINES = 4                       # a next action in full, wrapped: its finishing condition must survive
REF = re.compile(r"(?<![\w#])#([1-9][0-9]{0,6})\b")


def card(agent):
    """Summarize live AGI records for an agent picking up a conversation."""
    other = next(name for name in store.AGENTS if name != agent)
    now = datetime.datetime.now()
    lines = [f"AGI · where {agent} stands · {now.strftime('%a %d %b %H:%M')}",
             "AGI reference. Continue your current work and consult these records as needed."]
    cache = {}

    def message(message_id):
        if message_id not in cache:
            found = store.read_messages(message_id - 1, limit=1) if message_id > 0 else []
            cache[message_id] = found[0] if found and found[0]["id"] == message_id else None
        return cache[message_id]

    # 1. what's new since I last read
    ack = store.get_ack(agent) or 0
    since = store.read_messages(ack)
    cache.update((item["id"], item) for item in since)
    unread = [item for item in since if item["sender"] != agent and store.addressed_to(item, agent, lookup=message)]
    lines.append(f"New for you since #{ack} ({len(unread)}):" if unread else f"New for you since #{ack}: nothing")
    for item in unread[-UNREAD_SHOWN:]:
        lines.append(f"  #{item['id']} {item['sender']}: {_first_line(item['text'], 80)}")
    if len(unread) > UNREAD_SHOWN:
        lines.append(f"  … and {len(unread) - UNREAD_SHOWN} earlier")

    # 2. what's Next, how to finish it, and the direction that governs it
    end_of_today = now.replace(hour=23, minute=59, second=59).timestamp()
    todos = [item for item in store.list_todos() if item["next_actor"] == agent]
    todos.sort(key=lambda item: (not item.get("is_next"),                      # AGI's Next first
                                 not (item["due_at"] and item["due_at"] <= end_of_today),   # then due today or late
                                 -item["id"]))                                  # then newest; a future date doesn't lead
    lines.append(f"Up to you next ({len(todos)} to-dos; the first {min(len(todos), TODOS_SHOWN)}):" if todos
                 else "Up to you next: no to-dos")
    for item in todos[:TODOS_SHOWN]:
        flags = (" · Next" if item.get("is_next") else "") + (f" · due {_when(item['due_at'])}" if item["due_at"] else "")
        lines.append(f"  T{item['id']} {_first_line(item['title'], 60)}{flags}")
        wrapped = textwrap.wrap(" ".join(item["next_action"].split()), 92)
        shown = wrapped[:NEXT_ACTION_LINES]
        if len(wrapped) > NEXT_ACTION_LINES:
            shown[-1] = shown[-1][:88] + " …"
        lines += [("    next: " if n == 0 else "          ") + text for n, text in enumerate(shown)]
        cited = sorted({int(number) for number in REF.findall(item["next_action"])}, reverse=True)
        governing = [found for found in map(message, cited) if found and found["sender"] == store.USER][:2]
        source = message(item["source_id"]) if item["source_id"] is not None else None
        if not governing and source and source["sender"] == store.USER:
            governing = [source]
        for found in governing:
            words = re.sub(r"^↪ #[0-9]+\s*", "", found["text"])   # the reply arrow is noise here
            lines.append(f"    per #{found['id']}: {_first_line(words, 80)}")

    # 3. who owes what
    tasks = store.list_tasks()
    for owner, heading in ((agent, "You hold"), (other, f"{other} holds")):
        held = [task for task in tasks if task["state"] == "claimed" and task["owner"] == owner]
        lines.append(f"{heading}:" if held else f"{heading}: nothing")
        for task in held:
            doing = f" · doing: {_first_line(task['doing'], 40)}" if task.get("doing") else ""
            lines.append(f"  #{task['id']} {_first_line(task['text'], 60)}{doing}")
    theirs = next((item for item in reversed(store.read_tail(200)) if item["sender"] == other and item["kind"] == "msg"),
                  None)
    if theirs:
        ago = max(0, int((now - datetime.datetime.fromisoformat(theirs["ts"]).replace(tzinfo=None)).total_seconds() // 60))
        words = re.sub(r"^↪ #[0-9]+\s*", "", theirs["text"])
        lines.append(f"  {other}'s last update: #{theirs['id']}, {ago} min ago: {_first_line(words, 66)}")
    their_next = next((item for item in store.list_todos() if item["next_actor"] == other and item.get("is_next")), None)
    if their_next:
        lines.append(f"  {other}'s Next: T{their_next['id']} {_first_line(their_next['title'], 60)}")
    handed = [task for task in tasks if task["state"] == "released" and task.get("handoff_to") == agent]
    for task in handed:
        lines.append(f"  handed to you: #{task['id']} {_first_line(task['text'], 60)}")

    # 4. the decisions that govern
    decisions = [record for record in store.list_decisions() if record["selections"]]
    decisions.sort(key=lambda record: max(pick["answer_id"] for pick in record["selections"].values()))
    if decisions:
        lines.append("User's latest decisions:")
        for record in decisions[-DECISIONS_SHOWN:]:
            picks = " ".join(pick["choice"] for _, pick in sorted(record["selections"].items(), key=lambda p: int(p[0])))
            answered = max(pick["answer_id"] for pick in record["selections"].values())
            question = message(record["question_id"])
            about = f" · {_first_line(question['text'], 60)}" if question else ""
            lines.append(f"  #{record['question_id']} {record['project']}: {picks} (in #{answered}){about}")

    lines.append("Rules that keep slipping:")
    lines += [f"  - {rule}" for rule in REMINDERS]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m agi.card", description=__doc__.splitlines()[0])
    parser.add_argument("--as", dest="agent", required=True, choices=store.AGENTS)
    agent = parser.parse_args(argv).agent
    try:
        print(card(agent))
    except (OSError, ValueError) as error:       # a hook must never stop a session from starting
        print(f"AGI card unavailable: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
