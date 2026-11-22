"""Claude Code channel for AGI (prototype, Claude Code Channels research preview).

An MCP stdio server (newline-delimited JSON-RPC 2.0). It declares the experimental
`claude/channel` capability and pushes unread chat messages into the session as
`notifications/claude/channel`. Claude answers through the chat_* tools. Only protocol
messages go to stdout; logs go to stderr. Stdlib only.

Batching matches skills/join-claude/watch.py: messages from the user or mentioning @claude/@all go
out at once; others after QUIET seconds without news, or HOLD seconds after the first held.
Nothing is acked here: Claude calls chat_ack after handling, so a restart replays unacked messages.
"""
import json
import os
import select
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from agi import __version__, delivery_probe, evidence, help_docs, membership, reminders, store, updates
from agi.mcp_stdio import MAX_REQUEST, StdioServer, tool

REACH_EVERY = 30        # seconds between reachability writes, however busy the inbox (USB writes, Codex #1526)
AGENT = "claude"
QUIET = float(os.environ.get("CHANNEL_QUIET_S", 45))
HOLD = float(os.environ.get("CHANNEL_HOLD_S", 120))
SETTLE = float(os.environ.get("CHANNEL_SETTLE_S", 0.5))   # an urgent message waits this long for the rest of a burst
SETTLE_MAX = 2.0
CODE_CHECK_S = float(os.environ.get("CHANNEL_CODE_CHECK_S", 5))    # idle seconds between looks at its own code
SETTLE_CODE = float(os.environ.get("CHANNEL_SETTLE_CODE_S", 5))    # a change must hold this long before a restart
EVENT_CHARS = 16000                 # whole messages up to this much text share one event; the rest go in the next
INSTRUCTIONS = (
    'AGI messages arrive as <channel source="agi" first_id=".." last_id=".." senders="..">: each message '
    'is a "#id sender:" line followed by its exact text (which may span lines), with a blank line between messages. '
    'Image attachment lines list the pixel dimensions and absolute local path. '
    'last_id is the last message carried. Reply with chat_reply only when mentioned (@claude, @all) or genuinely useful. '
    "Before starting a user request, call chat_take with its id; if it fails, leave it to the owner. When it's "
    "finished, call chat_done with its id. After handling, call chat_ack with the last id handled (never ack "
    "unhandled ids). Use chat_read for history. On joining, check chat_release_notes and acknowledge "
    "its version after reading. Messages arrive only while this session is joined: when the person asks you to "
    "join or leave the chat, call chat_join or chat_leave. " + help_docs.INSTRUCTIONS
)


def task_hint(hint):
    """After "posted #N": whose request the reply went into and what they're doing, when it isn't Claude's own."""
    if not hint:
        return ""
    doing = f", doing: {hint['doing']}" if hint.get("doing") else ""
    return f" · note: #{hint['id']} is {hint['owner']}'s{doing}. Check before acting on it"


def todo_hint(hint):
    """After "posted #N": a line that sounds like a promise for later, put as a question. Only a question: Claude checks
    whether it is a real commitment not already done or recorded. This hint creates nothing."""
    if not hint:
        return ""
    return (f" · question: \"{hint['candidate']}\" sounds like a commitment for later. If it is one and isn't done "
            f"or recorded yet: agi todo add --source {hint['source_id']} …")


def log(text):
    print(f"claude_channel: {text}", file=sys.stderr, flush=True)


def session_id(given):
    """Claude Code's session ID when it's one membership accepts, else a fresh random one."""
    return given if given and membership.SESSION_ID.fullmatch(given) else uuid.uuid4().hex


def block(message):
    """A message header followed by its text and any local image paths."""
    tag = "" if message["kind"] == "msg" else f"/{message['kind']}"
    text = message["text"]
    if (revoked := message.get("task_revocation")) and revoked.get("owner") == AGENT:   # said to the person;
        text = (f"The person took #{revoked['task_id']} back from you: you no longer hold it. "   # Claude needs it
                "Stop working on it unless they ask again.")                                     # the other way
    lines = [f"#{message['id']} {message['sender']}{tag}:", text]
    for ref in message.get("attachments", ()):
        lines.append(f"Image attachment {ref['width']} × {ref['height']}  {store.attachment_path(ref)}")
    return "\n".join(lines)


def split(batch):
    """The messages one event carries, whole, and the rest for the next event. A message longer than
    EVENT_CHARS travels alone and whole: an event never carries part of a message or points past itself."""
    used = 0
    for number, message in enumerate(batch):
        used += len(block(message)) + (2 if number else 0)   # the blank line between messages counts too
        if number and used > EVENT_CHARS:
            return batch[:number], batch[number:]
    return batch, []


def urgent(message):
    """The user, or a message addressed to Claude: a real @claude/@all in prose (quoted or code mentions don't
    count) or an answer to one of Claude's own messages, by the store's one rule (store.addressed_to)."""
    return (message["sender"] == "user" or store.addressed_to(message, AGENT)
            or taken_back(message))


def taken_back(message):
    """A claim of Claude's that the person released (/unclaim): Claude must hear it, or it keeps working on a
    request that's no longer its own (#6036)."""
    return (message.get("task_revocation") or {}).get("owner") == AGENT


TOOLS = [
    tool("chat_reply", "Post a message to AGI as claude. request_id: only when retrying a post that failed, the id its "
         "error gave, so it can't post twice. choices and project: a question with numbered choices (1a, 1b, 2a…) "
         "for the user. Their reply is recorded as a decision (/decisions).",
         {"text": {"type": "string"}, "request_id": {"type": "string"},
          "choices": {"type": "array", "items": {"type": "string"}}, "project": {"type": "string"}}, ["text"]),
    tool("chat_ack", "Acknowledge messages up to this id as handled.", {"id": {"type": "integer"}}, ["id"]),
    tool("chat_release_notes", "Read unread notes for the installed AGI release. After reading, pass its version to acknowledge.",
         {"version": {"type": "string", "minLength": 1}}),
    help_docs.TOOL,
    tool("chat_take", "Claim a user request by message ID before starting. Use doing to describe the current action, shown "
         "beside the request so nobody starts the same thing.", {"id": {"type": "integer"}, "doing": {"type": "string"}},
         ["id"]),
    tool("chat_doing", "Say what you're doing now on a request you own (one line, shown beside it).",
         {"id": {"type": "integer"}, "doing": {"type": "string"}}, ["id", "doing"]),
    tool("chat_done", "Finish a user request you took, or hand it back unfinished with done=false. Both need evidence. "
         "result = the outcome so far, check = the commit, test, decision or manual check "
         "that shows it, skills = the skills used, or 'none, because …'. chat_evidence lists the skills recorded since "
         "the claim.",
         {"id": {"type": "integer"}, "result": {"type": "string"}, "check": {"type": "string"},
          "skills": {"type": "string"}, "done": {"type": "boolean"}}, ["id", "result", "check", "skills"]),
    tool("chat_read", "Read recent messages (exact text), or those after an id. Does not ack.",
         {"after": {"type": "integer"}}),
    tool("chat_join", "Join the chat from this session: messages start arriving here. If another Claude session is "
         "joined, the person is asked in AGI to move Claude to this one.", {}),
    tool("chat_evidence", "Before chat_done: the skills actually recorded since you took request id (from skill-load "
         "receipts), and with gate_commit or report the check that shows it. Review them, then pass them to "
         "chat_done.", {"id": {"type": "integer"}, "gate_commit": {"type": "string"}, "report": {"type": "string"}},
         ["id"]),
    tool("chat_confirm_delivery", "Confirm an AGI delivery test: call it with the id the test message gave. It doesn't post "
         "anything or change your ack.", {"id": {"type": "string"}}, ["id"]),
    tool("chat_leave", "Leave the chat from this session: no more messages arrive here until you join again. Your "
         "claims stay yours.", {}),
]


def help_text(topic=None):
    """Show the running version and the topics or one topic's Markdown."""
    found = help_docs.lookup(topic)
    if topic is not None:
        return f"AGI {found['version']} · {found['topic']}\n\n{found['markdown']}"
    rows = "\n".join(f"- {item['topic']}: {item['title'].rstrip('.')}{'' if item['title'][-1:] in '?!.' else '.'} "
                     f"Read when {item['read_when'][:1].lower()}{item['read_when'][1:]}" for item in found["topics"])
    return f"AGI {found['version']} docs\n\n{rows}\n\nCall chat_help with a topic to read it."


class Channel(StdioServer):
    def __init__(self, out=None):
        super().__init__(out, TOOLS, name="agi", version=__version__, instructions=INSTRUCTIONS,
                         experimental={"claude/channel": {}}, fallback="2025-06-18", require_version=False)
        self.started = False
        self.reached = 0.0                  # when reachability was last renewed (monotonic)
        self.stopping = threading.Event()   # set when the client goes: the watcher ends instead of running on
        self.delivered = self.resumed_from = 0   # the newest message handed to Claude, kept across a restart in place
        # which Claude session this is, for joining and leaving (#5982, #6005): Claude Code's own session ID, which
        # it passes to its MCP servers and keeps when a session is resumed (claude --continue, --resume, the login
        # start), so after a reboot the same conversation is still the joined one with no approval (#6032); a
        # random one only when there's none, kept across a restart in place
        self.session = os.environ.get("AGI_CHANNEL_SESSION") or session_id(os.environ.get("CLAUDE_CODE_SESSION_ID"))
        self.away = False                   # set while not joined; the next joined pass starts again from the ack
        self.revision = None                # the membership revision deliveries are made under
        self.nudged = False                 # told once, while not joined, that messages for Claude are waiting
        self.preface = ""                   # "N messages arrived while this session was away", once after a rejoin
        self._handshake["capabilities"]["tools"]["listChanged"] = True   # after a restart in place, tools may differ

    def notify(self, batch):
        meta = {"first_id": str(batch[0]["id"]), "last_id": str(batch[-1]["id"]),
                "count": str(len(batch)),
                "senders": ",".join(dict.fromkeys(m["sender"] for m in batch))}
        preface, self.preface = self.preface, ""          # once, on the first batch after a rejoin
        self.send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                   "params": {"content": preface + "\n\n".join(block(m) for m in batch), "meta": meta}})

    # --- watcher -------------------------------------------------------------
    def watch(self):
        try:
            delivered = store.get_ack(AGENT)
        except Exception as error:
            log(f"cannot read ack: {error}")
            delivered = 0
        delivered = self.delivered = max(delivered, self.resumed_from)   # a restart in place re-sends nothing
        held, first, last, called = [], 0.0, 0.0, None   # called: when the first urgent message was held

        def due():
            soon = min(first + HOLD, last + QUIET)
            return soon if called is None else min(soon, last + SETTLE, called + SETTLE_MAX)

        while not self.stopping.is_set():
            now = time.monotonic()
            wait = 25.0 if not held else max(0.05, due() - now)
            if self.away:                           # a join isn't a new message: look for it every second
                wait = min(wait, 1.0)
            if now - self.reached >= REACH_EVERY:   # reachable while this server runs; whether Claude is working
                for renew in (lambda: store.set_reachable(AGENT, ttl=90),   # whether Claude is working comes from
                              lambda: membership.observe(AGENT, self.session, ttl=90)):   # its own turns (hooks)
                    try:                            # best effort, each on its own: a failed write mustn't keep
                        renew()                     # messages back, nor the other lease from renewing
                    except Exception as error:
                        log(f"reachability not renewed: {error}")
                self.reached = now
            try:
                store.wait_for_change(delivered, timeout=reminders.wait_limit(min(wait, 25.0)),
                                      wake_when=lambda: delivery_probe.pending_for(self.session))
                rows = store.read_messages(delivered)
            except Exception as error:
                log(f"store error: {error}")
                time.sleep(5)
                continue
            if not self.joined():                   # left, or another session is joined: nothing goes out
                if rows:
                    delivered = self.delivered = rows[-1]["id"]
                held, called, self.away = [], None, True
                self.nudge(rows)
                continue
            self.nudged = False
            self.answer_probe()                     # /who test claude: one test, only to the joined session
            if self.away:                           # back: everything since the ack, at once
                self.away = False
                delivered = self.delivered = self.ack()
                rows = store.read_messages(delivered)
                called = time.monotonic()
                waiting = self.waiting()
                self.preface = (f"{waiting} message{'s' if waiting != 1 else ''} arrived while this session was "
                                "away.\n\n" if waiting else "")
            fresh = []
            for message in rows:
                delivered = self.delivered = max(delivered, message["id"])
                reminder = any(key in message for key in ("task_reminder_ids", "todo_ready", "todo_check", "todo_answer"))
                if taken_back(message) or (message["sender"] != AGENT
                                           and (message["kind"] == "msg" or message.get("todo_ready"))
                                           and (not reminder or urgent(message))):
                    fresh.append(message)
            now = time.monotonic()
            if fresh:
                first = first if held else now
                last = now
                held += fresh
                if called is None and any(urgent(m) for m in fresh):
                    called = now            # soon, but a burst arriving together still wakes once
            if held and now >= due():
                batch, held = split(held)   # anything left goes out next, straight away: it was due too
                if not held:
                    called = None
                if not self.still_joined():         # left (or left and came back) while it waited: the rejoin
                    held, called, self.away = [], None, True   # starts again from the ack
                    continue
                try:
                    self.notify(batch)
                except Exception as error:          # stdout is gone: the client closed this server
                    log(f"notify failed: {error}")
                    return
                try:                                # when the wake went out, for /who; diagnostic only, so a
                    store.woke(AGENT, batch[-1]["id"], first_id=batch[0]["id"])   # failed write can't stop delivery
                except Exception as error:
                    log(f"wake receipt not written: {error}")

    # --- tools ---------------------------------------------------------------
    def call_tool(self, name, args):
        try:
            if name == "chat_reply":
                text = args.get("text")
                if not isinstance(text, str):
                    return "error: text must be a string", True
                # every post carries a request id: a write can land before an error (a failed directory sync), and
                # the retry with the same id returns that post instead of a second copy (Codex's audit, #5554)
                request_id = args.get("request_id") or uuid.uuid4().hex
                try:
                    choices, project = args.get("choices"), args.get("project")
                    if choices is not None and not project:
                        return "error: a question with choices needs its project (AGI, Artlab, …)", True
                    prompt = {"project": project, "choices": choices} if choices is not None else None
                    row = store.append_message(AGENT, text, request_id=request_id, with_hints=True,
                                               decision_prompt=prompt)
                except OSError as error:        # a refusal (guard, bad text) posted nothing: those stay plain errors
                    return (f"error: {error} · it may have been posted; to retry without a second copy, send the same "
                            f"text with request_id {request_id}"), True
                return f"posted #{row['id']}{task_hint(row.get('task_hint'))}{todo_hint(row.get('todo_hint'))}", False
            if name == "chat_ack":
                store.set_ack(AGENT, args.get("id"))   # the inbox cursor only: work status is Claude's turns
                return f"acked {args['id']}{self.holding()}", False
            if name == "chat_release_notes":
                value = (dict(acked=updates.acknowledge_release(AGENT, args["version"])) if "version" in args
                         else help_docs.release_notes(AGENT))
                return json.dumps(value, ensure_ascii=False), False
            if name == "chat_help":
                try:
                    return help_text(args.get("topic")), False
                except ValueError as error:
                    return str(error), True
            if name == "chat_take":
                claim = store.take_task(AGENT, args.get("id"), doing=args.get("doing") or None)
                return f"claimed task {args.get('id')} (#{claim['id']})", False
            if name == "chat_doing":
                store.set_task_doing(AGENT, args.get("id"), args.get("doing"))
                return f"doing on #{args.get('id')}: {args.get('doing')}", False
            if name == "chat_done":
                done = args.get("done", True)
                store.release_task(AGENT, args.get("id"), done=done, result=args.get("result"),
                                   check=args.get("check"), skills=args.get("skills"))
                return f"{'finished' if done else 'handed back'} task {args['id']}{self.holding()}", False
            if name == "chat_join":
                membership.observe(AGENT, self.session, ttl=90)
                state = membership.request_join(AGENT, self.session)
                if state["joined"] and state["session"] == self.session:
                    waiting = self.waiting()
                    return ("joined: messages arrive here now" +
                            (f"; {waiting} waiting since your last ack" if waiting else "")), False
                return "asked to join: another Claude session is joined, so the person decides in AGI", False
            if name == "chat_evidence":
                found = evidence.suggest(AGENT, args.get("id"), gate_commit=args.get("gate_commit"),
                                         report=args.get("report"))
                skills = ", ".join(found["skills"]) or "none recorded since the claim"
                check = found["check"] or "(none given: pass gate_commit or report, or write the check yourself)"
                return f"skills: {skills}\ncheck: {check}\nReview both, then call chat_done with them.", False
            if name == "chat_confirm_delivery":
                delivery_probe.confirm(self.session, args.get("id"))
                return "delivery test confirmed: the person sees that messages reach this session", False
            if name == "chat_leave":
                membership.leave(AGENT, self.session)
                return f"left: no messages arrive here until chat_join{self.holding()}", False
            if name == "chat_read":
                after = args.get("after")
                rows = store.read_messages(after, limit=50) if after is not None else store.read_tail(20)
                return "\n\n".join(block(m) for m in rows) or "no messages", False
        except (store.GuardBlocked, store.ClaimConflict, ValueError, TypeError) as error:
            return f"refused: {error}", True
        except Exception as error:  # keep the server alive on any store failure
            return f"error: {error}", True
        raise KeyError(name)

    @staticmethod
    def holding():
        """A reminder of the requests Claude still holds: claims left behind once the work was done kept
        piling up on the task board."""
        try:
            held = [f"#{task['id']}" for task in store.list_tasks()
                    if task["state"] == "claimed" and task["owner"] == AGENT]
        except Exception:                    # only a reminder: never the reason an ack fails
            return ""
        return f" · still holding {' '.join(sorted(held))}: chat_done when finished" if held else ""

    def ack(self):
        try:
            return store.get_ack(AGENT)
        except Exception as error:
            log(f"cannot read ack: {error}")
            return 0

    def joined(self):
        """Whether the chat delivers to this session now; a failed read keeps messages back rather than
        sending them to a session that may have left. A new membership revision while still joined means it left
        and came back between two looks (Codex, #6047): that's a rejoin, so it starts again from the ack."""
        try:
            state = membership.status(AGENT)
        except Exception as error:
            log(f"membership not read: {error}")
            return False
        if not (state["joined"] and state["session"] == self.session):
            return False
        if state["revision"] != self.revision:
            if self.revision is not None:
                self.away = True
            self.revision = state["revision"]
        return True

    def nudge(self, rows):
        """Once, when no Claude session was ever chosen and messages for Claude arrive: tell this session so, without
        the messages. A session that already left, or another joined session, never receives this notice."""
        waiting = [m for m in rows if m["sender"] != AGENT and m["kind"] == "msg" and urgent(m)]
        if self.nudged or not waiting:
            return
        try:
            if membership.status(AGENT)["session"] is not None:
                return
        except Exception:
            return
        self.nudged = True
        try:
            self.send({"jsonrpc": "2.0", "method": "notifications/claude/channel", "params": {
                "content": (f"{len(waiting)} AGI message{'s' if len(waiting) != 1 else ''} for you "
                            f"(from #{waiting[0]['id']}) are waiting, but no Claude session has joined the chat. "
                            "If this session should be in it, call chat_join; they arrive then."),
                "meta": {"count": str(len(waiting)), "first_id": str(waiting[0]["id"]), "joined": "false"}}})
        except Exception as error:
            log(f"nudge not sent: {error}")

    def answer_probe(self):
        """Send one requested delivery test to the joined session and ask for confirmation."""
        try:
            probe = delivery_probe.take_pending(self.session, self.revision) if self.revision else None
        except Exception as error:
            log(f"delivery test not read: {error}")
            return
        if probe is None or not self.still_joined():   # the same check as a delivery, at the moment of sending
            return                                     # (Codex, #6113)
        try:
            self.send({"jsonrpc": "2.0", "method": "notifications/claude/channel", "params": {
                "content": f"AGI delivery test: the person is checking that messages reach you. Call chat_confirm_delivery with "
                           f"id {probe} now; nothing else is needed.",
                "meta": {"delivery_test": probe}}})
        except Exception as error:
            log(f"delivery test not sent: {error}")

    def still_joined(self):
        """At the moment of sending: the same membership the batch was prepared under."""
        try:
            return membership.can_deliver(AGENT, self.session, revision=self.revision)
        except Exception as error:
            log(f"membership not read: {error}")
            return False

    def waiting(self):
        """How many messages from others arrived after the ack: what a rejoin brings."""
        try:
            return sum(1 for m in store.read_messages(self.ack()) if m["sender"] != AGENT and m["kind"] == "msg")
        except Exception:
            return 0

    def on_initialized(self):
        if not self.started:
            self.started = True
            threading.Thread(target=self.watch, daemon=True).start()

    def serve(self, stdin=None):
        try:
            if stdin is None:
                self._serve_restarting()
            else:
                super().serve(stdin)
        finally:
            self.stopping.set()             # stdin closed: the client is gone

    # --- staying current -----------------------------------------------------
    # Between requests, check whether the installed modules have changed. After the files settle and a
    # test import succeeds, replace the running process while retaining its connection.

    def _serve_restarting(self):
        fd, buffer = sys.stdin.fileno(), b""
        stamp, changed_at = code_stamp(), None
        while True:
            while b"\n" not in buffer:
                ready, _, _ = select.select([fd], [], [], CODE_CHECK_S)
                if ready:                   # input, or the client closing, always comes before a reload
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        return
                    buffer += chunk
                    if len(buffer) > MAX_REQUEST:
                        self.error(None, -32700, 'Request too large')
                        return
                    continue
                if buffer:                  # half a request read: never reload under it
                    continue
                now_stamp = code_stamp()
                if now_stamp != stamp:      # still changing (a merge writes file by file): wait for it to settle
                    stamp, changed_at = now_stamp, time.monotonic()
                elif changed_at is not None and time.monotonic() - changed_at >= SETTLE_CODE:
                    changed_at = None
                    if imports_cleanly():
                        self._restart()
                    log("new code doesn't import; staying on the running version")
            raw, buffer = buffer.split(b"\n", 1)
            if not raw.strip():
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                self.error(None, -32700, 'Parse error')
                continue
            self.handle(message)             # a tool call runs to its answer before the loop can reload

    def _restart(self):
        with self._write_lock:              # never mid-way through a notification
            log("code changed on main: restarting in place")
            sys.stdout.flush()
            env = dict(os.environ, AGI_CHANNEL_RESUMED="1", AGI_CHANNEL_DELIVERED=str(self.delivered),
                       AGI_CHANNEL_SESSION=self.session)
            os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], env)


def code_stamp():
    """The channel's code as it is on disk now: every module it can import, by size and change time."""
    root = Path(__file__).resolve().parent
    stamps = []
    for path in sorted(root.rglob("*.py")):
        try:
            info = path.stat()
        except OSError:                     # renamed away mid-merge: counts as changing
            continue
        stamps.append((str(path), info.st_mtime_ns, info.st_size))
    return tuple(stamps)


def imports_cleanly():
    """Whether the code on disk now starts: a merge that broke it must not take the channel down with it."""
    root = Path(__file__).resolve().parent.parent
    try:
        return subprocess.run([sys.executable, "-P", "-c", "import agi.claude_channel"], cwd=root, timeout=30,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main():
    channel = Channel()
    if os.environ.pop("AGI_CHANNEL_RESUMED", None):   # replaced in place: the client's handshake already happened
        channel.initialized = True
        channel.resumed_from = int(os.environ.pop("AGI_CHANNEL_DELIVERED", "0") or 0)
        log(f"now running {__version__} from {Path(__file__).resolve().parent.parent}")
        channel.on_initialized()
        channel.send(dict(jsonrpc="2.0", method="notifications/tools/list_changed"))   # the new code's tools
    channel.serve()


if __name__ == "__main__":
    main()
