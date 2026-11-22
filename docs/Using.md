# Using the chat screen

Start the chat with `bin/agi` in a terminal. `?` on an empty line shows the keys, and stays as text if you keep
typing. Below 30 × 8 the screen asks for a larger terminal.

## Writing and sending
- Enter sends. Shift+Enter (or Ctrl+J, Alt+Enter) starts a new line, and a multi-line paste stays in the box
  until you press Enter. A very large paste shows as a `[Pasted text #1 · 200 lines]` chip that Backspace removes
  in one go and Enter sends in full.
- Ctrl+V pastes the clipboard's text at the cursor, or attaches its image; an image file dropped on the terminal is attached too (up to four
  per message). They show above the draft, Backspace at its start removes the last, and in the chat each is a
  link that opens it full size.
- Over SSH, Shift+Enter works when the terminal reports modified keys, which the screen asks for
  (`AGI_KEYS=legacy` stops asking). In a local X session it also checks whether Shift is held
  (`AGI_SHIFT_ENTER=0` stops that).
- What you're typing is saved as you go, so after a crash or power cut the next `agi` restores it.
- Sending keeps you where you're reading; it doesn't scroll to the bottom.

## Replying
- Click a message's number (`#42 13:00`, right of its first row) to start a reply: `/reply #42` on its own line,
  with the cursor on the line below. `/reply 42 text` works too, and sends `↪ #42 text` with a one-line quote.
- Tab after `/reply` fills in the newest message that mentions or answers you; further Tabs go to older ones.
- An @name calls that participant (an agent wakes up for it). To name someone without calling them, write it in
  backticks (`` `@admin` ``) or after a backslash (`\@admin`): neither counts as a mention.
- Agents reply the same way by starting a message with `↪ #42`. An agent's messages sent within three minutes
  share one header.

## Reading and moving around
- A normal exit saves the message at the top of the screen. Reopen AGI to
  return there. If you left at the latest message, AGI opens at the latest again.
- The touchpad or wheel scrolls the chat; PageUp and PageDown move half a screen. Esc goes back to the latest.
- Click any `#number` in a message (or a quoted `↪ #N` line) to go to that message; an older one opens a history
  view. Tab goes back to where you were before the jump, a step at a time, with or without text in the box (where
  the cursor isn't on an @name or /command for Tab to fill in).
- `/find words` jumps to the newest message containing them, its words highlighted until Esc, and lists older
  matches under the note. A bare `/find` repeats the search for older ones. `/find from:codex` (or another
  sender's name) lists that sender's messages and doesn't jump.
- In a `/find` or `/catchup` list, ↑ ↓ choose a row and Enter opens it. Tab goes back to where you were before the
  search. Any other key closes the list.
- A green `── new ──` line marks where you stopped reading. Click it to open `/catchup`.
- `/catchup` shows what is waiting for you since you last read: questions marked with choices that nobody has
  answered, to-dos waiting on you and replies to you. Then it shows what finished and what is in hand. It says how
  many other messages stay unread, and it marks nothing as read.
- A link shows its words underlined with ↗; clicking it opens it (a web page in the browser, a file or folder
  inside your home in its usual app). A program never runs; `AGI_LINK_ROOT` changes where links may point.
- Drag over the chat to select words, then Ctrl+C copies them as written: wrapped lines joined back, without names,
  times or marks; it stays on the clipboard after AGI closes. Esc clears the selection, and Ctrl+C with none still
  quits. The terminal's own selection is Shift+drag (`/mouse off` gives it back fully, `AGI_MOUSE=0` starts that way).
- Messages that mention you (`@user`, `@you` or `@all`, not inside code or a quote) get a green bar
  and a beep. If the window isn't focused, a desktop notification pops up with a soft sound (`AGI_NOTIFY=0` turns
  both off). `/sound away` is that default; `/sound always` also plays it while AGI is in front, `/sound off`
  silences it; the choice is remembered. The sound is the desktop's "new message" one, played with `paplay`. Every
  sound and terminal bell writes one line (time, message, why) to `ui/sounds.log` in the chat's data folder.

## Who's doing what
- From 80 columns, a rail on the left shows people, tasks and gates: what each agent is doing, the requests in
  hand and the last few done, and each project's gate. Click a task to go to its request and see all its words
  in the rail (Esc closes it), a person for everything they're on, the gates for their detail.
  `/tasks` beside the rail lists them below as well and puts the keyboard in the rail: ↑ ↓ choose, Enter opens,
  Esc goes back to typing.
- Narrower, the row above the box says what each agent is doing.
- Either way, only what is true: **working** (its own turn is running; its task and for how long), **waiting · #N**
  (holds a task between turns), **idle** (reachable, nothing held), **away** or **left** (it left the chat by
  choice; see `/leave` below).
- Your messages and mentions carry a quiet mark per recipient: a dot in its colour once delivered, its check once
  acked. `/tasks`, `/gates` and `/who` list the details.
- The admin session posts as **● admin** and sees messages that say **@admin**, and your replies to its own posts.

## What's promised for later
- Work someone promised for later is a to-do: an owner, a next step and the message it came from. In the chat each
  one shows as a line after **◷** (`T4157 claude · First daily review · due 22 Nov 09:00`, then whose next it is
  and, at the end, `done · …`). A `T` number in a message or on a ◷ line is a link: click it to open that to-do, done ones too.
- The rail counts them under the tasks: **you N** for what waits on you now, **later N** for the rest.
- `/todo` lists them, yours first, then each owner's, with how long each has waited and any date. ↑ ↓ choose,
  Enter opens one in full (↑ ↓ scroll it; `o` opens its linked report or checklist, if it has one), Enter again goes to the message it came from,
  in an opened one `n` marks it as its owner's next ("up next"), Esc steps back and then out. Typing anything
  else in the list goes back to the box.
- Agents record them with `agi todo add` before saying they'll do something later ([To-do](Todo.md)).

## Commands
Type `/` to find a command by name or by a word like `search`. ↑/↓ choose, Tab fills in, Esc hides the list,
Enter runs a complete command. A space after commands such as `/mode` shows their choices.
- `/connect` shows each installed client's setup. Run `/connect claude` or `/connect codex` to set it up for the
  open conversation. If the client points at another chat, AGI shows its folder and asks before changing it.
- `/who` shows each agent's connection and what to do if delivery stalls. `/who test claude` sends one test to
  the joined Claude session. Run `/who` again to see whether it answered.
- `/settings` is one list: the mention sound, desktop notifications, whether the mouse scrolls the chat and the
  agents' posting rate. ↑ ↓ choose a row and Enter changes it at once. It also shows the chat folder. The separate
  commands (`/sound`, `/mouse`, `/mode`, `/pause`) still work.
- `/update` checks for a newer AGI and shows what's new. It asks before installing, then asks whether to restart.
  Your draft and your place stay.
- `/backup` saves a verified copy of this chat as a ZIP file and says where it is.
- `/join claude` or `/join codex` delivers the chat to that agent's session again. `/leave claude` or
  `/leave codex` stops delivering it; the agent's claims stay. If another session of the same agent asks to join,
  the note line asks whether to move it there (Enter yes, Esc no). The sidebar shows **left** for an agent that
  left this way.
- `/unclaim #N` takes an agent's claim off request N. It shows whose claim it is and what they were doing, and asks
  yes or no first (Enter yes, Esc no). The former owner receives a notice.
- `/custom 10 1 60` allows ten agent messages per rolling minute for sixty minutes; `/custom 4 1`, without the
  last number, keeps four per minute until you change it. `/custom off` returns to normal: five per rolling minute,
  shared by both agents.
- `/mode wait` selects the user-reset guard; `/pause` pauses agent posts until `/resume`.
- `/export` (or `/export 300-404`) saves Markdown to `exports/` beside the chat's data folder
  (`AGI_EXPORTS` changes it).
- `/decisions` lists your answers to questions an agent marked with choices (1a, 1b, …), newest first: the
  picks, ✎ where you added words, the message they're in (click to read), and "needs a look" for an unclear one.
- After an update, a line above the box says what's new until `/whatsnew` shows the notes or Esc hides it; it
  doesn't come back for that version. `/whatsnew` opens them again any time.
- `/todo add what when` adds a to-do of your own. The time is read from the end of the line: `30m`, `in 2h`,
  `9:00`, `tomorrow 9:00`, `fri 17:00` or `2026-11-25`. It shows the time it read and asks yes or no before
  saving (Enter yes, Esc no). With no time, the to-do has no due time. In the `/todo` list, Ctrl+A starts one.
  A message you were typing returns after you save or cancel the to-do.
- `/todo #N 30m [note]` sets a check on request N, `/todo #N gate PROJECT SHA [note]` one for when that commit
  reaches main, `/todo #N off` cancels it. The check is a to-do that follows the request: it goes to whoever holds
  it then, waits while nobody does, and closes with it. Running either set command again reschedules the check.
  [To-do](Todo.md)

## Leaving
Ctrl+C twice leaves after pending saves finish; Ctrl+C again forces exit and prints unsaved text. The last
messages stay in the terminal.

Back to the [README](../README.md).
