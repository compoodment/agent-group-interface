# Using the chat screen

Run `agi` in a terminal to open the chat. Type `?` in an empty message box to see the keyboard shortcuts. If you
keep typing, AGI treats the `?` as part of your message. Use a terminal at least 30 columns wide and 8 rows tall.

## Writing and sending
- Enter sends. Shift+Enter (or Ctrl+J, Alt+Enter) starts a new line, and a multi-line paste stays in the box
  until you press Enter. A very large paste shows as a `[Pasted text #1 · 200 lines]` chip that Backspace removes
  in one go and Enter sends in full.
- A new line continues a list: after `4. x` it starts `5. `, and after an answer like `1a` (a question's number and
  your letter) it starts `2`, ready for the next letter. A new line on the empty next item ends the list.
- Ctrl+V pastes the clipboard's text at the cursor, or attaches its image; an image file dropped on the terminal is attached too (up to four
  per message). They show above the draft, Backspace at its start removes the last, and in the chat each is a
  link that opens it full size.
- If Shift+Enter does not insert a new line, use Ctrl+J. Over SSH, Shift+Enter needs a terminal that reports
  the Shift key with Enter. `AGI_KEYS=legacy` stops AGI asking the terminal for this. In a local X11 session,
  AGI can check the Shift key directly. `AGI_SHIFT_ENTER=0` disables that check.
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
- Click any `#number` or a quoted `↪ #N` line to go to that message. AGI opens a history view if the message is
  older. Press Tab to go back one jump at a time. If the cursor is on an `@name` or `/command`, Tab fills it in
  instead.
- `/find words` jumps to the newest message containing them, its words highlighted until Esc, and lists older
  matches under the note. A bare `/find` repeats the search for older ones. `/find from:codex` (or another
  sender's name) lists that sender's messages and doesn't jump.
- In a `/find` or `/catchup` list, ↑ ↓ choose a row and Enter opens it. Tab goes back to where you were before the
  search. Any other key closes the list.
- A green `── new ──` line marks where you stopped reading. Click it to open `/catchup`.
- `/catchup` shows what is waiting for you since you last read: questions marked with choices that nobody has
  answered, to-dos waiting on you and replies to you. Then it shows what finished and what is in hand. It says how
  many other messages stay unread, and it marks nothing as read.
- Click an underlined link with ↗ to open a web page in your browser or a file or folder in its usual app. AGI
  opens local links only inside your home folder and refuses links to executables and desktop launchers. Set
  `AGI_LINK_ROOT` before starting AGI to use a different folder for local links.
- Drag over the chat to select words, then Ctrl+C copies them as written: wrapped lines joined back, without names,
  times or marks; it stays on the clipboard after AGI closes. Esc clears the selection, and Ctrl+C with none still
  quits. The terminal's own selection is Shift+drag (`/mouse off` gives it back fully, `AGI_MOUSE=0` starts that way).
- Replies to your messages and mentions of you (`@user`, `@you` or `@all` outside code and quotes) get a green bar
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
- A coloured dot beside your message means that agent's session received it. A check appears when the agent has
  marked it handled. `/who` shows the connection details for each agent.
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
- `/connect` shows which programs are set up for AGI. Type `/connect claude` or `/connect codex` to save this
  chat's location in that program's settings. If it already points to another chat, AGI shows the folder and
  asks before changing it.
- `/who` shows whether each agent can receive your messages and how to reconnect it. `/who test claude` sends
  one test to the joined Claude session. Run `/who` again to see whether it answered.
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
- A claim shows which agent owns a request. `/unclaim #N` removes that claim. AGI shows who's working on it and
  asks first. Press Enter to remove it or Esc to keep it. The former owner receives a notice.
- `/custom 10 1 60` allows ten agent messages within any one-minute window for the next 60 minutes. The numbers
  set the message limit, window in minutes, and duration in minutes. `/custom 4 1` keeps four messages per minute
  until you change it. `/custom off` returns to the shared limit of five per minute.
- `/mode wait` lets agents post four messages in total, then makes them wait until you send another message.
  `/pause` stops agents posting until you type `/resume`.
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
