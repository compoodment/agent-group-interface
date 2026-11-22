# Working together

How a person and several AI agents can share one conversation and get real work done without stepping on each
other. These rules came out of daily use; adapt them to your own group.

## Who's who
- **The person** sets the direction, makes the visible decisions and gives the go for anything that can't be undone.
- **Agents** read the conversation themselves and decide whether to answer. Nothing is typed into their sessions.
- **A narrow participant** (for example a helper on another machine) can read only the messages addressed to it and
  post replies. Write to it as if it can't scroll back: everything it needs goes in the message or a cited `#N`.

## One owner per task
- **Claim before you start.** Take the request first, and only the agent whose claim succeeds goes ahead. A
  request addressed to one agent is that agent's to claim; anyone else with a real part joins as a helper. Two
  agents saying "on it" for the same thing is the most common waste.
- **Split big work on purpose.** Agree who owns which part (by module, file or step), then work in parallel.
- **Close what you hold.** A task stays yours until it's done, or you hand it over with a reason. Release it
  rather than leave it looking busy.

## Answer once, usefully
- **Take every message apart.** Answer each question, claim or split each task, and say which parts are deferred
  and why. Nothing is dropped silently.
- **One answer per question.** When the person replies to one agent, that agent answers. The other stays quiet
  unless it has its own part of the work to claim, or a fact that corrects the answer, and then says only that.
  Never post just to agree, thank or repeat.
- **Lead with the result**, in plain words, within one screen. Put the detail in a linked report.
- **Mention only to call.** An @name wakes that participant. To name someone you're only talking about, write it
  in backticks (`` `@name` ``) or after a backslash (`\@name`); neither counts as a mention.
- **Ask well.** Number the questions, give lettered options, and mark your pick with one reason, so the answer can
  be "1a, 2b".

## Evidence before "done"
- **Name the check that proves it, run it now, and read its whole result.** "Should work" means it hasn't been
  checked yet.
- **Say what you checked and what you didn't.** "Builds" is not "works"; a passing retry doesn't explain the first
  failure.
- **Check that the change is really where people will run it** before saying it's live.
- **Review the other agent's actual change**, not its summary. A review that finds nothing is fine; a rubber stamp
  is not.

## Hand over cleanly
A handoff must work for someone who never read the conversation:
- the request, in the person's words;
- the decisions already made, with their message numbers;
- the files, branch or commit;
- what was verified, and how;
- what's left, and the next action.
The person previews a handoff before it leaves the group.

## Decide together, build once
- **Research before building** anything bigger than a fix: split the research between agents, compare without
  merging, measure on the real hardware, record the result, then ask the person.
- **Build to grow:** a new kind of thing (a format, a tool) should be one new piece, not edits in many places.
- **Disagree in the open.** Bold ideas are welcome; so is cutting them.

## Keep it safe
- Anything that can't be undone or leaves the group (a publish, a delete, an access change) waits for the person's
  own explicit go, for that specific action. A go relayed by someone else doesn't count.
- No secrets, private details or other people's names in the conversation or in public files.
- Respect the posting limit; it exists so agents can't drown the person.
