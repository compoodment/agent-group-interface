# Contributing to AGI

## Report a problem

For a security issue, see [how to report it privately](SECURITY.md).

Search [Issues](https://github.com/compoodment/agent-group-interface/issues)
for the same problem. Say what you expected, what happened, and the shortest
steps that reproduce it. Include `agi --version`. For a screen problem, include
your terminal, `$TERM`, and whether you use tmux or SSH. For an agent connection
problem, include the client and its version. Share only the details needed
to reproduce the issue. If AGI printed an error, include the relevant lines
with private information removed. Keep your chat directory and `agi backup`
archives private.

For a screenshot or terminal example, open a disposable conversation from a
terminal and reproduce the problem with invented messages. This keeps the
example separate from your usual chat.

```sh
AGI_DATA="$(mktemp -d)" agi
```

## Propose a change

Small fixes and documentation edits can go straight to a pull request. Open
an issue first for a new feature or a change to existing behaviour so we can
agree on the goal before you spend time building it. Keep one purpose per
pull request. Say what you changed and what you tried. If you change a
shortcut or command, update the [guide](docs/Using.md) or the
[command reference](docs/CONTRACT.md) that describes it.

Contributions are accepted under the project's [MIT licence](LICENSE).
Include applicable licence notices and credit when you adapt someone else's work.

## Run AGI and its checks locally

Fork the repository, clone your fork, and open a terminal in the clone. Use
Python 3.11 or newer and a UTF-8 terminal. Open a test chat from the checkout.

```sh
scratch=$(mktemp -d)
AGI_DATA="$scratch" ./bin/agi
```

For code changes, run these checks from the repository root. They use disposable data. Set
`AGI_SCRATCH` to an existing directory to place temporary test data there.
Clipboard tests use a private Xvfb display when the dependencies are available.

```sh
python3 -m unittest discover -s tests -v
python3 tests/terminal_probe.py bin/agi
```

For a documentation-only change, follow the changed instructions and check
its links. In your pull request, say which checks ran and what you could not
check. Maintainers explain changes they request and give a reason when they
close an issue or pull request.

## Work with other contributors

Give specific, respectful feedback on the change and make room for questions.
Maintainers may remove harassment, personal attacks, and posts that expose
someone else's private information.
