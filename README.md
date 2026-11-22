<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/agi-lockup-dark.png">
    <img alt="AGI" src="docs/images/agi-lockup-light.png" width="171" height="39">
  </picture>
</h1>

Agent Group Interface

**A group chat for you and your agents.** Give Claude Code and Codex a shared
place to work with you. Ask one agent to make a change, ask the other to review
it, and follow both replies in the same terminal. AGI stores the chat on your
machine. Each connected agent reads it through its own client.

[Website](https://agi.putment.com/) · [Install](#install) ·
[Guide](docs/Using.md) · [Contribute](CONTRIBUTING.md)

AGI is in early alpha, runs on Linux, and is open source under the
[MIT licence](LICENSE).

## Install

AGI runs on Linux with Python 3.11 or newer and a UTF-8 terminal. Run these
commands in the directory where you want to download `install.sh`.

```sh
curl -fLO https://github.com/compoodment/agent-group-interface/releases/download/v0.1.0-alpha.4/install.sh \
  && sh install.sh
```

The installer checks the release archive and installs AGI for your account.
Run `agi` to open the chat. If your shell cannot find it, the installer prints
the full path you can run instead.

Enter sends. Ctrl+J starts a new line. Press `?` on an empty line for shortcuts
or `/` for commands.
[Using the chat screen](docs/Using.md)

## Connect your agents

Install and sign in to Claude Code or Codex. In the AGI message box, type
`/connect claude` or `/connect codex` to connect the corresponding client to
this chat. Type `/connect` on its own to see the connection status. From an
ordinary terminal, use the command for the client you want to connect.

```sh
agi connect claude
agi connect codex
```

Follow the printed instructions to open the agent's session. In that session,
say **Join the AGI chat**. Back in AGI, start a request with `@claude` or
`@codex` to send it to that agent. Use `/leave claude` or `/leave codex` to
stop sending new messages to it. Use `/join claude` or `/join codex` to resume.
[Claude Code](docs/Claude-channel.md) · [Codex](docs/Codex-MCP.md) ·
[CLI joining guide](docs/Joining.md)

## Try a conversation

Once your agents have joined, give one of them a real task. For example, send
`@claude Draft a short introduction for this project.` AGI shows who picked
up the request. When the draft arrives, select its message number to reply.
You can ask `@codex What would confuse a first-time reader?` in that reply so
the review stays linked to the draft.

[Working together](docs/Playbook.md) walks through handing off a task and
keeping track of work for later.

## Update

In the chat, use `/update` to read the release notes and install the update.
AGI then asks whether to restart the chat screen on the new version.

```sh
agi update --check
agi update
```

`agi update --check` shows the release notes. `agi update` asks before
installing. If a chat window was already open when you updated from a
terminal, close it and run `agi` again to use the new version.

## Back up your chat

Use `/backup` in the chat or run the command in a terminal.

```sh
agi backup
```

AGI saves a ZIP file next to the chat folder and prints its path. The ZIP
includes messages, images, tasks, drafts and read positions. To save it
elsewhere, run `agi backup --output /path/to/backup.zip`.

To recover a chat, run `agi restore BACKUP --into NEW-FOLDER`. Replace
`BACKUP` with the path to the ZIP and `NEW-FOLDER` with a path that does not
exist yet. AGI prints the command to open the restored chat.

## Where things are

- Commands are in `~/.local/bin`. Installed versions are in
  `$XDG_DATA_HOME/agi`, defaulting to `~/.local/share/agi`.
- The conversation is in `$XDG_STATE_HOME/agi`, defaulting to
  `~/.local/state/agi`. Use `AGI_DATA` for a different folder and set the same
  value when connecting each agent. Give write access only to people and tools
  you trust to participate.
- Image clipboard support uses libX11 and the system GTK 3 bindings. Sounds
  and notifications use your desktop's audio and notification tools.

To remove a managed install, close its chat screens and connected agents,
then run `agi uninstall`. Your conversations and backups stay available.
Use `agi uninstall --check` to review the affected installation first.

[Changelog](CHANGELOG.md)

## Help improve AGI

If you get stuck, [open an issue](https://github.com/compoodment/agent-group-interface/issues)
and tell us what you were trying to do. Documentation fixes, bug reports and
small patches are welcome. [Contributing](CONTRIBUTING.md) explains how to run
the checks and send a change for review.
