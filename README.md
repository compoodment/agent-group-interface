# AGI

Agent Group Interface

A group chat for you and your agents. Ask for work, see who picked it up, and
follow replies and results in the same terminal. The bundled integrations
support Claude Code and Codex.

## Install

AGI runs on Linux with Python 3.11 or newer and a UTF-8 terminal. Download and
run the installer for this release.

```sh
curl -fLO https://github.com/compoodment/agent-group-interface/releases/download/v0.1.0-alpha.4/install.sh \
  && sh install.sh
```

The installer verifies the release archive and installs AGI for your user.
Run `agi` to open the chat. If your shell cannot find it, the installer prints
the command's full path.

Enter sends. Ctrl+J starts a new line. Press `?` on an empty line for shortcuts
or `/` for commands.
[Using the chat screen](docs/Using.md)

## Connect your agents

Install and sign in to Claude Code or Codex. In AGI, run `/connect claude` or
`/connect codex` to register the client for this chat. `/connect` shows which
clients are installed and where they are registered. You can also connect
from a terminal.

```sh
agi connect claude
agi connect codex
```

Each command prints how to start the client. In the session you want to use,
say **Join the AGI chat**. Mention that agent in AGI to send it a request.
Use `/leave claude` or `/leave codex` to pause delivery and `/join` with the
agent's name to return to a connected session.
[Claude Code](docs/Claude-channel.md) · [Codex](docs/Codex-MCP.md) ·
[CLI joining guide](docs/Joining.md)

## Update

In the chat, use `/update` to review available notes and confirm installation.
The chat screen offers to reopen on the new version once it finishes saving.

```sh
agi update --check
agi update
```

The first command shows available release notes. The second reviews the release
and asks before installing it. AGI verifies the downloaded archive and switches
to the new code in one step. Reopen the chat screen to use it. Conversation
files stay where they are. Open Claude Code or Codex again when you want to
load refreshed tools.

## Back up your chat

Use `/backup` in the chat or run the command in a terminal.

```sh
agi backup
```

AGI prints the path to a verified ZIP beside the conversation directory. It
includes the messages, images, tasks, drafts and read positions. Use
`agi backup --output PATH` to choose the file. To recover a copy, run
`agi restore BACKUP --into NEW-FOLDER` and follow the printed command.
The destination must be new so your existing conversation is safe.

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

## Run the checks

```sh
python3 -m unittest discover -s tests -v
python3 tests/terminal_probe.py bin/agi
```

Tests use disposable data. Set `AGI_SCRATCH` to choose where temporary files go.
Private-display clipboard tests use Xvfb when it is available.
[Data and command contract](docs/CONTRACT.md)
