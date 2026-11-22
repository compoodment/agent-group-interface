"""The CLI and MCP agent read the same bundled help without touching chat data."""

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agi import __version__, claude_channel, cli, codex_mcp, help_docs
from agi.ui.composer import COMMAND_HELP


class HelpDocsTests(unittest.TestCase):
    def test_cli_and_tool_offer_the_same_installed_topics(self):
        server = codex_mcp.Server("codex")
        index = server.call("chat_help", {})
        self.assertEqual(index["version"], __version__)
        slugs = [row["topic"] for row in index["topics"]]
        self.assertEqual(slugs, sorted(set(slugs)))
        self.assertTrue({"chat-storage", "commands", "delivery", "first-request"} <= set(slugs))
        self.assertTrue(all(row["summary"] and row["read_when"] for row in index["topics"]))
        self.assertTrue(all("markdown" not in row for row in index["topics"]))

        with redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["docs"]), 0)
        self.assertIn(f"AGI {__version__}", output.getvalue())
        for slug in slugs:
            self.assertIn(slug, output.getvalue())

        topic = server.call("chat_help", {"topic": "delivery"})
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(cli.main(["docs", "delivery"]), 0)
        self.assertEqual(output.getvalue(), f"AGI {__version__}\n\n{topic['markdown']}\n")
        self.assertIn("# Why didn't my agent answer?", topic["markdown"])

    def test_lookup_does_not_follow_paths_or_touch_the_chat(self):
        with tempfile.TemporaryDirectory(prefix="agi-docs-test-", dir=os.environ.get("AGI_SCRATCH")) as temp:
            root = Path(temp)
            topic = root / "outside.md"
            topic.write_text("Private test content", encoding="utf-8")
            docs = root / "docs"
            docs.mkdir()
            (docs / "linked.md").symlink_to(topic)
            server = codex_mcp.Server("codex")
            with patch("agi.store._prepare", side_effect=AssertionError("docs touched the chat")):
                self.assertIn("topics", server.call("chat_help", {}))
                with patch.object(help_docs, "TOPICS", docs):
                    for name in ("../outside", "/outside.md", "linked", "unknown"):
                        with self.subTest(topic=name):
                            text, failed = server.call_tool("chat_help", {"topic": name})
                            self.assertTrue(failed)
                            self.assertNotIn("Private test content", text)
                            self.assertIn("error", json.loads(text))

    def test_reference_covers_the_cli_commands_menu_and_agent_tools(self):
        markdown = help_docs.lookup("commands")["markdown"]
        parser = cli._parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        self.assertEqual(set(re.findall(r"`agi ([a-z][a-z-]*)`", markdown)), set(commands.choices))
        self.assertEqual(set(re.findall(r"`(/[a-z]+)`", markdown)), set(COMMAND_HELP))
        registered_tools = {entry["name"] for entry in codex_mcp.TOOLS + claude_channel.TOOLS}
        self.assertEqual(set(re.findall(r"`(chat_[a-z_]+)`", markdown)), registered_tools)


if __name__ == "__main__":
    unittest.main()
