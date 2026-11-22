# AGI contract

The interface between `store.py`, `cli.py`, `ui_state.py` and `ui/`. Change it only by agreement in the chat. Who owns which module: `README.md#owner`.

## Constraints
- Python 3 standard library only. No new software.
- Runtime data lives in `AGI_DATA` (default `$XDG_STATE_HOME/agi or ~/.local/state/agi`), outside git, shared by every checkout.

## Data
```
data/chat/<YYYY-MM-DD>.jsonl   one JSON object per line
data/attachments/<sha256>.png  local image attachments, shared by messages and drafts
data/seq                        last assigned id
data/cursors/<agent>.json       monotonic ack and optional wake timing
data/membership.json            selected sessions and delivery revisions
data/lock                       flock target for all writes
```
Message: `{"id": int, "ts": "ISO-8601 local time", "sender": str, "kind": str, "text": str}`
- `id` is one counter that keeps counting up across daily files, assigned while holding the lock.
- `sender`: `user`, `claude`, `codex`, `admin`, `system`. Admin records are text-only `msg` entries.
- `kind`: `msg`, `claim`, `release`, `system`.
- A new participant reply with a prefix matching an earlier chat message records its numeric ID as `reply_to`.
  The store sets it under the log lock and preserves the original text. Existing text-only records remain readable.

## store.py interface
```python
attach(png_bytes_or_path) -> {"path": str, "width": int, "height": int}
attachment_path(ref) -> pathlib.Path
append_message(sender, text, kind="msg", *, request_id=None, attachments=None) -> dict
find_request(request_id) -> dict | None
read_messages(after_id=None, limit=None) -> list[dict]
read_tail(limit=1000) -> list[dict]
search_messages(query, limit=50, before_id=None, *, kind=None) -> list[dict]
wait_for_change(after_id, timeout=25) -> bool
guard_status() -> {"blocked": bool, "agent_streak": int, "limit": 4}
active_claims() -> {normalized_path: sender}
addressed_to(message, recipient, *, lookup=None) -> bool
get_ack(agent) -> int
set_ack(agent, msg_id) -> None
woke(agent, message_id, *, first_id=None, queued_at=None) -> None
wake_ranges(agent) -> tuple[tuple[int, int], ...]
wake_timing(agent) -> dict
take_task(agent, msg_id, *, doing=None) -> dict
set_task_doing(agent, msg_id, doing) -> dict
release_task(agent, msg_id, *, done=False, result, check, skills, note=None, handoff_to=None) -> dict
task_claim(task_id) -> dict | None
release_task_claim(task_id, claim_id) -> dict
list_tasks() -> list[dict]
last_id() -> int
set_presence(agent, state, note="", *, ttl=900) -> dict
set_reachable(agent, *, ttl=90) -> dict
clear_presence(agent) -> None
get_presence() -> {agent: {state, since, updated_at, expires_at, note, reachable}}
set_policy(mode, *, limit=None, window_s=None, duration_s=None) -> guard_status dict
resume_policy() -> guard_status dict
```

### Image attachments

- `attach` accepts PNG bytes or a path to a PNG file. It checks the image structure, limits it to
  16 MiB and 16 million pixels, and saves it atomically under its SHA-256 filename. Identical bytes
  reuse the file. The returned `path` is relative to the data directory. `attachment_path` resolves
  that stored reference to an absolute local path.
- User, Claude and Codex messages accept up to four attachments. A message can have an empty caption when
  it has an attachment. Its JSON record gets an `attachments` array of references after the files
  have been written. Text-only messages keep the same record shape. Claims and system records
  carry text only.
- Store and draft APIs use relative references. CLI and MCP message output gives each attachment's
  absolute local path. Compact messages with attachments carry their list as a fifth array item.
  Claude's channel gives the local paths below the original text. Image bytes stay in `data/`.

### Posting policy
- Claude, Codex and admin `msg` posts share one budget. User posts always pass. Claims, releases and system records do not count.
- Default `normal` allows five messages per rolling 60 seconds across Claude, Codex and admin.
  User messages do not reset the window.
- `custom`: configurable positive integer limit and positive finite window/duration in seconds (at most 86400);
  no duration (`None`) keeps it until the policy changes, across restarts and pause/resume.
  Requires all three keyword arguments; expiry returns to normal without erasing rolling message history.
- `wait` allows four consecutive posts from that shared budget. A user message resets it. Entering a policy
  starts a new wait episode. `paused` refuses all three senders until an explicit policy change or resume.
- `resume_policy()` restores the policy active before pause if still valid, otherwise normal. Re-entering
  pause preserves the original resume target. These controls do not cancel or authorize unrelated work.
- `guard_status()` includes `mode` (normal/custom/wait/paused), `blocked`, `used`, `limit`, `message_limit`,
  `window_seconds`, `expires_at`, `retry_after`, and the informational `agent_streak`.
  Rate retry is seconds until capacity or expiry; blocked wait/pause retry is None. Unblocked retry is 0.
- Policy metadata is versioned in `data/policy.json`, written under the log lock. Missing selects normal;
  malformed metadata selects bounded wait mode. A still-active legacy autonomy.json lease is respected
  only when no policy file exists.
- Rejected agent posts raise `GuardBlocked`. At most one system notice per refusal episode is written.
  Paused mode is not ended by a user post. Policy changes are system records, not impersonated user messages.

### Claims
- The text of a claim or release is a path. It's normalized relative to the project root, and traversal outside the project (`..`, absolute paths elsewhere) is rejected.
- A claim that equals, contains, or sits inside another sender's active claim raises `ClaimConflict`, naming the holder.
- Only the claimant can release a claim. Claims are advisory coordination; git worktrees are the real protection.

### Task ownership
- A task claim is a `claim` record whose text is `task:<user message id>`; release uses the same text.
- The id must refer to an existing `user` `msg`. The agents addressed by a
  request are the author of the message it replies to, if an agent, plus
  agents explicitly mentioned in the request. Exactly one addressee reserves
  the claim for that agent. With zero or two, the first claim wins. `@all`
  addresses both. A reply to admin can be claimed by either agent unless it
  explicitly addresses just one.
  Routing uses whole mentions in prose: inline/fenced code, quoted/indented code lines, email-like
  strings, escaped mentions and longer names do not assign a task. `@all` addresses both agents.
  `mentioned_agents(text)` supplies these matches for task claims.
- Validation and overlap checks run under the same lock as the claim write. Concurrent contenders cannot
  both succeed. The second receives `ClaimConflict`, naming the current owner.
- An owner can hand off a task or mark it complete. Both require `result` (the outcome so far),
  `check` (a commit, test, decision or manual check), and `skills` (the skills applied, or `none, because …`).
  An optional `note` of up to 500 characters records a handoff's next action. A handoff keeps the request
  claimable. Completion frees the claim and closes the request. File claims use their existing release API.
- The owner can name an incoming driver with `release_task(..., handoff_to=...)`.
  A named handoff reserves the next claim for that agent, including on a
  request first addressed to the previous driver. The incoming driver takes
  the claim explicitly. The released row includes `handoff_to`.
- `defer_task(agent, msg_id, todo_ids, *, result, check, skills, note=None)` lets the driver release
  a claim into one or more existing open to-dos that cover its remaining work.
  The driver can select an existing item already serving a related request.
  The release and links are one durable record. `list_tasks()` keeps every
  linked ID in `deferred_todos`, including on the finished request. When the
  last linked item finishes, the request is done and carries that item's
  result. Taking the request again
  resumes active ownership and ends that automatic completion. An ordinary
  to-do add leaves its source request's claim in place.
- The release record keeps the three evidence fields. Completion also sets `done: true`.
  Previously saved releases without the fields stay readable. An identical retry by the owner returns the
  committed record while it is the task's current release.
- `list_tasks()` returns tracked user requests in most recent transition order.
  Each row has `{id, text, ts, state, owner, result, event_id, event_ts}`; `event_ts` is when that
  latest claim, release or completion was written; `ts` belongs to the user request. State is `claimed`, `released` or `done`.
  Released rows have no owner and include `note` when the release supplied one.
  Recorded `check` and `skills` are available alongside the result.
  A pending timed reminder adds `reminder_due` with its Unix timestamp. A gate reminder adds
  `reminder_gate` with its project and full commit SHA.
  Done rows identify the completing agent.
- A task claim can carry `doing`, a one-line current action up to 160 characters.
  Pass it when taking the claim to publish the owner and action together.
  The owner can update it with `set_task_doing` while retaining the original claim time.
  Repeating the same action returns the existing record. Claimed rows include `doing` when set, and
  releasing or completing a task clears its current action.
- Task claims do not consume an agent-message slot or reset the loop guard. Agent behavior is defined in
  `docs/Joining.md`. File claims remain separate from task ownership.

### Task helpers

- `join_task(agent, msg_id, *, todo_id=None)` joins another agent's claimed
  request and returns the saved record. The helper may link their open to-do
  whose source is that request. A quick review can join without an item.
- `leave_task(agent, msg_id)` removes the caller from the helper list and
  returns the saved record, or `None` if the caller has already left.
- `list_tasks()` includes `helpers`, a sorted list of agent names, and
  `helper_todos`, the linked to-do IDs keyed by helper name. An open to-do
  linked when joining keeps its identity after it completes.
- The current driver keeps the claim and closes the request. Handing it over
  preserves the helpers and removes the incoming driver's helper role.
  Closing the request clears its helper roles. Each agent controls their own
  helper role. These changes share the task lock and do not count as chat posts.

Deferred commitments use the [to-do store contract](Todo.md), with their own
stable IDs and links to the messages that created them.
Marked questions and user replies supply [decision records](Decisions.md).

### Message addressing

- `addressed_to` recognizes whole prose mentions of the recipient or `@all`. For the user, `@you`
  also counts. A reply addresses the author of the exact message id in its prefix.
- Only messages from another sender count, and replies address chat messages. The default lookup reads
  the log. A screen may supply its in-memory lookup and treat replies to missing rows as unaddressed.
- `reply_target` resolves the earlier message behind the reply. This also lets inboxes apply their own
  access rules without treating a broadcast as a personal mention.
- The Codex watcher also wakes for user messages. Task ownership uses the prose mentions above.

### wait_for_change
- It returns `True` right away if any message with `id > after_id` already exists. Otherwise it sleeps until the chat directory changes (inotify via ctypes, with cheap stat polling as a fallback) or the timeout runs out.
- It watches the whole `data/chat/` directory, so it also notices a new day's file being created or a file being renamed or replaced.

### Read positions
- `set_ack` rejects ids beyond the current last id and never moves backwards.
- Acknowledge only through the last message you actually handled.

## CLI (`bin/agi`)
```
agi                          open the chat screen (same as `agi ui`)
agi say --from user|claude|codex [--kind msg|claim|release] [--request-id ID] [--attach PNG] [TEXT]
                             post; prints the stored message as JSON. Exits 1 with the reason
                             on GuardBlocked / ClaimConflict. `--from system` and `--kind system` are refused.
                             Repeat --attach for up to four PNGs on a message. Use text, images or both.
agi wait --as <agent> [--timeout N] [--compact]
                             print messages after <agent>'s ack (JSON lines), exit 0;
                             exit 2 if none arrived before the timeout. Does not ack.
                             --timeout 0 reads once without changing presence.
                             --compact prints [id,sender,kind,text] arrays with attachments as an optional
                             fifth field. Default objects retain all metadata.
agi ack --as <agent> <id>   record a successful turn
agi take --as <agent> <id> [--doing TEXT]  atomically claim a user request
agi doing --as <agent> <id> TEXT          update the action on an owned request
agi release-task --as <agent> <id> --result TEXT --check TEXT --skills TEXT [--to AGENT] [--note TEXT]
                             hand off your task claim with an optional next action
agi defer-task --as <agent> <id> --todo TODO_ID [--todo TODO_ID ...] --result TEXT --check TEXT --skills TEXT [--note TEXT]
                             move the request's remaining work to the selected open to-dos
agi done --as <agent> <id> --result TEXT --check TEXT --skills TEXT
                             complete the owned task and record its evidence
agi join-task --as <agent> <id> [--todo TODO_ID]
                             join another agent's task, optionally linking your part
agi leave-task --as <agent> <id>
                             leave your helper role
agi remind --as <agent> <id> --in SECONDS [--note TEXT] [--request-id 32HEX]
                             set a one-time reminder for a task you own
agi remind --as <agent> <id> --after-gate PROJECT SHA [--note TEXT] [--request-id 32HEX]
                             remind the task owner when that exact commit reaches main
agi cancel-reminder --as <agent> <id> [--request-id 32HEX]
                             cancel that task's reminder or target a specific reminder ID
agi docs [TOPIC]             list installed documentation topics or read one topic as Markdown
```

## Installed documentation

`agi docs` and the native `chat_help` tool read `docs/topics/` in the running package. The tool takes an
optional `topic` argument. With no topic it returns an object with `version` and `topics`. Each topic has
`topic`, `title`, `summary` and `read_when` fields. With a topic it returns `version`, those four fields and
`markdown`. Topic names are the slugs of the bundled Markdown files. Only those names can be read, and
an unknown name tells the reader how to list available topics. Lookup never reads or changes the chat.

A topic file starts with `summary` and `read_when` in its front matter and then a Markdown heading. The
release ships these same public files. Both integrations point to `chat_help` in their instructions. When
an agent first checks unread release notes for a version, the notes response also includes the topic list
as `docs`. Acknowledging those notes keeps the existing release receipt and leaves the chat cursor alone.

## Task reminders

A human or the current claim owner can set one reminder per owned task. Set it again to move its
deadline. A CLI retry with the same request ID keeps the original deadline. The optional note is
limited to 500 characters. `/tasks` shows pending deadlines and exact revisions, including after a handoff.

In the chat screen, use `/todo #N gate PROJECT SHA [note]` for a check that follows the request.
Give `AGI` or `Artlab` and the full tested commit SHA. It fires once that exact commit is
in the project's local main history, including after newer integrations. A rebase gives the
candidate a new SHA. Update the reminder to the revision the gate is actually testing.
Set `AGI_GATE_REPO` to select a different AGI checkout for the exact-commit check.

Due reminders post a single system message per current owner, using an explicit mention. A released
task waits for a new owner, a completed task is skipped, and paused or blocked posting delays delivery.
The event that posts the reminder also records its delivery under the log lock, so a restart does not
post a duplicate. The chat window, Codex bridge and Claude channel check deadlines as part of their
existing listeners. If they are all closed, due reminders are checked when one reopens.

A to-do with a deadline uses that same scheduler. At delivery its
`todo_check` record names the item, deadline change revision, due time,
current owner and any different recipient. The record is the delivery receipt, so concurrent
listeners and restarts see one notice for the scheduled date. Rescheduling
changes the receipt identity. Clearing the deadline or completing the item
cancels delivery. The notice preserves the item's revision, prerequisite and
next actor.

## ui/ (the chat screen)
- Display only. Reads with `read_tail` (startup), `read_messages`, `wait_for_change`, `search_messages`,
  `guard_status`, `active_claims`, `get_presence` and `get_ack`; writes only through `ui_state.DraftSession.send`
  (or `append_message(USER, ...)` when draft state is unavailable), `append_message(USER, path, claim|release)`
  and `set_policy`/`resume_policy`. For reminders it uses `schedule_reminder` and `cancel_reminder` as
  the user. Gate reminders use `schedule_gate_reminder`. Uses `ui_state.get_read_marker`/`mark_read` for the user's `new` line, and
  `mentioned(text, names)` for whether a message mentions the user (the rule `mentioned_agents` uses).
- Never touches agent read positions and never applies agent rules.
- Slash commands: the catalogue lives in `agi/ui/composer.py` (`COMMAND_HELP`); `/help` and `/` list them.
  The picker prefers command-name prefixes, then searches the descriptions. Enter on a description match or
  unfinished argument fills the selected text without running it. Tab fills the selection and leaves it editable.
  Bare `/export` runs with the full-history default. `/mode`, `/mouse`, `/custom off` and the user's own active file
  releases have argument completion. Start a message with `//` to send text that begins with `/`.
- **Replies** are plain text: a message starting `↪ #<id> ` answers message `<id>` (1–12 digits). Agents may use the
  same prefix. The screen draws a one-row quote of the target when it holds it; quoted text never counts as a mention.
- Layout is a thread with activity and task summaries. Task claims
  and releases are drawn under the user message they refer to. An agent's messages less than three minutes apart
  share one header; each keeps its own `#id` and time.
- The draft is saved about a second after the last edit and cleared only after a send succeeds. Bracketed paste
  (`ESC[200~`…`ESC[201~`) is inserted as text; pastes over 1,000 characters or 12 lines become a chip.
- The read marker moves only after a key or focus-in at the live bottom of the transcript, written off the input thread.
- Focus reporting (`ESC[?1004h`) drives desktop notifications (`notify-send`, off with `AGI_NOTIFY=0`) for
  mentions while unfocused, and an `AGI (N)` window title counting arrivals while away.
- The theme follows the terminal's background, asked at startup with an OSC 11 query (late replies are
  swallowed); `AGI_THEME=light|dark` overrides it, and `NO_COLOR` turns off all colour.
- Threads: a watcher sleeps in `wait_for_change`; a status thread loads guard, claims and presence every 2 s (`STATUS_EVERY`)
  (its first, full scan waits for 1.5 s without typing). Both wake the main loop through pipes; store reads never
  run on the input path.

## Agent behaviour
The join loop is in `docs/Joining.md`. The rules are in `AGENTS.md`.

`agi skill read --as <agent> NAME` prints the installed skill and saves its file name, resolved path,
SHA-256 and read time. `agi skill record --as <agent> NAME` saves the same receipt and prints it as JSON
for a provider hook. Names resolve in the provider's skill directory, its `.system` subdirectory, then the
shared skills directory. Set `AGI_SKILLS_DIR` to override the shared directory.

During a review, use `agi skill list --as <agent>` to see the latest receipt for each skill. Cite the
instruction that affected the work. Receipts live in `data/skills/` and have no effect on chat read positions.

## Presence and cheap reads
- `last_id()` returns the latest committed log id, or 0 for an empty log. Sequence metadata alone is
  not authoritative after a crash. It does not copy message history into its result.
- Work status comes from a provider turn observation or an explicit report. `since` stays
  fixed during a state, and `updated_at` and `expires_at` bound the observation. Times are
  UTC Unix seconds and missing times are null. An optional note is limited to 200 characters.
- Reachability is a separate lease. Blocking `wait` renews it every 30 seconds and leaves the
  work state alone. Acknowledgment changes only the inbox cursor. Saved legacy `listening`
  and unversioned presence files establish contact only. Initial contact alone appears idle.
  When the last working observation
  expires and contact stays fresh, activity is unknown and the screen shows connected.
  An idle observation clears work and keeps the separate contact lease in force. With both
  leases expired, activity is unknown and `reachable` is false.
- `agi heartbeat --as <agent> [--ttl SECONDS]` refreshes contact, default 90 seconds.
  `agi status --as <agent> working|idle [--ttl SECONDS] [--note TEXT]` records work state.
  The Codex watcher reads the native turn state even with posting paused and refreshes the
  leases every 10 seconds with a 35-second lifetime.
- Work and contact live in `data/presence/<agent>.json` and `<agent>.reach.json`. Writes
  are atomic under a per-agent lock. Presence never changes the log, guard or acknowledgment cursor.
- Store readers may cache validated append-only records within a process. Replacement, truncation,
  same-size edits and removed files invalidate the affected cache. Returned messages remain independent
  dictionaries so callers cannot mutate the store cache. Hand edits while appending are unsupported.

## Wake timing

Providers call `woke(agent, message_id, first_id=..., queued_at=...)` after
their queue or handoff accepts an incoming message or batch. `message_id`
is the last included message. `first_id` defaults to that same ID.
`queued_at` uses Unix seconds and defaults to the time the receipt is written.
A successful queue is evidence about the provider handoff. The agent's
acknowledgment is the evidence of handling.

The cursor file keeps up to 20 recent receipt ranges per agent. A retry of
the same range end keeps the earliest recorded queue time. Advancing the
ack stamps newly covered sample ranges in the same atomic write. Legacy
cursors keep their IDs and start with an empty sample history.

`wake_ranges(agent)` returns the retained inclusive `(first_id, last_id)`
ranges for read receipts. An agent with no retained samples returns an empty tuple.

`wake_timing(agent)` returns `last` and `recent` with samples
`{id, wake_s, ack_s}`. Both durations start at the log time of the sampled
message. An unknown or backwards-clock duration is null. `oldest_unacked`
is null or `{id, age, queued}` for the oldest unread user message. `queued`
reports coverage by a retained receipt. Display a delay warning only while
posting policy allows delivery. Receipts are diagnostic and leave posting,
retries, acknowledgments and work state independent.

## Policy CLI and user controls
- `agi custom N M [D]`: N agent messages in each rolling M-minute window, for D minutes, or until changed
  without D.
- `agi mode normal|wait|paused|resume`, `agi pause`, `agi resume` change/restore policy.
  `agi guard` prints current status. Screen commands are `/custom N M [D]`, `/custom off`,
  `/mode normal|wait`, `/pause`, `/resume`.
- Normal/custom capacity refills by time; agents wait at least `retry_after` and retry only useful posts.
  Wait mode resumes on a user post; paused needs an explicit resume/policy command. Never evade a refusal.
- Policy expiry changes the posting budget, not listening or an already assigned task. The UI displays
  the selected policy, rate and any expiry. Clock changes can affect wall-time windows.

## Bounded history inspection
- `read_tail(limit=1000)` returns the latest `limit` committed records in ascending id order;
  limit must be a non-negative integer. Returned dictionaries are independent of the cache.
- `agi log [--last N]` prints the latest N JSON records, default 20, maximum 1000; zero prints none.
- `agi show <id>` prints the complete JSON record for that positive id, or exits 1 if absent.
  Neither command changes acknowledgments, presence, claims or guard state.
- The UI preserves leading indentation when sending; final trailing whitespace/newlines are trimmed.
  A whitespace-only draft is not sent. Store/CLI text remains exactly what the caller supplies.

## Append success boundary
A message is committed once its complete record and chat directory have both been fsynced.
`seq` is auxiliary monotonic metadata: after that boundary, an OSError updating seq does not turn
success into a failed send. Return the committed record; the next writer takes max(seq, log id).
Failures before that durability boundary still propagate. UI refresh errors after a returned append
must not invite resending the already committed message.

## Fast history access
- `read_tail(limit=1000)` reads only bounded committed file tails, returning the latest ids in ascending
  order. It ignores unterminated suffixes and validates selected records. It does not audit every old
  record; the existing full scan remains the integrity check.
- Daily filenames need not match id order after clock changes. Tail selection compares each file's
  final committed id and merges candidate tails, assuming the store's per-file append order.
- `search_messages(query, limit=50, before_id=None, *, kind=None)` performs literal Unicode casefold matching over
  message text, returning the latest matches in ascending id order. `before_id` is exclusive pagination.
  Optional `kind` filters msg|claim|release|system before the limit; None retains all kinds.
  The screen uses msg so protocol matches cannot hide older conversation matches.
  `agi search TEXT [--last N] [--before ID]` exposes this as JSON lines (default 50, maximum 1000).
  Query must contain non-whitespace text; limits and ids must be non-negative integers. Zero limit
  returns no records. Neither access path changes acknowledgments or other conversation state.
- UI first paint should use the tail and defer full state scanning to a worker, then refresh state.

## Human UI state (ui_state.py, owned by Codex)
- `get_read_marker() -> int` and `mark_read(id) -> int` store a versioned, monotonic human marker
  in `data/ui/read.json`, separate from agent acks. Future ids are rejected. The UI marks interaction
  at latest, never background arrivals; the session's displayed new-message divider remains fixed.
- `DraftSession()` is a context manager/close handle with `restored` (None or a dict with draft,
  cursor, chips, chip_count and optional attachments), `warnings` (preserved-but-unreadable drafts) and
  `save(draft, cursor, chips=None, chip_count=0, *, attachments=None)`. Each window holds its own exclusive lock.
- Startup recovers the most recently modified valid saved draft whose lock is free; a live window's
  draft is never stolen. Other saved drafts remain preserved. New windows otherwise get a new id.
- Versioned draft JSON under `data/ui/drafts/` is atomically saved with chip payloads and any attachment
  references. An empty draft has no text or attachments. The UI debounces edits about one second, flushes on graceful exit, and
  closes the handle. An abrupt power loss can lose the last unsaved debounce interval.
- Malformed saved drafts are preserved and skipped with warnings. State does not append messages,
  reset the guard or alter agent read positions. Session state and control files use mode 0600.

Cold `read_messages(after_id)` may use a bounded tail when it fully covers the requested range;
older cursors fall back to full scanning. It never truncates an unread backlog to fit the tail.
Full in-process caches continue to use incremental log reads. The UI must also load cold guard/claim
state off its input/render thread, since those are full integrity scans.

## Idempotent sends
- `request_id` is optional for message-kind records: exactly 32 lowercase hexadecimal characters.
  Legacy records and CLI/agent calls without it retain their existing shape and behavior.
  Use the same value in MCP `chat_post(request_id=...)` and CLI `agi say --request-id ID` when
  retrying the same message through a different transport.
- Under the log lock, retrying the same request id with identical sender, kind, text and attachments returns the original
  record after confirming log durability. Reusing it for different content is rejected. Duplicate
  request ids in the committed log are corruption, not two legitimate deliveries.
- `find_request(id)` returns a matching durable record or None. It does not append or acknowledge.
- `DraftSession.send(text, draft, cursor, chips=None, chip_count=0, *, attachments=None)` first saves a pending-send token
  with the draft, then appends idempotently and clears the saved draft. It returns the stored record.
  A failed pre-commit operation leaves the saved intent for retry. A post-commit clear failure still
  returns success; recovery recognizes the pending token in the authoritative log and avoids a ghost.
- Recovery restores an uncommitted pending intent without auto-sending it. Changed draft content
  starts a new intent; cursor-only changes preserve it. Normal draft records need no log scan.
- After a lossless UI migration of a restored draft, call
  `DraftSession.adopt_restored_representation(draft, cursor, chips, chip_count, *, attachments=None)` once before saving.
  This keeps its pending intent through cursor moves. The next save or send persists the new form.

## Codex same-session watcher (bridge.py and codex_client.py)
- `agi bridge start [--thread UUID]`, `status`, `stop`, `retry`: one local process targeting the
  existing Codex session. Start defaults to CODEX_THREAD_ID; no new session is ever created.
- The watcher reads native thread status through the existing local Unix WebSocket, with metadata
  only. It queues through `codex queue --thread UUID --message ... --resume` only when not active.
- Silence, self-echoes and general system records cause no model request. User messages always qualify;
  peer messages qualify for @codex/@all or replies to a Codex message. Other peer messages remain
  available on the next read. Relevant unread messages
  are retained until acknowledged. All records remain in the shared history; ack never moves itself.
- A user release of Codex's task claim wakes Codex with the release record.
- Normal delivery has one outstanding wake, cleared when ack covers its range. Before acting on it,
  `agi bridge accepts WAKE_ID` checks the exact thread, membership revision and pending wake.
  Prepared/queued/failed
  phases and a stable wake id survive restart. At most three attempts are made without acknowledgment;
  failure is visible in status/presence. A newer user message reopens delivery without skipping the
  older inbox; manual retry is also available. Native active turns are never requeued.
- Posting policy is checked before waking. Pause/wait and rate delays are handled by the local process.
- Durable state, private log and singleton locks live in data/bridge/. Stop wakes the local process
  through inotify and never sends a signal to a PID from metadata. No network listener is opened.
- On an I/O failure, a running watcher closes its connections and backs off for ten seconds.
  It can resume unread delivery even when the error status could not be written. Before reopening
  its watch, it checks that the held runner lock still belongs to the current data directory and
  mount. A replaced lock or mount requires a fresh start.
- `agi bridge probe` requests a same-session diagnostic wake. The notified agent runs
  `agi bridge confirm WAKE_ID`; confirmation records thread identity without guessing an ack.
- Queue exit zero is not proof of successful resume: stderr warnings are retained, and delivery is
  confirmed only by native activity/agent acknowledgment (or explicit probe confirmation).

## Session membership

Provider registration points a client at the chat. Joining selects a session
to receive new messages. AGI saves that selection under the log lock and
tracks reachability with expiring per-session leases.

- Each provider has one selected session. The provider asks to join from
  that exact session. Changing the selection requires user confirmation.
- Leaving invalidates pending delivery for the previous revision. Codex
  withdraws matching items still queued where the native service supports
  it. A turn already running in a provider can finish.
- Rejoining retains the agent's acknowledgment. The consumer starts
  with the unread records after that receipt.
- A task claim remains with its owner when they leave. A separate user
  action can release one claim by its exact ID. It appends a system
  record and keeps the task history.

## Search, preferences and connection diagnostics

- `history_view.find(query, before=None, limit=30)` returns matching messages
  newest first and a `more` flag. An optional `from:` filter applies before
  the limit. `history_view.catch_up(after)` uses the journal to project
  `waiting`, `finished`, `active` and `other_unread`. Neither operation
  writes to a read marker, draft, agent receipt or transcript anchor.
- `store.add_personal_todo(title, due_at=None, request_id=None)` writes a
  to-do owned by `user` with `source_id=None`. The event supplies the item's
  ID. A timed receipt addresses the user. A retried request ID returns the
  same item.
- `ui_state.user_settings()` reads the conversation's notification and
  mouse preferences. `set_user_setting(name, enabled)` updates one value
  under a lock. An explicit `AGI_NOTIFY` or `AGI_MOUSE` applies to that
  process. Sound and posting policy use their own existing files.
- `membership.status(agent)` supplies the selected session, reachability,
  revision and last recorded contact. `resume_command(agent)` returns
  the provider's command for a known session ID. It has no side effects.
- `agi delivery-test CLAUDE_OR_CODEX` requests one test.
  `--status` reads its result. The Claude channel reserves one ping and
  confirms the matching ID and membership revision. This test writes
  diagnostic state and leaves the conversation acknowledgment intact.
- `agi task-evidence --as AGENT ID` and Codex's `chat_task_evidence`
  suggest the actual skill receipts for the agent's active claim.
  An explicitly selected integrated gate or report can supply suggested
  check text. Recording a handoff or completion still requires a
  separate task action.
- `installation.check_update()` returns an offer for the app to show.
  `apply_update(offer)` checks the reviewed release, switches the
  managed version and returns the stable launcher. Restarting the chat
  screen uses its normal save boundary. `agi uninstall --check`
  previews the managed files and owned registrations eligible for removal.

## UI durable-write completion and exit
- One serialized writer handles journalled sends, draft saves and final read-marker writes. Enter captures
  the submitted draft; its text and paste chips remain frozen until commit or failure. Transcript navigation
  remains available. Draft saves are not queued behind an in-flight send, avoiding ghost drafts.
- Send completion clears the draft only after success. Save/send failures preserve retryable text. Marker
  completion is handled separately and never clears the composer. Background completion wakes the UI.
- Normal quit queues any dirty draft and final marker, waits visibly for completion, and cancels closing
  if saving fails. A further Ctrl+C explicitly forces exit. Signals also force exit without disk writes.
- Forced exit prints full unsaved text, expanding paste chips, if persistence is not known to have completed.
  Terminal output is a recovery aid, not a durable backup. No failed/pending write is called saved.
- The writer change covers these durable writes; slash-command reads/exports and transcript refresh reads
  can still wait for storage. The functional UI suite has a 180-second deadline; other checks retain 90 seconds.

## Admin messages

The screen can display saved text-only admin messages and their read marker.
