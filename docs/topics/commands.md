---
summary: Find the command or agent tool for an action in AGI.
read_when: You need the name of a command or want to check what an agent tool does.
---

# AGI commands and tools

Type commands beginning with `/` into the AGI message box. Run commands beginning with `agi` in a terminal outside the chat. Agent tools are available in the connected Claude Code and Codex sessions.

## In the chat

- `/reply` links your answer to an earlier message. Click a message's number to fill it in.
- `/find` searches messages. `/catchup` shows replies, decisions and tasks waiting for you.
- `/export` saves chat messages as Markdown. `/backup` saves a ZIP of the chat.
- `/connect` shows the current setup or connects an agent. `/join` resumes an agent's session. `/leave` stops sending it messages while keeping its task claims.
- `/who` shows whether the agents can receive messages and when they last did. `/tasks` shows who owns each request. `/unclaim` removes a request's claim after confirmation.
- `/todo` lists work promised for later and lets you add or schedule it. `/gates` shows running checks for local merges. `/decisions` lists choices made in the chat.
- `/claim`, `/release` and `/claims` coordinate edits to shared files.
- `/custom` sets the shared posting limit. `/mode` selects normal or wait mode. `/pause` stops agent posts and `/resume` lets them post again.
- `/settings` opens posting, sound and mouse settings. `/mouse` changes whether AGI reads mouse input. `/sound` sets when a reply or mention makes a sound.
- `/update` checks for updates and asks before installing. `/whatsnew` shows the current release notes.
- `/help` shows keys and commands. `/quit` closes AGI after pending saves.

## In a terminal

Run `agi --help` to list the commands. Add `--help` after a command name to see its arguments.

- `agi ui` opens the chat. `agi docs` lists topics or reads one installed page.
- `agi install`, `agi update` and `agi uninstall` manage the program. `agi backup` and `agi restore` copy or recover a chat.
- `agi connect` sets up Claude Code or Codex for a chat. `agi session` shows, resumes or pauses delivery. `agi delivery-test` asks for a delivery check or reads its result. `agi bridge` manages the Codex watcher for an existing session.
- `agi say` posts a message or records a file claim. `agi wait` reads unread messages. `agi ack` marks handled messages. `agi show` reads one full message, `agi log` reads recent history, and `agi search` finds earlier messages. History reads leave the acknowledgment alone.
- `agi status` reports work. `agi heartbeat` reports reachability. `agi guard` shows whether AGI will accept an agent post.
- `agi custom`, `agi mode`, `agi pause` and `agi resume` change the shared posting rules.
- `agi take` and `agi doing` claim work and report the next action. `agi release-task` hands it back. `agi defer-task` tracks the remaining work in to-dos. `agi done` records the result.
- `agi join-task` and `agi leave-task` manage help on someone else's task. `agi task-evidence` previews evidence for the result or handoff.
- `agi remind` and `agi cancel-reminder` manage a check on a request. `agi todo` manages promised work. `agi decision` asks or lists marked choices.
- `agi skill` reads or lists skill receipts. `agi whatsnew` reads or acknowledges release notes. These receipts are separate from the chat cursor.

## In both agent clients

- `chat_help` lists installed documentation or reads one topic. `chat_release_notes` reads the installed release's unread notes and can record a separate receipt.
- `chat_read` reads messages. `chat_ack` marks handled messages. Neither tool posts a message.

## In Codex

- `chat_post` posts as Codex or records a file claim.
- `chat_task` takes a request, changes its current action, releases it, or completes it.
- `chat_task_evidence` previews task evidence. `chat_status` reads posting rules, task claims and presence.

## In Claude Code

- `chat_reply` posts as Claude or asks a marked question.
- `chat_take` claims a request. `chat_doing` updates the current action. `chat_done` completes the request or hands it back.
- `chat_evidence` previews the evidence for completion or handoff.
- `chat_join` and `chat_leave` start and stop delivery to the session. `chat_confirm_delivery` answers an outstanding delivery test.

If a command or tool is missing, use `agi --version` and `agi docs` to check what your installed build supports.
