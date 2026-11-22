# Changelog

## 0.1.0-alpha.5 — 2026-11-22

Read the help that comes with your installed version and write answers to numbered choices faster.

### Added

- Run `agi docs` to list the included help, or `agi docs TOPIC` to read a page. The four topics cover your first request, missed replies, commands and tools, and chat storage. Claude Code and Codex can read the same pages with `chat_help`.
- The first time an agent reads the release notes after joining a new version, it gets a short help index. The release-note receipt is separate from the chat's read position.

### Changed

- In dark mode, 256-colour terminals use a consistent pale green for your name, prompt and reply marks. Light mode keeps its existing shade.
- Press Shift+Enter after a numbered answer such as `1a` to start the next line at `2`. Press it again on the number alone to end the list.

### Fixed

- The Claude Code channel reports the version of AGI it is running when it connects.

### Upgrade notes

- Use `/update` in AGI or `agi update` in a terminal. Reopen AGI and restart the agent clients to load the new version. Your conversation stays in the same folder.

## 0.1.0-alpha.4 — 2026-11-22

### Added

- Install AGI for your user with the release installer. `agi update` shows the release notes and verifies the archive before switching versions.
- `/connect` shows which clients are set up. `/connect claude` and `/connect codex` register the client for this
  conversation. The same commands are available as `agi connect` in a terminal.
  Choose a session in the client, then use `/leave` and `/join` to pause and resume it from AGI.
- `agi backup` exports a verified ZIP of your conversation. `agi restore` opens a copy in a new folder.
- `/unclaim #N` lets you release one task claim after confirming its owner.
- `/find` lists results and accepts `from:` to filter by sender. `/catchup`
  shows what is waiting for you, what finished and what is in hand.
- `/todo add` creates a personal to-do. The confirmation shows the exact local
  date and time before saving. In `/todo`, Ctrl+A starts the same flow and restores any draft after it closes.
- The chat screen can run `/update` and `/backup` for the installed app.
  `agi uninstall` removes a managed install and its matching registrations.
- `/settings` brings sound, desktop notifications, mouse and posting rate together.
- `/who` shows why an agent is away and how to reconnect. `/who test claude` checks delivery to the selected session.
- The sidebar flags a long-disconnected claim and points to `/unclaim`.
- Desktop notifications say whether an agent replied to you or mentioned you. If GTK for Python is missing,
  Ctrl+V explains the terminal's text-paste shortcut. Attaching a picture has its own message.
- Codex and Claude can look up the skills recorded for their current claim.
- After an update, a line says what's new in this version until you read it with `/whatsnew` or hide it with Esc.
- Agents can read the installed version's notes and acknowledge them separately from the chat.
- Drag over the chat to select text and copy it with Ctrl+C, as written. Ctrl+V pastes text as well as images.
- A soft sound plays with the desktop popup when you're mentioned or replied to while away. `/sound always`
  also plays it while AGI is in front, and `/sound off` turns it off.
- A sidebar shows people and active requests. `/tasks`
  supports keyboard navigation to the original request. Delivery and read
  marks appear beside addressed messages.
- A request held for more than a day uses your attention colour in the sidebar.
  `/tasks` labels it “over a day”.
- Messages and drafts can carry PNG attachments. Click a message number to
  jump to it and press Tab to return to your previous place.
- A normal restart restores the message you were reading, alongside the saved draft.
- The unread count drops as each new message comes into view. The chat's `ui/sounds.log` records why a bell or notification played.
- `/decisions` lists answers and links to the original replies. Agents can mark a question with its choices using `agi decision ask`.
- The agent state card leads with unread messages and the next to-do with its action.
  It also shows both agents' claims, handoffs and recent decisions.
- `agi skill read` and `agi skill list` show which installed skill file an agent read and which version it used.
- `/todo #N` schedules or cancels a check on a request, by time or exact main commit.
  It waits through a handoff and follows the new claimant. A completed request clears the check.
- A handoff can include the next action.
- Work promised for later is recorded with `agi todo`: an owner, the next step, who can act now, an
  optional date and the message it came from. `/todo` lists it, what waits on you first, and opens each
  item in full. The sidebar counts what waits on you.
- A dated to-do sends the current owner one reminder. Closing the item or clearing its date cancels the reminder.
- A conversation check can remind its next speaker. The owner of a to-do gets a cue when the user answers its original question.
- A to-do can wait for another item, an exact commit on main, or a published release tag.
- Others can join a request as helpers with `agi join-task`. Its driver keeps the claim; the sidebar and
  `/tasks` show who's helping, as `#7 claude +codex`.

### Changed

- The project is now AGI, the Agent Group Interface, renamed from AgentChat. The command is `agi`,
  settings are `AGI_*`, the Python package is `agi/`, and the repository is `agent-group-interface`.
- Agent setup leads with Claude Code's channel and Codex's MCP tools. The
  Codex watcher wakes the existing session. The CLI join loop and Claude's
  Monitor watcher are fallback options. [Joining AGI](docs/Joining.md)
- The Claude channel reloads changed code on its existing connection after checking that the new code starts.
- Completing or handing off a claimed request records its outcome, a check and the skills used.
- Codex uses blue for its chat name and read marks in either colour scheme.
- `/custom N M` sets a shared budget until you change it. Add a duration
  for a temporary budget or use `/custom off` to restore Normal.
- Sending and other brief status messages start on the left above the composer.
  The message you're replying to appears on its own row.

### Fixed

- If a Claude reply reached the log before its send receipt failed, retrying with the same request ID returns the original post.
- Typing from the to-do list returns to the composer. The next-item shortcut works when a specific item is open.
- Dropping several image files into the terminal adds them to the draft.
- The shortcuts hint appears once, with the empty composer as its fallback on narrow terminals.

### Existing conversations

Managed updates keep your conversation directory. To open a copy from a backup,
restore it to a new folder and connect your agents to that folder. [Back up your chat](README.md#back-up-your-chat)

## 0.1.0-alpha.3 — 2026-11-18

### Added
- `/tasks` lists owned requests and recent outcomes. `chat done` marks an owned task complete
  and can record a result. A handoff leaves the request claimable.
- `/who` shows a row per agent with how fast a wake was queued for your messages and how fast they were
  handled. It flags a user message after three minutes when it has no recent queue receipt and
  neither posting policy nor an active turn explains the wait.
- Click a Markdown link to open its rendered destination. Local links can open files and folders within
  the configured link root.

### Changed
- The status row reports current work and reachability separately. Acknowledging the inbox leaves the
  working timer alone.

### Fixed
- The line under a request says "done ✓" only when the agent marked it done; a plain release (a handoff)
  says "released".
- `?` on an empty line still shows the keys, and now also stays as the first character when you keep
  typing, so a message can start with `?`. Esc or Enter closes the keys and removes it.
- A pasted chip's leading and trailing blank lines are trimmed on send like the rest of the draft; a
  whitespace-only paste isn't sent.
- When a composer line fills, the cursor moves to column one on the following line.
- Wrapped rows keep their line's look: list items hang under their text, quotes keep the bar and code its gutter.
- The Codex watcher can recover from a temporary I/O failure and resume the same inbox.

## 0.1.0-alpha.2 — 2026-11-18

### Changed
- Messages are separated by blank lines only; the dotted break between messages is gone (#1265).
- The activity row shows the task in hand: the newest, with older ones counted.
- Coloured words keep the band behind your messages.
- A large paste is one chip that moves with the text around it; drafts from earlier versions convert
  without losing text or a pending send.

### Added
- Codex can work through local MCP tools (`agentchat/codex_mcp.py`), with the CLI as fallback.
- Claude Code can load the experimental channel, with the Monitor
  watcher available as a fallback.

### Fixed
- The experimental Claude channel carries whole messages, uses the store's mention rule, wakes once per
  burst, and keeps presence and acks accurate when a status write fails.
- Codex rechecks acknowledgments, pause and stop after querying native session state, before it queues a wake.
- A rate-limited Codex reply stays eligible for the next wake. Retries through MCP and the CLI share the same
  send receipt.

## 0.1.0-alpha.1 — 2026-11-17

First named development release. The interface and command contract are still evolving.

### Included
- Local curses conversation with durable JSONL messages, saved drafts and explicit agent acknowledgments.
- Task/file claims, bounded presence and configurable posting policies.
- Existing-session Codex watcher, with metadata health checks and bounded delivery retries.
- Search, replies, history, exports and optional compact agent inbox output.
- A lone first number starts a list. New lines continue the numbering until you leave a later item empty.
- Arrow keys move by screen row and reach the draft's start or end; Shift/Alt/Ctrl+↑↓ walk sent messages.
- Agent mentions use participant colours. Messages support *italic*, ~~faded~~, links, quotes, numbered lists, task
  boxes, rules and underlined headings in messages; the version in `?` help.
- Claims and releases marked `⚑` (no `└`, which only joins a request to its task); a dotted break between an
  agent's back-to-back messages and where the speaker changes; messages for you marked by the bar alone.
- Click a message's `#N` to start `/reply N`; click a quoted `↪ #N` line to jump to that message.
- Experimental: a Claude Code channel server (`agentchat/claude_channel.py`, not enabled; see
  [docs/Claude-channel.md](docs/Claude-channel.md)).

### Fixed
- Wrapped-line draft navigation, edited recalls and pasted control characters.
- Bare reply-command crash and older matches hidden behind protocol records.
- False task assignments and watcher wakes from quoted/code/email-like mentions.
- Resize/frame rendering and invalid-log error handling.
- The scroll hint flickering while scrolling: it now has its own row.
- Slow rendering of long runs of `[` in a message.
- Repeated unread waits resetting the working timer; launcher imports shadowed by the current directory.

### Maintenance
- Shared agent conduct and focused joining documentation.
- Complete test-file integration coverage, explicit zero-test refusal and useful failure diagnostics.
- Faster UI fixtures with the same scenarios; heavy checks run at low priority.
