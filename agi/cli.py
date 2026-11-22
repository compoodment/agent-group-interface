"""Command-line entry point. Owner: Codex; UI is imported only when needed."""

import argparse
import json
import math
import shlex
import sys
import time

from agi import gate_reminders, store, todo, __version__


def _todo_parser(commands):
    parser = commands.add_parser("todo", help="Record and review deferred commitments")
    actions = parser.add_subparsers(dest="todo_action", required=True)
    listing = actions.add_parser("list", help="List open commitments")
    listing.add_argument("--all", action="store_true", help="Include completed commitments")
    selection = actions.add_parser("next", help="Choose one next to-do for an owner")
    selection.add_argument("--as", dest="actor", choices=todo.PARTICIPANTS, required=True)
    selection.add_argument("id", type=int, nargs="?")
    selection.add_argument("--clear", action="store_true", help="Clear the current choice")
    selection.add_argument("--owner", choices=todo.PARTICIPANTS, help="Owner to clear, if different from the actor")
    selection.add_argument("--request-id", help="Stable ID for retrying the same operation")
    personal = actions.add_parser("add-personal", help="Add your own to-do without a source message")
    personal.add_argument("title")
    personal.add_argument("--due-at", type=float, help="Deadline in Unix seconds")
    for action in ("add", "update", "done"):
        command = actions.add_parser(action)
        command.add_argument("--as", dest="actor", choices=todo.PARTICIPANTS, required=True)
        command.add_argument("--request-id", help="Stable ID for retrying the same operation")
        if action == "add":
            command.add_argument("--source", type=int, required=True, help="Source chat message id")
            command.add_argument("--follow-request", action="store_true", default=argparse.SUPPRESS,
                                 help="Follow the claim and completion of the source request")
        else:
            command.add_argument("id", type=int)
            command.add_argument("--revision", type=int, required=True, help="Current revision from todo list")
        if action == "done":
            command.add_argument("--result", required=True, help="Outcome and supporting evidence")
            continue
        required = action == "add"
        command.add_argument("--project", required=required, default=argparse.SUPPRESS)
        command.add_argument("--title", required=required, default=argparse.SUPPRESS)
        command.add_argument("--owner", choices=todo.PARTICIPANTS, required=required, default=argparse.SUPPRESS)
        command.add_argument("--next-action", required=required, default=argparse.SUPPRESS)
        command.add_argument("--next-actor", choices=todo.PARTICIPANTS, default=argparse.SUPPRESS)
        command.add_argument("--due-at", type=float, default=argparse.SUPPRESS, help="Deadline in Unix seconds")
        command.add_argument("--remind-next-actor", action=argparse.BooleanOptionalAction,
                             default=argparse.SUPPRESS, help="Send the deadline to the next actor")
        command.add_argument("--reference", default=argparse.SUPPRESS)
        wait = command.add_mutually_exclusive_group()
        wait.add_argument("--after-todo", type=int, metavar="ID", help="Wait for another to-do to finish")
        wait.add_argument("--after-main", nargs=2, metavar=("PROJECT", "SHA"), help="Wait for an exact commit on main")
        wait.add_argument("--after-release", nargs=2, metavar=("PROJECT", "TAG"), help="Wait for a public GitHub release")
        if action == "update":
            wait.add_argument("--clear-wait", action="store_true", help="Clear the pending prerequisite")
            for field in ("next_actor", "due_at", "reference"):
                command.add_argument("--clear-" + field.replace("_", "-"), dest=field,
                                     action="store_const", const=None, default=argparse.SUPPRESS)


def _todo_command(args):
    if args.todo_action == "list":
        return store.list_todos(include_done=args.all)
    if args.todo_action == "add-personal":
        return store.add_personal_todo(args.title, due_at=args.due_at)
    if args.todo_action == "next":
        if (args.clear and args.id is not None) or (not args.clear and args.id is None):
            raise ValueError("Give one to-do ID or --clear")
        return store.set_next_todo(args.actor, args.id, owner=args.owner, request_id=args.request_id)
    if args.todo_action == "done":
        return store.complete_todo(args.actor, args.id, args.revision, args.result, request_id=args.request_id)
    fields = {key: value for key, value in vars(args).items() if key in todo.FIELDS}
    if args.after_todo is not None:
        fields["wait_for"] = f"todo:{args.after_todo}"
    if args.after_main or args.after_release:
        project, target = args.after_main or args.after_release
        kind = "gate" if args.after_main else "release"
        fields["wait_for"] = f"{kind}:{gate_reminders.project_id(project)}:{target}"
    if getattr(args, "clear_wait", False):
        fields["wait_for"] = None
    if args.todo_action == "add":
        return store.add_todo(args.actor, args.source, request_id=args.request_id, **fields)
    return store.update_todo(args.actor, args.id, args.revision, request_id=args.request_id, **fields)


def _decision_parser(commands):
    parser = commands.add_parser("decision", help="Ask a marked question or list its recorded choices")
    actions = parser.add_subparsers(dest="decision_action", required=True)
    actions.add_parser("list", help="Show marked questions and recorded choices")
    ask = actions.add_parser("ask", help="Send a question with explicit choice codes")
    ask.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    ask.add_argument("--project", required=True)
    ask.add_argument("--choice", action="append", required=True, help="Repeat for each code, such as 1a and 1b")
    ask.add_argument("--request-id")
    ask.add_argument("text")


def _parser():
    parser = argparse.ArgumentParser(prog="agi", description="A tiny local agent group chat")
    parser.add_argument("--version", action="version", version=f"AGI {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("ui", help="Open the user's chat screen")
    docs = commands.add_parser("docs", help="Read the installed AGI documentation")
    docs.add_argument("topic", nargs="?", metavar="TOPIC", help="Topic name. Omit to list topics")
    install = commands.add_parser("install", help="Install AGI for this user")
    install.add_argument("--prefix", metavar="PATH", help="Directory for installed versions")
    install.add_argument("--bin-dir", metavar="PATH", help="Directory for the agi and agi-mcp commands")
    update = commands.add_parser("update", help="Check for and install an AGI release")
    update.add_argument("--version", metavar="VERSION", help="Select a specific newer release")
    confirmation = update.add_mutually_exclusive_group()
    confirmation.add_argument("--check", action="store_true", help="Show what's available without installing")
    confirmation.add_argument("--yes", action="store_true", help="Apply the update without a prompt")
    uninstall = commands.add_parser("uninstall", help="Remove this managed installation")
    removal = uninstall.add_mutually_exclusive_group()
    removal.add_argument("--check", action="store_true", help="Show what would be removed")
    removal.add_argument("--yes", action="store_true", help="Remove this installation without a prompt")
    backup = commands.add_parser("backup", help="Export a verified ZIP of the conversation")
    backup.add_argument("--output", metavar="PATH", help="Write a new backup to this file")
    restore = commands.add_parser("restore", help="Recover a backup into a new conversation directory")
    restore.add_argument("backup", metavar="BACKUP")
    restore.add_argument("--into", required=True, metavar="PATH", help="New conversation directory")
    connect = commands.add_parser("connect", help="Register AGI with an installed agent")
    connect.add_argument("provider", choices=("claude", "codex"))
    connect.add_argument("--dry-run", action="store_true", help="Show the registration command")
    session = commands.add_parser("session", help="See, resume or pause an agent session")
    session.add_argument("action", choices=("status", "join", "leave"))
    session.add_argument("agent", choices=store.AGENTS)
    test_delivery = commands.add_parser("delivery-test", help="Run one agent delivery test or check its result")
    test_delivery.add_argument("agent", choices=store.AGENTS)
    test_delivery.add_argument("--status", action="store_true", help="Read the result of the requested test")
    evidence = commands.add_parser("task-evidence", help="Suggest evidence for a task you hold")
    evidence.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    evidence.add_argument("id", type=int)
    evidence.add_argument("--project", default="AGI")
    check = evidence.add_mutually_exclusive_group()
    check.add_argument("--gate", metavar="COMMIT", help="Use the integrated receipt for this exact commit")
    check.add_argument("--report", metavar="PATH", help="Refer to this existing local report")
    say = commands.add_parser("say", help="Append a message")
    say.add_argument("--from", dest="sender", choices=(store.USER,) + store.AGENTS, required=True)
    say.add_argument("--kind", choices=("msg", "claim", "release"), default="msg")
    say.add_argument("--request-id", help="Retry the same message with the same 32-character lowercase hex ID")
    say.add_argument("--attach", metavar="PNG", action="append", default=[], help="Attach a local PNG file")
    say.add_argument("text", nargs="*", metavar="TEXT")
    wait = commands.add_parser("wait", help="Wait for messages after an agent's last ack")
    wait.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    wait.add_argument("--timeout", type=float, default=25)
    wait.add_argument("--compact", action="store_true", help="Print compact JSON arrays with optional attachments")
    ack = commands.add_parser("ack", help="Acknowledge messages after handling them")
    ack.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    ack.add_argument("id", type=int)
    status = commands.add_parser("status", help="Report actual work or idle status")
    status.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    status.add_argument("state", choices=("working", "idle"))
    status.add_argument("--note", default="")
    status.add_argument("--ttl", type=float, default=900, help="Seconds the observation remains valid")
    heartbeat = commands.add_parser("heartbeat", help="Refresh reachability without changing work status")
    heartbeat.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    heartbeat.add_argument("--ttl", type=float, default=90)
    custom = commands.add_parser("custom", help="Set a custom message budget")
    custom.add_argument("limit", type=int)
    custom.add_argument("window_minutes", type=float)
    custom.add_argument("duration_minutes", type=float, nargs="?",
                        help="Optional duration in minutes. Omit to keep the budget until changed.")
    mode = commands.add_parser("mode", help="Choose a posting policy")
    mode.add_argument("mode", choices=("normal", "wait", "paused", "resume"))
    commands.add_parser("pause", help="Pause agent posts")
    commands.add_parser("resume", help="Resume the previous posting policy")
    commands.add_parser("guard", help="Show guard mode and any retry delay")
    show = commands.add_parser("show", help="Read a full message without changing acknowledgments")
    show.add_argument("id", type=int)
    log = commands.add_parser("log", help="Read a bounded recent history without acknowledging it")
    log.add_argument("--last", type=int, default=20, metavar="N")
    search = commands.add_parser("search", help="Search full history without changing acknowledgments")
    search.add_argument("text", nargs="+", metavar="TEXT")
    search.add_argument("--last", type=int, default=50, metavar="N")
    search.add_argument("--before", type=int, dest="before_id", metavar="ID")
    for command, help_text in [("take", "Atomically claim a user request before starting work"),
                               ("doing", "Update the action on your active task"),
                               ("release-task", "Release ownership of a request"),
                               ("defer-task", "Move a request's remaining work to its open to-dos"),
                               ("done", "Mark an owned request complete"),
                               ("join-task", "Join another agent's task as a helper"),
                               ("leave-task", "Leave a task's helper role")]:
        task = commands.add_parser(command, help=help_text)
        task.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
        task.add_argument("id", type=int, help="User message id")
        if command in ("release-task", "defer-task", "done"):
            task.add_argument("--result", required=True, help="Outcome so far")
            task.add_argument("--check", required=True, help="Commit, test, decision or manual check")
            task.add_argument("--skills", required=True, help="Skills applied, or 'none, because …'")
        if command == "take":
            task.add_argument("--doing", help="What you are starting on this task")
        elif command == "doing":
            task.add_argument("doing", help="What you are doing on this task now")
        elif command == "release-task":
            task.add_argument("--note", help="Optional handoff note for the next owner")
            task.add_argument("--to", choices=store.AGENTS, help="Reserve the next claim for this agent")
        elif command == "defer-task":
            task.add_argument("--todo", type=int, required=True, action="append", metavar="TODO_ID")
            task.add_argument("--note", help="Optional explanation of what is deferred")
        elif command == "join-task":
            task.add_argument("--todo", type=int, metavar="TODO_ID", help="Link your open to-do for this task")
    remind = commands.add_parser("remind", help="Schedule a one-shot reminder for a claimed task")
    remind.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    remind.add_argument("id", type=int, help="User task message id")
    trigger = remind.add_mutually_exclusive_group(required=True)
    trigger.add_argument("--in", dest="after_seconds", type=float, metavar="SECONDS", help="Seconds from now")
    trigger.add_argument("--after-gate", nargs=2, metavar=("PROJECT", "SHA"),
                         help="When the exact AGI or Artlab commit reaches main")
    remind.add_argument("--note", help="Optional reminder detail")
    remind.add_argument("--request-id", help="Stable reminder id (32 lowercase hexadecimal characters)")
    cancel_reminder = commands.add_parser("cancel-reminder", help="Cancel a task reminder")
    cancel_reminder.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
    cancel_reminder.add_argument("id", type=int, help="User task message id")
    cancel_reminder.add_argument("--request-id", help="Cancel this reminder id instead of the task's active reminder")
    _todo_parser(commands)
    skill = commands.add_parser("skill", help="Read a skill and keep its local receipt")
    skill_actions = skill.add_subparsers(dest="skill_action", required=True)
    for action in ("read", "record", "list"):
        command = skill_actions.add_parser(action)
        command.add_argument("--as", dest="agent", choices=store.AGENTS, required=True)
        if action != "list":
            command.add_argument("name", metavar="NAME")
    whatsnew = commands.add_parser("whatsnew", help="Read the installed release notes or acknowledge a version")
    whatsnew.add_argument("--as", dest="reader", choices=(store.USER, *store.AGENTS), required=True)
    whatsnew.add_argument("--ack", metavar="VERSION", help="Acknowledge this version after reading the notes")
    _decision_parser(commands)
    bridge = commands.add_parser("bridge", help="Manage the same-session Codex watcher")
    bridge.add_argument("action", choices=("start", "status", "stop", "refresh", "retry", "probe", "confirm", "accepts"))
    bridge.add_argument("wake_id", nargs="?")
    bridge.add_argument("--thread")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command in (None, "ui"):
            from agi import ui
            return ui.main()
        if args.command == "docs":
            from agi import help_docs
            print(help_docs.format_cli(args.topic))
        elif args.command == "install":
            from agi import installation
            installation.install(args.prefix, args.bin_dir)
        elif args.command == "update":
            from agi import installation
            installation.update(check=args.check, yes=args.yes, requested=args.version)
        elif args.command == "uninstall":
            from agi import installation
            installation.uninstall(check=args.check, yes=args.yes)
        elif args.command == "backup":
            from agi import backup
            print(backup.create(args.output))
        elif args.command == "restore":
            from agi import backup
            recovered = backup.restore(args.backup, args.into)
            print(f"Open the recovered copy with AGI_DATA={shlex.quote(str(recovered))} agi")
            print("Agent registrations still point at their original conversation.")
        elif args.command == "connect":
            from agi import connect
            return connect.main([args.provider] + (["--dry-run"] if args.dry_run else []))
        elif args.command == "session":
            from agi import bridge, membership
            if args.action == "join":
                value = bridge.resume() if args.agent == "codex" else membership.resume(args.agent)
            elif args.action == "leave":
                value = membership.leave(args.agent)
            else:
                value = membership.status(args.agent)
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "delivery-test":
            from agi import bridge, delivery_probe
            if args.agent == "claude":
                value = delivery_probe.status() if args.status else delivery_probe.request()
            else:
                value = bridge.status() if args.status else bridge.probe()
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "task-evidence":
            from agi import evidence
            print(json.dumps(evidence.suggest(args.agent, args.id, gate_commit=args.gate,
                                              report=args.report, project=args.project), ensure_ascii=False))
        elif args.command == "say":
            if args.attach and args.kind != "msg":
                raise ValueError("Attachments require a participant message")
            if len(args.attach) > store.MAX_ATTACHMENTS:
                raise ValueError(f"A message can have up to {store.MAX_ATTACHMENTS} image attachments")
            images = [store.attach(path) for path in args.attach]
            message = store.append_message(args.sender, " ".join(args.text), args.kind,
                                           request_id=args.request_id, attachments=images, with_hints=True)
            print(json.dumps(store.delivery_message(message), ensure_ascii=False))
        elif args.command == "bridge":
            from agi import bridge
            if args.action == "start":
                if args.wake_id is not None:
                    raise ValueError("Start requires --thread, not a wake id")
                value = bridge.start(args.thread)
            elif args.action in ("confirm", "accepts"):
                if args.thread is not None:
                    raise ValueError(f"{args.action} accepts a wake id, not --thread")
                if args.action == "accepts":
                    return 0 if bridge.accepts(args.wake_id) else 2
                value = bridge.confirm(args.wake_id)
            else:
                if args.thread is not None or args.wake_id is not None:
                    raise ValueError("Only start accepts --thread. Confirm and accepts take a wake id.")
                value = getattr(bridge, args.action)()
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "take":
            print(json.dumps(store.take_task(args.agent, args.id, doing=args.doing), ensure_ascii=False))
        elif args.command == "doing":
            print(json.dumps(store.set_task_doing(args.agent, args.id, args.doing), ensure_ascii=False))
        elif args.command in ("release-task", "defer-task", "done"):
            evidence = dict(result=args.result, check=args.check, skills=args.skills)
            if args.command == "defer-task":
                value = store.defer_task(args.agent, args.id, args.todo, note=args.note, **evidence)
            else:
                value = store.release_task(args.agent, args.id, done=args.command == "done",
                                           note=getattr(args, "note", None), handoff_to=getattr(args, "to", None),
                                           **evidence)
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "join-task":
            value = store.join_task(args.agent, args.id, todo_id=args.todo)
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "leave-task":
            print(json.dumps(store.leave_task(args.agent, args.id), ensure_ascii=False))
        elif args.command == "remind":
            if args.after_gate is not None:
                project, commit = args.after_gate
                value = store.schedule_gate_reminder(args.agent, args.id, project, commit,
                                                     note=args.note, reminder_id=args.request_id)
            else:
                if not math.isfinite(args.after_seconds) or args.after_seconds < 0:
                    raise ValueError("--in must be a finite non-negative number of seconds")
                value = store.schedule_reminder(args.agent, args.id, time.time() + args.after_seconds,
                                                note=args.note, reminder_id=args.request_id,
                                                preserve_deadline=args.request_id is not None)
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "cancel-reminder":
            value = store.cancel_reminder(args.agent, args.id, reminder_id=args.request_id)
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "todo":
            print(json.dumps(_todo_command(args), ensure_ascii=False))
        elif args.command == "skill":
            from agi import skills
            if args.skill_action == "list":
                print(json.dumps(skills.recent(args.agent), ensure_ascii=False))
            else:
                contents, receipt = skills.read(args.agent, args.name)
                if args.skill_action == "record":
                    print(json.dumps(receipt, ensure_ascii=False))
                else:
                    sys.stdout.buffer.write(contents)
        elif args.command == "decision":
            if args.decision_action == "ask":
                prompt = {"project": args.project, "choices": args.choice}
                value = store.append_message(args.agent, args.text, request_id=args.request_id, decision_prompt=prompt)
            else:
                value = store.list_decisions()
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "ack":
            store.set_ack(args.agent, args.id)
        elif args.command == "whatsnew":
            from agi import help_docs, updates
            value = (dict(acked=updates.acknowledge_release(args.reader, args.ack)) if args.ack is not None
                     else help_docs.release_notes(args.reader))
            print(json.dumps(value, ensure_ascii=False))
        elif args.command == "status":
            print(json.dumps(store.set_presence(args.agent, args.state, args.note, ttl=args.ttl), ensure_ascii=False))
        elif args.command == "heartbeat":
            print(json.dumps(store.set_reachable(args.agent, ttl=args.ttl)))
        elif args.command == "custom":
            duration = args.duration_minutes
            print(json.dumps(store.set_policy("custom", limit=args.limit,
                             window_s=args.window_minutes * 60,
                             duration_s=duration * 60 if duration is not None else None)))
        elif args.command in ("mode", "pause", "resume"):
            mode = args.mode if args.command == "mode" else ("paused" if args.command == "pause" else "resume")
            print(json.dumps(store.resume_policy() if mode == "resume" else store.set_policy(mode)))
        elif args.command == "guard":
            print(json.dumps(store.guard_status()))
        elif args.command == "show":
            if args.id < 1:
                raise ValueError("Message id must be positive")
            messages = store.read_messages(args.id - 1, limit=1)
            if not messages or messages[0]["id"] != args.id:
                raise ValueError(f"Message #{args.id} does not exist")
            print(json.dumps(store.delivery_message(messages[0]), ensure_ascii=False))
        elif args.command in ("log", "search"):
            if not 0 <= args.last <= 1000:
                raise ValueError("--last must be between 0 and 1000")
            messages = (store.read_tail(args.last) if args.command == "log" else
                        store.search_messages(" ".join(args.text), args.last, args.before_id))
            for message in messages:
                print(json.dumps(store.delivery_message(message), ensure_ascii=False))
        elif args.command == "wait":
            after = store.get_ack(args.agent)
            if args.timeout < 0 or not math.isfinite(args.timeout):
                raise ValueError("timeout must be a finite non-negative number")
            deadline = time.monotonic() + args.timeout
            while True:
                if args.timeout > 0:
                    store.set_reachable(args.agent)
                messages = store.read_messages(after)
                if messages:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 2
                store.wait_for_change(after, min(remaining, 30))
            for message in messages:
                value = store.delivery_message(message, compact=args.compact)
                print(json.dumps(value, ensure_ascii=False, separators=(",", ":") if args.compact else None))
        return 0
    except BrokenPipeError:
        return 1
    except (store.GuardBlocked, store.ClaimConflict, ValueError, OSError, KeyError) as error:
        print(f"chat: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
