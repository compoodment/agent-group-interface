"""agi connect: registers AGI with Claude Code or Codex through the client's own command, with the right paths."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agi import connect  # noqa: E402

# A stand-in client that keeps its registrations like the real ones: Claude Code in ~/.claude.json (user scope,
# add-json refuses a name that exists), Codex printed by `mcp get --json`. Every call is recorded as a JSON line;
# an add whose words contain $REFUSE_IF is refused.
FAKE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as calls:
    calls.write(json.dumps([Path(sys.argv[0]).name, *args]) + "\\n")
claude = Path(sys.argv[0]).name == "claude"
state = Path.home() / (".claude.json" if claude else "codex.json")
saved = json.loads(state.read_text()) if state.exists() else {}
servers = saved.setdefault("mcpServers", {})
refuse = os.environ.get("REFUSE_IF")
if args[:2] == ["mcp", "get"]:
    if "agi" not in servers:
        sys.exit(1)
    print(json.dumps({"name": "agi", "transport": dict(servers["agi"], type="stdio")}))
elif args[:2] == ["mcp", "remove"]:
    if servers.pop("agi", None) is None:
        sys.exit(1)
elif args[:2] in (["mcp", "add-json"], ["mcp", "add"]):
    if refuse and refuse in " ".join(args):
        print("no thanks", file=sys.stderr)
        sys.exit(1)
    if "agi" in servers:
        print("MCP server agi already exists", file=sys.stderr)
        sys.exit(1)
    if claude:
        servers["agi"] = json.loads(args[-1])
    else:
        env, rest = {}, args[3:]
        while rest[0] == "--env":
            key, _, value = rest[1].partition("=")
            env[key], rest = value, rest[2:]
        servers["agi"] = {"command": rest[1], "args": rest[2:], "env": env}
state.write_text(json.dumps(saved))
"""


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="agi-connect-", dir=os.environ.get("AGI_SCRATCH")))
        self.addCleanup(shutil.rmtree, self.folder)
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        self.home = self.folder / "home"
        self.home.mkdir()
        self.data = self.folder / "data"
        self.calls = self.folder / "calls"

    def client(self, name, where=None):
        path = (where or self.bin) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE)
        path.chmod(0o755)

    def run_connect(self, *args, code=ROOT, **env):
        environment = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.home),
                           AGI_DATA=str(self.data), CALLS=str(self.calls), PYTHONPATH=str(code))
        environment.update(env)
        return subprocess.run([sys.executable, "-P", "-m", "agi.connect", *args], env=environment, cwd=self.folder,
                              capture_output=True, text=True, timeout=30)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def registration(self, client):
        state = self.home / (".claude.json" if client == "claude" else "codex.json")
        return json.loads(state.read_text())["mcpServers"].get("agi") if state.exists() else None

    def test_claude_is_registered_for_every_folder_with_the_channel_and_the_data(self):
        self.client("claude")
        done = self.run_connect("claude")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([call[:4] for call in self.recorded()],
                         [["claude", "mcp", "remove", "--scope"], ["claude", "mcp", "add-json", "--scope"]])
        self.assertEqual(self.registration("claude"), {
            "type": "stdio", "command": "python3", "args": ["-P", "-m", "agi.claude_channel"],
            "env": {"PYTHONPATH": str(ROOT), "AGI_DATA": str(self.data), "MCP_PROTOCOL_NEGOTIATION": "legacy"}})
        self.assertIn(connect.START_CLAUDE, done.stdout)                 # the one line to start Claude with
        self.assertIn("I am using this for local development", done.stdout)

    def test_codex_is_registered_with_the_adapter_and_the_data(self):
        self.client("codex")
        done = self.run_connect("codex")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.registration("codex"), {
            "command": str(ROOT / "bin" / "agi-mcp"), "args": ["--as", "codex"], "env": {"AGI_DATA": str(self.data)}})
        self.assertIn("reopen Codex", done.stdout)

    def test_a_matching_registration_is_left_alone(self):
        for client in ("claude", "codex"):
            with self.subTest(client):
                self.client(client)
                self.assertEqual(self.run_connect(client).returncode, 0)
                before = len(self.recorded())
                again = self.run_connect(client)
                self.assertEqual(again.returncode, 0)
                self.assertIn("Already connected", again.stdout)
                self.assertFalse([c for c in self.recorded()[before:] if c[2] in ("remove", "add", "add-json")])

    def test_a_refused_registration_puts_the_earlier_one_back(self):
        # Codex (#6004): a failed run must never leave you with no connection
        for client in ("claude", "codex"):
            with self.subTest(client):
                self.client(client)
                old = self.folder / "old-data"
                self.assertEqual(self.run_connect(client, AGI_DATA=str(old)).returncode, 0)
                earlier = self.registration(client)
                done = self.run_connect(client, REFUSE_IF=str(self.data))
                self.assertEqual(done.returncode, 1)
                self.assertIn("refused the registration: no thanks", done.stdout)
                self.assertIn("Your earlier registration is back as it was.", done.stdout)
                self.assertEqual(self.registration(client), earlier)

    def test_an_install_registers_its_current_link_never_a_version_folder(self):
        # the update switches `current`; a registration naming the version folder would keep the old code
        # (tests/test_claude_channel.py: test_an_update_that_moves_the_current_link_takes_over)
        prefix = self.folder / "share" / "agi"
        version = prefix / "versions" / "0.1.0-alpha.9"
        shutil.copytree(ROOT / "agi", version / "agi", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "bin", version / "bin")
        for name in ("VERSION", "CHANGELOG.md"):
            shutil.copy(ROOT / name, version / name)
        (prefix / "current").symlink_to("versions/0.1.0-alpha.9")
        self.client("claude")
        self.client("codex")
        self.assertEqual(self.run_connect("claude", code=prefix / "current").returncode, 0)
        self.assertEqual(self.run_connect("codex", code=version).returncode, 0)  # even when started by its folder
        self.assertEqual(self.registration("claude")["env"]["PYTHONPATH"], str(prefix / "current"))
        self.assertEqual(self.registration("codex")["command"], str(prefix / "current" / "bin" / "agi-mcp"))

    def test_disconnect_removes_only_this_installs_registration(self):
        # for uninstall (#6039): removing one copy of AGI never disconnects another
        code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from agi import connect; "
                "print(connect.disconnect(sys.argv[1], sys.argv[2]))")
        for client in ("claude", "codex"):
            with self.subTest(client):
                self.client(client)
                self.assertEqual(self.run_connect(client).returncode, 0)

                def disconnect(root):
                    environment = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.home),
                                       CALLS=str(self.calls))
                    return subprocess.run([sys.executable, "-P", "-c", code, client, str(root)], env=environment,
                                          capture_output=True, text=True, timeout=30).stdout.strip()

                self.assertIn("belongs to another install", disconnect(self.folder / "elsewhere"))
                self.assertIsNotNone(self.registration(client))
                self.assertEqual(disconnect(ROOT), f"Removed AGI from {connect.CLIENTS[client]}.")
                self.assertIsNone(self.registration(client))
                self.assertIn("had no AGI registration", disconnect(ROOT))

    def test_a_plain_checkout_registers_itself(self):
        self.assertEqual(connect.stable_root(ROOT / "agi"), ROOT)

    def test_claude_installed_in_local_bin_is_found_off_the_path(self):
        self.client("claude", where=self.home / ".local" / "bin")
        done = self.run_connect("claude")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNotNone(self.registration("claude"))

    def test_a_missing_client_says_what_to_do_and_changes_nothing(self):
        done = self.run_connect("codex")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Codex isn't installed here", done.stdout)
        self.assertEqual(self.recorded(), [])

    def test_a_first_registration_that_is_refused_says_nothing_was_there(self):
        self.client("codex")
        done = self.run_connect("codex", REFUSE_IF="agi-mcp")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Codex refused the registration: no thanks. Nothing was registered before.", done.stdout)
        self.assertNotIn("reopen Codex", done.stdout)

    def test_a_dry_run_prints_the_commands_and_runs_nothing(self):
        self.client("claude")
        done = self.run_connect("claude", "--dry-run")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.recorded(), [])
        lines = done.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("mcp add-json --scope user agi", lines[1])


if __name__ == "__main__":
    unittest.main()
