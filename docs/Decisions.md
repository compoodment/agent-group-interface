# Decision records

AGI can record an answer to a question offered with explicit choices. The
question and the human's reply stay in chat. The decision view links to those
messages so that a qualifier or change of mind stays visible in the original
words. A decision entry records a choice. It does not execute commands or
change access, publish, install, or schedule work.

## Store contract

```python
append_message(sender, text, *, decision_prompt={"project": str, "choices": list[str]}) -> dict
list_decisions() -> list[dict]
```

An agent marks a question when sending it, using explicit choice codes like
`1a`, `1b`, `2a` and `2b`. It goes through the usual posting policy. The
number groups the alternatives for each question. A direct user reply may
give compact picks such as `1a 2b`, or one numbered line per choice. The
latter form lets the user qualify a choice in prose. An ambiguous or
unrecognised reply is marked for review. A clear subsequent reply changes
only the questions it answers.

Each projection contains `question_id`, `project`, `choices`, `selections`
and `review_answer_id`. Selections are keyed by the question number as text.
Each gives `choice`, `answer_id` and `qualified`. Use `answer_id` to open the
original reply when the choice needs its context. `review_answer_id` points to
the last ambiguous direct reply until a clear reply replaces it. List rows
in question order. Questions and answers persist in the chat journal.

## Command line

```text
agi decision ask --as <agent> --project <project> --choice 1a --choice 1b <question>
agi decision list
```

The question is a normal message. Reply to its message number in AGI to record
the choice automatically. Both commands return JSON.
