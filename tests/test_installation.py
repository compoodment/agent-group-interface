"""Exercise managed installs, updates and removal in disposable directories."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import __version__, cli, connect, installation, release_assets, store


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agi-install-", dir=os.environ.get("AGI_SCRATCH"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.prefix = self.root / "app"
        self.commands = self.root / "commands"
        self.data = self.root / "conversation"
        self.env = {**os.environ, "AGI_DATA": str(self.data), "PYTHONDONTWRITEBYTECODE": "1"}
        self.wrapper = installation.SOURCE / "bin/agi"
        self.installed = self.commands / "agi"
        self.installed_root = self.prefix / "versions" / __version__

    def run_agi(self, command, *arguments, expected=0):
        result = subprocess.run([command, *arguments], env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout

    def install(self):
        return self.run_agi(self.wrapper, "install", "--prefix", self.prefix, "--bin-dir", self.commands)

    def test_cli_installs_both_commands_and_is_repeatable_without_data(self):
        self.assertIn(str(self.installed), self.install())
        self.install()
        self.assertEqual((self.prefix / "current").resolve(), self.installed_root)
        self.assertEqual(self.run_agi(self.installed, "--version").strip(), f"AGI {__version__}")
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {}}}
        result = subprocess.run([self.commands / "agi-mcp", "--as", "codex"], input=json.dumps(request) + "\n",
                                env=self.env, text=True, capture_output=True, timeout=15, check=True)
        self.assertEqual(json.loads(result.stdout)["result"]["serverInfo"]["version"], __version__)
        self.assertFalse(self.data.exists())

    def test_conflict_preserves_the_existing_launcher_and_leaves_no_install(self):
        self.commands.mkdir()
        self.installed.write_text("existing command")
        self.run_agi(self.wrapper, "install", "--prefix", self.prefix, "--bin-dir", self.commands, expected=1)
        self.assertEqual(self.installed.read_text(), "existing command")
        self.assertFalse(self.prefix.exists())

    def test_retry_refuses_broken_version_left_before_activation(self):
        self.install()
        (self.prefix / "current").unlink()
        (self.installed_root / "bin/agi").chmod(0o644)
        self.run_agi(self.wrapper, "install", "--prefix", self.prefix, "--bin-dir", self.commands, expected=1)
        self.assertFalse((self.prefix / "current").is_symlink())
        (self.installed_root / "bin/agi").chmod(0o755)
        self.install()
        self.assertEqual((self.prefix / "current").resolve(), self.installed_root)

    def uninstall(self, **options):
        with (mock.patch.object(installation, "SOURCE", self.installed_root),
              mock.patch.object(store, "DATA_DIR", self.data), redirect_stdout(io.StringIO()) as output):
            installation.uninstall(**options)
        return output.getvalue()

    def test_uninstall_previews_then_preserves_data_and_other_registration(self):
        self.install()
        cache = self.installed_root / "agi" / "__pycache__"
        cache.mkdir()
        (cache / "cli.cpython-313.pyc").write_bytes(b"generated Python cache")
        (self.prefix / "notes").write_text("keep this")
        self.data.mkdir()
        (self.data / "conversation.txt").write_text("chat history")
        backup = self.root / "backup.zip"
        backup.write_bytes(b"backup bytes")
        settings = self.root / "claude.json"
        claude_registration = connect.wanted("claude", self.prefix / "current", self.data)
        settings.write_text(json.dumps({"mcpServers": {"agi": claude_registration}}))
        foreign_registration = connect.wanted("codex", self.root / "another install", self.data)

        def provider_run(command, **kwargs):
            if command[1:3] == ["mcp", "get"]:
                stdout = json.dumps({"transport": foreign_registration})
            else:
                self.assertEqual(command, ["claude", "mcp", "remove", "--scope", "user", "agi"])
                settings.write_text(json.dumps({"mcpServers": {}}))
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with (mock.patch.object(connect, "find", side_effect=lambda client: client),
              mock.patch.object(connect, "claude_settings", return_value=settings),
              mock.patch.object(installation.subprocess, "run", side_effect=provider_run) as run):
            self.assertIn("Conversation data and backups stay in place", self.uninstall(check=True))
            with (mock.patch.object(sys.stdin, "isatty", return_value=True),
                  mock.patch("builtins.input", return_value="no")):
                self.uninstall()
            run.assert_not_called()
            self.assertTrue(self.installed.is_symlink())
            self.assertEqual(json.loads(settings.read_text())["mcpServers"]["agi"], claude_registration)

            output = self.uninstall(yes=True)
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["claude", "mcp", "remove", "--scope", "user", "agi"], ["codex", "mcp", "get", "agi", "--json"],
            ])
        self.assertIn("Removed AGI from Claude Code", output)
        self.assertIn("Codex's AGI registration belongs to another install", output)
        self.assertEqual(json.loads(settings.read_text()), {"mcpServers": {}})
        self.assertEqual((self.data / "conversation.txt").read_text(), "chat history")
        self.assertEqual(backup.read_bytes(), b"backup bytes")
        self.assertEqual((self.prefix / "notes").read_text(), "keep this")
        self.assertFalse((self.prefix / "versions").exists())
        self.assertFalse((self.prefix / "current").is_symlink())
        self.assertFalse(self.installed.is_symlink())
        self.assertFalse((self.commands / "agi-mcp").is_symlink())

    def test_uninstall_refuses_an_unknown_file_inside_a_version(self):
        self.install()
        backup = self.installed_root / "docs" / "extra-backup.zip"
        backup.write_bytes(b"user data")
        with mock.patch.object(connect, "disconnect") as disconnect:
            with self.assertRaisesRegex(ValueError, "cannot verify the installed files"):
                self.uninstall(yes=True)
            disconnect.assert_not_called()
        self.assertEqual(backup.read_bytes(), b"user data")
        self.assertTrue(self.installed.is_symlink())

    def test_uninstall_restores_the_launcher_when_a_late_process_is_seen(self):
        self.install()
        with (mock.patch.object(installation, "_require_stopped", side_effect=[None, ValueError("late process")]),
              mock.patch.object(connect, "disconnect") as disconnect):
            with self.assertRaisesRegex(ValueError, "late process"):
                self.uninstall(yes=True)
            disconnect.assert_not_called()
        self.assertEqual(self.run_agi(self.installed, "--version").strip(), f"AGI {__version__}")

    def test_uninstall_refuses_a_running_managed_python_process(self):
        self.install()
        env = {**self.env, "PYTHONPATH": str(self.prefix / "current")}
        script = "import agi, sys; print(agi.__version__, flush=True); sys.stdin.read()"
        with subprocess.Popen([sys.executable, "-P", "-B", "-c", script], cwd=self.root, env=env,
                              text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), __version__)
                with mock.patch.object(connect, "disconnect") as disconnect:
                    with self.assertRaisesRegex(ValueError, "Close AGI and its connected agent sessions"):
                        self.uninstall(yes=True)
                    disconnect.assert_not_called()
            finally:
                child.stdin.close()
                child.wait(timeout=10)
        self.assertTrue(self.installed.is_symlink())

    def test_uninstall_retains_the_install_when_a_client_refuses(self):
        self.install()
        settings = self.root / "claude.json"
        settings.write_text(json.dumps({"mcpServers": {
            "agi": connect.wanted("claude", self.prefix / "current", self.data),
        }}))
        refusal = subprocess.CompletedProcess([], 1, stdout="", stderr="configuration is locked")
        with (mock.patch.object(connect, "find", return_value="claude"),
              mock.patch.object(connect, "claude_settings", return_value=settings),
              mock.patch.object(installation.subprocess, "run", return_value=refusal)):
            with self.assertRaisesRegex(ValueError, "configuration is locked"):
                self.uninstall(yes=True)
        self.assertEqual(self.run_agi(self.installed, "--version").strip(), f"AGI {__version__}")

    def remote(self, version):
        source = self.root / "source"
        source.mkdir()
        installation._copy_runtime(installation.SOURCE, source)
        (source / "VERSION").write_text(version + "\n", encoding="ascii")
        name = f"agent-group-interface-{version}.tar.gz"
        archive = self.root / name
        with tarfile.open(archive, "w:gz") as output:
            output.add(source, arcname=name.removesuffix(".tar.gz"))
        payload = archive.read_bytes()
        asset = {"name": name, "state": "uploaded", "size": len(payload),
                 "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                 "browser_download_url": f"{release_assets.DOWNLOADS}/v{version}/{name}"}
        records = [{"tag_name": f"v{version}", "draft": False, "prerelease": True,
                    "published_at": "2026-11-22T12:00:00Z", "assets": [asset], "body": "New release."}]
        return payload, records

    def test_verified_update_preserves_data_and_previous_version(self):
        self.install()
        version = "999.0.0-alpha.1"
        payload, records = self.remote(version)
        def open_url(url):
            return io.BytesIO(json.dumps(records).encode() if url.startswith(release_assets.RELEASES) else payload)

        first = json.loads(self.run_agi(self.installed, "say", "--from", "user", "first"))
        self.run_agi(self.installed, "ack", "--as", "codex", str(first["id"]))
        self.run_agi(self.installed, "say", "--from", "user", "next")
        before = {p.relative_to(self.data): p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        with mock.patch.object(installation, "SOURCE", self.installed_root), mock.patch.object(
                release_assets, "_open", side_effect=open_url), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["update", "--check"]), 0)
            self.assertEqual((self.prefix / "current").resolve(), self.installed_root)
            with mock.patch.object(sys.stdin, "isatty", return_value=False), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["update"]), 1)
            self.assertEqual((self.prefix / "current").resolve(), self.installed_root)
            original = records[0]["assets"][0]["digest"]
            records[0]["assets"][0]["digest"] = "sha256:" + "0" * 64
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["update", "--yes"]), 1)
            self.assertIn("checksum", errors.getvalue())
            self.assertEqual((self.prefix / "current").resolve(), self.installed_root)
            self.assertFalse((self.prefix / "versions" / version).exists())
            records[0]["assets"][0]["digest"] = original
            self.assertEqual(cli.main(["update", "--yes"]), 0)
        after = {p.relative_to(self.data): p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual((self.prefix / "current").resolve().name, version)
        self.assertEqual(self.run_agi(self.installed, "--version").strip(), f"AGI {version}")
        self.assertEqual(self.run_agi(self.installed_root / "bin/agi", "--version").strip(), f"AGI {__version__}")
        unread = self.run_agi(self.installed, "wait", "--as", "codex", "--timeout", "0")
        self.assertEqual([json.loads(line)["text"] for line in unread.splitlines()], ["next"])


if __name__ == "__main__":
    unittest.main()
