# AGI to-do

AGI to-dos hold work you want to come back to. Each item has a stable ID,
a project, an owner and a next action. Items can link to a chat message.
An optional deadline represents a real date. It sends the current owner
one notice when a running listener reaches that time. A prerequisite makes a to-do
ready after another to-do finishes, an exact commit reaches main, or a
named GitHub release is published. The list shows age and time since the
last change from the saved timestamps.

## Add a to-do for yourself

In the chat, type `/todo add check the release tomorrow 9:00` and press
Enter. You can also open `/todo` and press Ctrl+A to start the command.
Put a time at the end if you want a reminder. For example, use `30m`, `2h`,
`tomorrow 9:00` or `fri 17:00`. AGI shows the title and resolved local time
before saving. Press Enter to confirm or Esc to cancel.

Personal items use their own journal entry as their source. They appear in
your list, and a timed reminder is addressed to you.

## Follow a conversation or request

For a conversation, record the points left in the next action and set the next
speaker with `--next-actor`. Use `--due-at` with `--remind-next-actor` to send
the one-time check to whoever is next at that deadline. If the next actor is empty,
the check stays pending until one is chosen. The item keeps its owner.

To schedule a check for the current claimant of a request, add a to-do with
`--source <request-id> --follow-request`. Its owner follows the request when
another agent claims it. A timed or exact-main check waits while the request
is unclaimed. Completing the request closes its following to-dos. Close a
following to-do early to cancel its check. To defer a request's remaining work,
use an independent to-do.

## Store contract

```python
add_todo(actor, source_id, project, title, owner, next_action, *,
         next_actor=None, due_at=None, reference=None, wait_for=None, request_id=None,
         remind_next_actor=False, follow_request=False) -> dict
add_personal_todo(title, *, due_at=None, request_id=None) -> dict
update_todo(actor, todo_id, revision, *, request_id=None, **changes) -> dict
complete_todo(actor, todo_id, revision, result, *, request_id=None) -> dict
set_next_todo(actor, todo_id=None, *, owner=None, request_id=None) -> dict | None
list_todos(*, include_done=False) -> list[dict]
```

`actor`, `owner` and any `next_actor` are `user`, `claude` or `codex`.
The owner keeps the item moving. Set `next_actor` when someone can take
the next step now, and leave it empty while a prerequisite is pending.
The rail's count for the user is the number of open items with
`next_actor == "user"`. Keep the active request rail and open the full
commitment list through `/todo`.

When the user replies directly to the source of an open to-do that waits on them,
the owner gets a cue to check the reply. The owner still chooses who acts next.
A newer reply can replace a queued cue while delivery is paused. Editing or
closing the item clears a cue for its earlier revision.

Each row includes `id`, `source_id`, `project`, `title`, `owner`,
`next_action`, `next_actor`, `due_at`, `reference`, `result`, `created_at`,
`updated_at`, `revision` and `is_next`. `source_id` identifies the linked chat
message. It is `None` for personal items.
`created_at` and `updated_at` are ISO 8601 strings with a timezone offset.
`due_at` is Unix seconds or `None`. `reference` links to a checklist, report
or other supporting material. `result` is `None` while the item is open.
The completion result should name the outcome and its evidence. Finished
items remain readable with `include_done=True`.

A waiting row has `wait_for`. The store accepts `todo:<id>`,
`gate:<project-id>:<full-commit>` or `release:<project-id>:<tag>`.
For an Artlab main check, opt in by setting `ARTLAB_GATE_REPO` to the local
Artlab checkout. AGI main checks use the AGI checkout by default. Set
`AGI_GATE_REPO` to use another checkout. Setting a
prerequisite clears `next_actor`. After it is satisfied, a single durable
record clears the wait, sets `next_actor` to the current owner and sends
the owner a ready notice. Listeners verify the row's revision before
delivering it. Completion or replacement can supersede a pending wait.

Completion and main checks read local state. For an explicit release wait,
AGI checks the exact tag through GitHub's public release endpoint for
Artlab or AGI. The request contains the public repository and tag,
and sends no chat or to-do text, credentials or analytics. A draft, an
unpublished tag or an unavailable check leaves the prerequisite pending.
An existing listener checks again as it runs. Results are cached for up
to two minutes per listener.

## Command line

Use one of these options on `todo add` or `todo update` to set a wait.

- `--after-todo <id>` waits for a to-do's completed result.
- `--after-main <project> <full-sha>` waits for local main to contain that commit.
- `--after-release <project> <tag>` waits for the exact published release.
- `todo update --clear-wait` removes the prerequisite. Include
  `--next-actor <name>` if someone can act immediately.

```text
agi todo list [--all]
agi todo add-personal "Check the release"
agi todo add --as <actor> --source <message-id> --project <project> \
  --title <title> --owner <owner> --next-action <next-action>
agi todo update --as <actor> <id> --revision <revision> --next-actor <actor>
agi todo done --as <actor> <id> --revision <revision> --result <outcome>
agi todo next --as <actor> <id>
agi todo next --as <actor> --clear [--owner <owner>]
```

Commands return JSON. For an item waiting on a prerequisite, omit
`--next-actor` on add or use `--clear-next-actor` on update. Run
`agi todo add --help` or `agi todo update --help` for the optional deadline,
reference and retry arguments.

## Choosing what is next

Each owner can mark one open item as next. The user can choose for any
owner. Selecting another item replaces the earlier selection in one write.
Closing an item or changing its owner clears its marker. The marker leaves
the item's next actor and trigger as recorded. For the store call, omit
the to-do ID to clear the marker and specify an owner to clear for someone else.

## Edits and durability

The owner or the user may edit or complete an item. Updates accept any
of `project`, `title`, `owner`, `next_action`, `next_actor`, `due_at` and
`reference`. Pass the revision you read so a concurrent change produces
an error and you can reload. A mutation's optional request ID lets a caller
retry the exact operation after an interrupted response.

Mutations use the existing chat lock and journal. The timed notice uses
the existing reminder scheduler and respects the posting policy. Changing
the deadline schedules the new time. Clearing the deadline or completing the
item cancels delivery. A timed notice leaves the item and its prerequisites
as recorded. See [task reminders](CONTRACT.md#task-reminders) for which
programs drive delivery.
