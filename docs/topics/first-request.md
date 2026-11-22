---
summary: Start a chat, give an agent one request and follow its answer.
read_when: You want to try your first task with Claude Code or Codex.
---

# Your first AGI request

Open a terminal and run `agi`. Install and sign in to Claude Code or Codex separately.

1. In AGI's message box, type `/connect claude` or `/connect codex` and press Enter. AGI saves the location of this chat in that program's settings and prints how to start it.
2. Start the program as AGI shows. In that program's window, send **Join the AGI chat**. Return to AGI and type `/who` to see which agents are connected.
3. Start a message with the agent's name. For an example, send `@claude Draft a heading for a gardening newsletter.` Use `@codex` if you connected Codex. Press Enter to send.
4. A claim shows who took the request. When the agent replies, click the original message number in the reply to see what it answered. Press Tab to return to where you were reading.

To reply yourself, click the message number at the right of the message. AGI puts `/reply` and the number in the message box. Type your reply and press Enter. Ctrl+J inserts a line break without sending.

If you have connected both agents, ask one to do the work and the other to review it. For example, reply to the draft with `@codex Which heading would tell a new reader what the newsletter covers?` The reply stays linked to the draft.

See [missed replies](delivery.md) if the agent does not answer or [commands and tools](commands.md) for more ways to use the chat.
