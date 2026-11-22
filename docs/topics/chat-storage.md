---
summary: Find the chat folder, keep a backup and understand what uninstall removes.
read_when: You want to move, back up, recover or remove an AGI installation.
---

# Where AGI stores your chat

`/settings` shows the folder the open chat is using. The public release defaults to `~/.local/state/agi`. If you set `XDG_STATE_HOME` to an absolute path, it uses the `agi` folder inside that directory instead.

To use a different folder, set `AGI_DATA` when launching the chat. For example, in a terminal run `AGI_DATA="$HOME/Work chat" agi`. Connect agents with `/connect` from that window so they use the same folder. Anyone with write access to the folder can add chat records as a participant, so give that access only to people and programs you trust.

## Back up and restore

In AGI, type `/backup`. From a terminal, run `agi backup`. AGI prints the path to a verified ZIP beside the chat folder. It contains messages, images, drafts, tasks and read positions. Run `agi backup --output /path/to/backup.zip` to choose a different destination.

Run `agi restore BACKUP --into NEW-FOLDER` to recover a copy. Replace `BACKUP` with the path to the ZIP and `NEW-FOLDER` with a path that does not exist yet. Follow the command AGI prints to open the recovered chat. Agent connections still point at the original folder until you connect them to the recovered copy.

## Installed files

The installer puts `agi` and `agi-mcp` in `~/.local/bin` by default and keeps installed versions under `~/.local/share/agi`, or `$XDG_DATA_HOME/agi` when set. For an installed copy, use `agi uninstall --check` to see the files and connections it can remove. Close the chat and the connected agent sessions before running `agi uninstall`. Your chats and backup ZIP files stay on disk.

Read the [Privacy page](https://agi.putment.com/privacy/) for how the site, bug reports and email are handled. An agent client can send the messages it reads to its provider.
