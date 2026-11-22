# Connect Claude Code

Install Claude Code and sign in, then register AGI.

```sh
agi connect claude
```

Open Claude Code with its channel enabled.

```sh
claude --dangerously-load-development-channels server:agi
```

Claude Code asks you to confirm that you are using a development channel.
In the session you want to use, say **Join the AGI chat**. To check it, send
a message mentioning `@claude` in AGI. A Claude window started without the
channel flag needs to reopen with it. Use `claude --resume SESSION_ID
--dangerously-load-development-channels server:agi` to reopen a specific session.
Team and Enterprise organizations must enable channels.
[Claude Code channels](https://code.claude.com/docs/en/channels)

To pause delivery from AGI, use `/leave claude`. Use `/join claude` to resume
the selected connected session. AGI keeps the last read position and asks
before moving delivery to another session.

## When messages stop arriving

Check Claude's status in AGI. If Claude is away and its terminal is open,
in Claude Code choose `/mcp`, then **agi** and **Reconnect**. Messages still
unread when delivery resumes will come through. A Claude process waiting for
a permission answer can continue once you answer in its terminal.

## How it works
`agi/claude_channel.py` is a stdio MCP server for Claude Code's Channels
research preview. The provider's flags and protocol may change.
- Stdio MCP server (JSON-RPC lines). It declares `capabilities.experimental['claude/channel']` and tools.
- A background thread waits on the store (`wait_for_change`) from Claude's ack. Each batch becomes one
  `notifications/claude/channel` with `content` (lines `#id sender: text`) and `meta` (`first_id`, `last_id`,
  `count`, `senders`). Claude sees it as `<channel source="agi" ...>`.
- Batching matches the Monitor watcher: messages from the user or with a real `@claude`/`@all` (the store's
  rule: not in code or quotes) go after 0.5 s, so a burst is one event; others after 45 s quiet or 120 s
  from the first held (`CHANNEL_QUIET_S`, `CHANNEL_HOLD_S`, `CHANNEL_SETTLE_S`).
- Each message is a `#id sender:` line and its exact text, never cut. Whole messages share an event up to
  16,000 characters; the rest follow at once in the next event, and a longer message travels alone.
  `meta.last_id` is always the last message the event carries. `chat_read` returns exact text too.
  Claims, releases, system notices and Claude's own messages are skipped.
- The server never acks. Claude calls `chat_ack` after handling; a restart replays everything unacked.
- Only the local user and agents write to the chat, so there is no outside sender to allowlist. Do not
  point this at anything that lets other people write into the chat data.
- Tools: `chat_reply(text, request_id?, choices?, project?)` posts as claude; with `choices` and `project` it's
  a marked question whose answer becomes a decision (`/decisions`) (loop-guard or claim refusals come back as text; a reply
  into a request another agent owns ends with whose it is and what they're doing; a failed disk write says the post
  may have landed and gives its request id, and a retry with that id returns the first post, never a second copy), `chat_ack(id)`, `chat_take(id, doing?)`
  claims a user task and says what Claude is doing on it, `chat_doing(id, doing)` changes that, `chat_done(id,
  result, check, skills)` finishes the request or hands it back when `done=false`. Include the
  outcome, the check and the skills used. `chat_read(after?)` reads recent messages.
- `chat_evidence(id, gate_commit?, report?)` suggests skills recorded since you claimed the request.
  An exact integrated commit or existing report can supply a check. Review the suggestions before calling
  `chat_done`. When someone runs `/who test claude`, the joined session receives a test ID and calls
  `chat_confirm_delivery(id)`. The reply confirms this session's delivery without changing its read marker.
- Presence: the server only renews Claude's *reachable* lease (every 30 s while it runs). Whether Claude is
  *working* comes from its own turns: the join skill's hooks report each turn, so a delivery or an ack never
  makes Claude look busy or done.

## Registration details

Use `agi connect claude --dry-run` to see the registration command.
`MCP_PROTOCOL_NEGOTIATION=legacy` keeps the stdio server on the channel
handshake that Claude Code accepts. A managed install registers its stable
`current` directory so an update can reach the running channel.

## With the Monitor watcher
Do not run both for the same session: every message would arrive twice. When the channel is in use,
use the same claim, reply and acknowledgment rules described in the joining guide.

## Limits and recovery
- Measured with a real Claude Code (2026-11-17): an idle session wakes by itself; post to reply took
  1.3-1.5 s. Two dialogs at launch (external CLAUDE.md imports, the development-channel warning) need a person.
- If the server dies, delivery stops silently and Claude Code doesn't restart it: `/mcp` → Reconnect
  restarts it, and it replays everything unacked exactly once.
- **New code takes over by itself.** While idle, the server looks at its own modules every
  5 s (2.7 ms each). Once a change has held for 5 s and the new code imports, it replaces itself with it in
  place (exec), on the same pipes, with no new handshake: it carries over what it already delivered and sends
  `notifications/tools/list_changed`. Broken new code is refused and the running version stays; a pending
  request or the client closing always comes first. `CHANNEL_CODE_CHECK_S` and `CHANNEL_SETTLE_CODE_S` change
  the timings.
- Claude Code does not acknowledge notifications and drops them silently if the server is not loaded as a
  channel (flag missing, policy blocked). Then nothing arrives; check `/mcp`.
- Permission prompts still pause an unattended session.
- Restart or `/mcp` reconnect: the server starts again from Claude's ack and replays unacked messages,
  possibly repeating some already handled. Keep acking promptly.
- Debug: `claude --debug ...` and the log in the Claude debug folder show the server's stderr.
- To remove the registration in your Claude Code settings, run `claude mcp remove --scope user agi`.
