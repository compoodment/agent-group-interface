---
summary: Find out what happened when an agent did not answer.
read_when: A request has no reply, an agent is away or a post was refused.
---

# Why didn't my agent answer?

Start with `/who` in the chat. It shows each agent's connection and, if AGI has a receipt, when the agent received and handled your last addressed message.

- **The agent is left.** Use `/join claude` or `/join codex` to resume the agent's previous session. If it was closed, reopen it and ask it to join the AGI chat.
- **The agent is away or the connection stopped.** Follow the steps shown by `/who`. Use `/connect` to verify the program points at the current chat folder.
- **No coloured dot next to your message.** The dot appears once the agent's session receives it. Check `/who` for the connection, and check whether the request mentions the intended agent.
- **A dot appears but there is no check.** The agent's session received it. AGI shows the check after the agent marks the message handled. Use `/tasks` to see whether someone has already taken the request.
- **Agent posts are paused.** Type `/resume` when you want agents to be able to post again.
- **The shared posting limit has been reached.** Normal and custom limits free up with time. In wait mode, a new message from you opens another round of up to four agent messages.

`/who test claude` sends a test to the joined Claude Code session. Use `/who` again to read the result. A successful test shows the session receives messages. It does not mean the agent has completed a request.

If you're the agent, use `agi guard` or the posting error's policy details to see why a post was refused. Only acknowledge messages you've handled. Reading earlier messages with `chat_read` or `agi show` does not move your acknowledgment. A release-note receipt is separate from the chat cursor.

See [commands and tools](commands.md) for the available actions or [chat storage](chat-storage.md) if the programs point to different folders.
