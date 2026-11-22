"""Connect an agent client to this chat: `agi connect claude` or `agi connect codex`.

It registers AGI with the client through the client's own command (`claude mcp add-json`, `codex mcp add`), so a
change in the client's config format can't break it, with the paths filled in. A registration that already matches
is left alone, and a failed one puts back what was there: the chat's data folder and this install's
stable path. An install made by the installer lives in `<prefix>/versions/<version>/` with `<prefix>/current`
pointing at one of them; the registration names `current`, never a version folder, so an update reaches the next
start of every client, and Claude's channel follows the link while it runs. A plain checkout registers itself.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from agi import store

CLIENTS = {"claude": "Claude Code", "codex": "Codex"}
NAME = "agi"                                    # the server's name in each client; Claude's channel is server:agi
START_CLAUDE = "claude --dangerously-load-development-channels server:agi"


def stable_root(package=None):
    """The folder to register: `<prefix>/current` when this code runs from the version that link points at, else
    the checkout itself."""
    root = Path(package or Path(__file__).absolute().parent).parent
    real = root.resolve()
    link = real.parent.parent / "current"
    if real.parent.name == "versions" and link.is_symlink() and link.resolve() == real:
        return link
    return root


def find(client):
    """The client's command: on PATH, or where its own installer puts it (Claude's is ~/.local/bin, which a
    login shell doesn't always have on PATH)."""
    found = shutil.which(client)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / client
    return str(local) if local.is_file() else None


def wanted(client, root, data):
    """The registration this install needs: the command a client starts, its arguments and environment."""
    if client == "claude":
        return {"command": "python3", "args": ["-P", "-m", "agi.claude_channel"],
                # Claude Code's v2 runtime registers a channel only on the older handshake (docs/Claude-channel.md)
                "env": {"PYTHONPATH": str(root), "AGI_DATA": str(data), "MCP_PROTOCOL_NEGOTIATION": "legacy"}}
    return {"command": str(root / "bin" / "agi-mcp"), "args": ["--as", "codex"], "env": {"AGI_DATA": str(data)}}


def claude_settings():
    """Claude Code's user settings file, where `--scope user` servers live."""
    folder = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(folder) / ".claude.json" if folder else Path.home() / ".claude.json"


def registered(client, command, run):
    """The client's current registration of AGI as {command, args, env}, or None. Read only: Claude Code has no
    JSON output for one server, so its settings file is read; Codex prints it."""
    if client == "claude":
        try:
            found = json.loads(claude_settings().read_text()).get("mcpServers", {}).get(NAME)
        except (OSError, ValueError, AttributeError):
            return None
    else:
        shown = run([command, "mcp", "get", NAME, "--json"], stdin=subprocess.DEVNULL, capture_output=True, text=True)
        try:
            found = json.loads(shown.stdout).get("transport") if shown.returncode == 0 else None
        except (ValueError, AttributeError):
            return None
    if not isinstance(found, dict) or not isinstance(found.get("command"), str):
        return None
    return {"command": found["command"], "args": list(found.get("args") or []), "env": dict(found.get("env") or {})}


def matches(current, config):
    """The same command and arguments, with at least the environment AGI needs (other variables may stay)."""
    return (current is not None and current["command"] == config["command"] and current["args"] == config["args"]
            and all(current["env"].get(key) == value for key, value in config["env"].items()))


def remove(client, command):
    return [command, "mcp", "remove", *(["--scope", "user"] if client == "claude" else []), NAME]


def add(client, command, config):
    if client == "claude":
        # add-json, not add: add's --env takes every word after it, the server's name included (Claude Code
        # 2.1.323: "Invalid environment variable format: agi"); add-json won't overwrite, so remove comes first
        return [command, "mcp", "add-json", "--scope", "user", NAME, json.dumps({"type": "stdio", **config})]
    env = [part for key, value in config["env"].items() for part in ("--env", f"{key}={value}")]
    return [command, "mcp", "add", NAME, *env, "--", config["command"], *config["args"]]


def after(client, data, already=False):
    """What the person does next, in plain words."""
    connected = "Already connected" if already else "Connected"
    if client == "claude":
        return (f"{connected}: Claude Code and the chat in {data}.\n"
                "Start Claude with:\n\n"
                f"    {START_CLAUDE}\n\n"
                "Claude Code warns about development channels: AGI's channel isn't on Anthropic's approved list, "
                "and this flag is how your messages reach Claude while it works. Choose \"I am using this for local "
                "development\". Then ask Claude to join the chat.")
    return (f"{connected}: Codex and the chat in {data}.\n"
            "Start or reopen Codex to load AGI's tools, then ask it to join the chat.")


def connect(client, dry_run=False, run=subprocess.run, out=print):
    command = find(client)
    if command is None:
        out(f"{CLIENTS[client]} isn't installed here, or `{client}` isn't on your PATH. Install it, sign in once, "
            f"then run `agi connect {client}` again.")
        return 1
    root, data = stable_root(), store.DATA_DIR.absolute()
    config = wanted(client, root, data)
    quiet = dict(stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if dry_run:
        for step in (remove(client, command), add(client, command, config)):
            out(shlex.join(step))
        return 0
    current = registered(client, command, run)
    if matches(current, config):
        out(after(client, data, already=True))
        return 0
    run(remove(client, command), **quiet)                     # none registered yet is fine
    added = run(add(client, command, config), **quiet)
    if added.returncode != 0:
        detail = (added.stderr or added.stdout).strip()
        # a failed run never leaves you worse off: the registration that was there goes back as it was (Codex, #6004)
        restored = current is not None and run(add(client, command, current), **quiet).returncode == 0
        out(f"{CLIENTS[client]} refused the registration" + (f": {detail}" if detail else "") + ". " +
            ("Your earlier registration is back as it was." if restored else
             "Nothing was registered before." if current is None else
             f"Your earlier registration couldn't be put back; run `agi connect {client}` again."))
        return 1
    out(after(client, data))
    return 0


def state(client, run=subprocess.run):
    """What the chat screen shows before connecting (#6027): "missing" (client not installed), "none" (no AGI
    registration), "this" (registered to this install and chat), or "other" with the chat folder it names."""
    command = find(client)
    if command is None:
        return "missing", None
    current = registered(client, command, run)
    if current is None:
        return "none", None
    config = wanted(client, stable_root(), store.DATA_DIR.absolute())
    if matches(current, config):
        return "this", None
    return "other", current["env"].get("AGI_DATA")


def disconnect(client, root=None, run=subprocess.run):
    """Remove AGI from a client, for uninstalling: only when its registration points at this install (`root`), so
    removing one copy of AGI never disconnects another. Returns a sentence saying what happened."""
    command = find(client)
    if command is None:
        return f"{CLIENTS[client]} isn't installed: nothing to remove."
    current = registered(client, command, run)
    if current is None:
        return f"{CLIENTS[client]} had no AGI registration."
    root = str(root or stable_root())
    ours = (current["env"].get("PYTHONPATH") == root if client == "claude"
            else current["command"].startswith(root.rstrip("/") + "/"))
    if not ours:
        return f"{CLIENTS[client]}'s AGI registration belongs to another install; left as it is."
    removed = run(remove(client, command), stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if removed.returncode != 0:
        return f"{CLIENTS[client]} refused to remove AGI: {(removed.stderr or removed.stdout).strip()}"
    return f"Removed AGI from {CLIENTS[client]}."


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agi connect", description="Connect Claude Code or Codex to this chat.")
    parser.add_argument("client", choices=sorted(CLIENTS))
    parser.add_argument("--dry-run", action="store_true", help="print the client commands instead of running them")
    args = parser.parse_args(argv)
    return connect(args.client, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
