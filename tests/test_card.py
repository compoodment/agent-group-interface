"""Read the state card from a disposable AGI conversation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = os.environ.get("AGI_SCRATCH")


class CardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="agi-card-", dir=SCRATCH)
        self.env = dict(os.environ, AGI_DATA=self.folder.name, PYTHONPATH=str(ROOT))

    def tearDown(self):
        self.folder.cleanup()

    def agi(self, *args):
        done = subprocess.run([str(ROOT / "bin" / "agi"), *args], env=self.env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def card(self, agent="claude"):
        done = subprocess.run([sys.executable, "-P", "-m", "agi.card", "--as", agent], env=self.env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_a_fresh_chat_says_none_everywhere_and_keeps_the_rules(self):
        shown = self.card()
        self.assertIn("You hold: nothing", shown)
        self.assertIn("Up to you next: no to-dos", shown)
        self.assertIn("codex holds: nothing", shown)
        self.assertIn("New for you since #0: nothing", shown)
        self.assertIn("Rules that keep slipping:", shown)

    def test_it_shows_what_is_held_waiting_and_unread_and_only_for_that_agent(self):
        self.agi("say", "--from", "user", "please make the thing")                       # 1
        self.agi("take", "--as", "claude", "1")                                          # 2: claim
        self.agi("todo", "add", "--as", "claude", "--owner", "claude", "--source", "1", "--project", "AGI",
                 "--title", "Follow up the thing", "--next-action", "check it", "--next-actor", "claude")
        self.agi("todo", "add", "--as", "codex", "--owner", "codex", "--source", "1", "--project", "AGI",
                 "--title", "Codex's own thing", "--next-action", "do it", "--next-actor", "codex")
        self.agi("say", "--from", "codex", "@claude a question for you")
        self.agi("say", "--from", "codex", "a note for nobody in particular")
        shown = self.card()
        self.assertIn("#1 please make the thing", shown)
        self.assertIn("Follow up the thing", shown)
        self.assertNotIn("Codex's own thing", shown)                                     # someone else's
        self.assertIn("codex: @claude a question for you", shown)
        self.assertNotIn("nobody in particular", shown.split("Up to you next")[0])       # not new for me...
        self.assertIn("codex's last update: #", shown)                                   # includes the other agent's last update
        self.assertIn("Codex's own thing", self.card("codex"))

    def test_it_adds_direction_next_actions_decisions_and_the_other_agents_claims(self):
        newest = lambda: json.loads(self.agi("log", "--last", "1").splitlines()[-1])["id"]
        self.agi("say", "--from", "user", "first the site, then the release")
        direction = newest()
        self.agi("todo", "add", "--as", "claude", "--owner", "claude", "--source", str(direction), "--project", "AGI",
                 "--title", "Build the site", "--next-action", "render Install at four sizes", "--next-actor", "claude")
        self.agi("say", "--from", "user", "@codex please take the release")
        request = newest()
        self.agi("take", "--as", "codex", str(request))
        self.agi("decision", "ask", "--as", "claude", "--project", "AGI", "--choice", "1a", "--choice", "1b",
                 "Which name? 1a long 1b short")
        question = newest()
        self.agi("say", "--from", "user", f"↪ #{question} 1a")
        answer = newest()
        shown = self.card()
        self.assertIn(f"per #{direction}: first the site, then the release", shown)          # direction
        self.assertIn("next: render Install at four sizes", shown)                              # the next action
        self.assertIn(f"#{question} AGI: 1a (in #{answer})", shown)                            # the decision
        self.assertIn(f"codex holds:\n  #{request} @codex please take the release", shown)    # the other agent

    def test_a_reply_to_an_older_message_counts_as_unread(self):
        # The parent of an unread reply may predate the read position.
        self.agi("say", "--from", "codex", "which one?")                                 # 1
        self.agi("ack", "--as", "codex", "1")
        self.agi("say", "--from", "user", "↪ #1 the second")                            # 2
        self.assertIn("user: ↪ #1 the second", self.card("codex"))


    def test_next_leads_with_its_full_action_and_the_newer_direction_it_cites(self):
        # A marked to-do leads ahead of a future due date and shows the newer direction it cites.
        import time
        newest = lambda: json.loads(self.agi("log", "--last", "1").splitlines()[-1])["id"]
        self.agi("say", "--from", "user", "an old request from long ago")
        old = newest()
        self.agi("say", "--from", "user", "↪ #1 the release before the other project, please")
        newer = newest()
        action = (f"Verify the site against the release, then ask for the go; release before the other project "
                  f"(#{newer}). Only then publish, and only after checking every install link resolves.")
        self.agi("todo", "add", "--as", "claude", "--owner", "claude", "--source", str(old), "--project", "AGI",
                 "--title", "A future dated one", "--next-action", "later", "--next-actor", "claude",
                 "--due-at", str(int(time.time()) + 5 * 86400))
        added = json.loads(self.agi("todo", "add", "--as", "claude", "--owner", "claude", "--source", str(old),
                                    "--project", "AGI", "--title", "Ship the release", "--next-action", action,
                                    "--next-actor", "claude"))
        self.agi("todo", "next", "--as", "claude", str(added["id"]))
        self.agi("say", "--from", "user", "please hand this over")
        request = newest()
        self.agi("take", "--as", "codex", str(request))
        self.agi("release-task", "--as", "codex", str(request), "--to", "claude", "--note", "yours now",
                 "--result", "half", "--check", "read it", "--skills", "none, because a test")
        shown = self.card()
        first = shown.index("Up to you next")
        self.assertLess(shown.index("Ship the release", first), shown.index("A future dated one", first))
        self.assertIn("· Next", shown)
        self.assertIn("every install link resolves.", shown)                  # the condition, uncut
        self.assertIn(f"per #{newer}: the release before the other project", shown)
        self.assertNotIn(f"per #{old}:", shown.split("A future dated one")[0])   # the newer one governs, not the source
        self.assertIn(f"handed to you: #{request} please hand this over", shown)
        self.assertIn("AGI reference. Continue your current work and consult these records as needed.", shown)

if __name__ == "__main__":
    unittest.main()
