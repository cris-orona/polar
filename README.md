# Polar

Polar is a personal Slack approval app for AI coding agents.

An agent (for example a Claude Code session) often needs a human yes or no. It may need to
send a message as you, pick one option, or answer a few questions. Polar gives the agent one
CLI command for this. Polar then sends you a card in a Slack DM with buttons. Your click is
the approval.

## The problem

- An agent that runs in auto mode should not post to Slack as you. It has no way to prove
  that you read the text.
- Typing "ok" in a terminal is easy to miss. It is also hard to do from a phone.
- With Polar, the agent only queues a request. It never holds a token that can post as you.
  Polar posts the message with your user token, and only after you click Send.
  The click is the approval.

## What it does

1. The agent runs `polar ask ...`. The CLI writes one row in a local SQLite queue. It never calls Slack.
2. The Polar app (Socket Mode) reads the queue and DMs you a card.
3. You click a button. Only the configured owner's clicks count.
4. For a reply, Polar posts it to the target channel or thread with your user token.
5. Polar writes the outcome to SQLite. The agent reads it with `polar status <id>`. Polar can also type the outcome into the agent's session (see "Optional integrations").

```
agent -> polar CLI -> SQLite queue -> Polar app (Socket Mode)
                                          |
                                          v
                                   DM card (Block Kit)
                                          |  owner clicks
                                          v
                       post with user token  ->  outcome back to the agent
```

## Request kinds

| Kind | What the owner sees | What the click does |
| --- | --- | --- |
| `reply` | A draft message for a channel or thread. Buttons: Send, Edit, Discard, Request changes. | Send posts the text as the owner. Edit opens a modal first. |
| `decision` | A title, context and 2 to 4 options. | The chosen option goes back to the agent. |
| `questions` | 1 to 5 typed questions with options or free text. | The answers go back to the agent. |
| `email` | A Gmail draft reply, like `reply`. Needs a Gmail OAuth setup. | Send sends the mail in the same thread. |
| `report` | An FYI card: what a session did, with links, optional file and follow-up buttons. | No approval needed. Follow-up buttons start new work. |
| `agent` | A card that offers to open a new agent session for a message. | The owner starts the session. |

Every approval card also has "Pedir cambios" (request changes). The owner types what to change.
The agent then queues a new card with `--replaces <id>`.

Polar also has these optional features. They are all in the code today. Read `docs/reference.md` for details.

- Tasks: the owner DMs Polar, and Polar starts a Claude Code session in tmux (or hands the task to a laptop relay).
- File handling: files in the owner's DMs are downloaded for the session.
- Web-first delivery and reminders: a local HTTP API records decisions from a web page. Polar can send reminder DMs instead of card DMs.
- Gmail: `polar mail ...` and the `email` card.

## Screenshots

_Screenshots to be added._

## Install

Requirements: Python 3.9 or newer, a Slack workspace where you can install an app.

1. Create the Slack app. In the Slack API site choose "Create New App", then "From a manifest", and paste `slack/manifest.yaml`.
2. Install the app to your workspace. Copy three tokens:
   - the bot token (`xoxb-...`),
   - the app-level token for Socket Mode (`xapp-...`, scope `connections:write`),
   - your user token (`xoxp-...`).
3. Store the tokens. Polar looks in this order: the macOS keychain, a private file, an environment variable.
   - Keychain: services `polar-bot-token`, `polar-app-token`, `polar-user-token` (account = `$USER`).
   - Files: `~/.config/polar/{bot,app,user}-token`. The folder must be mode 0700 and each file mode 0600. Polar ignores them otherwise.
   - Env: `POLAR_BOT_TOKEN`, `POLAR_APP_TOKEN`, `POLAR_USER_TOKEN`.
4. Install and run:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export POLAR_OWNER_ID=U0123456789        # your Slack user id
export POLAR_OWNER_DM=D0123456789        # your DM channel with the bot (needed for tasks and notices)
export POLAR_SLACK_URL=https://myteam.slack.com
bin/polar doctor                          # checks the three tokens
bin/polar serve
```

5. Queue a request from any shell on the same machine:

```sh
bin/polar ask decision --title "Deploy now?" --context "CI is green." \
  --option go:Go --option wait:Wait --requester "my-agent"
bin/polar status <id>
```

`deploy/` has an example macOS LaunchAgent and an `install.sh` that copies Polar to an always-on Mac over ssh.
Edit the plist first (label, owner id, URLs).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `POLAR_OWNER_ID` | placeholder | Slack user id. Only this user's clicks and DMs count. Set it. |
| `POLAR_OWNER_DM` | placeholder | The owner's DM channel with the bot. |
| `POLAR_SLACK_URL` | `https://example.slack.com` | Workspace URL, used to build message permalinks. |
| `POLAR_DATA_DIR` | `<repo>/data` | SQLite file, web token, downloaded files, Gmail files. |
| `POLAR_TOKEN_DIR` | `~/.config/polar` | Folder for token files. |
| `POLAR_BOT_TOKEN`, `POLAR_APP_TOKEN`, `POLAR_USER_TOKEN` | none | Token fallback. |
| `POLAR_TZ` | `UTC` | Time zone for reminder quiet hours. |
| `POLAR_DELIVERY` | `dm` | `web` stops the card DMs (a web page shows the cards instead). |
| `POLAR_SLACK_REMINDERS` | `on` | `off` stops the reminder DMs. |
| `POLAR_WEB_PORT` | `8791` | Local web API port, bound to 127.0.0.1. `0` turns it off. |
| `POLAR_WEB_ALLOW` | none | Comma list (`send`, `prod`) that enables gated web actions. |
| `POLAR_WEB_URL` | none | Base URL of your web page. Adds a "full report" link to report cards. |
| `POLAR_TASK_MAX`, `POLAR_TASK_MIN_FREE_GB`, `POLAR_WORK_DIR` | see reference | Limits and folder for tmux task sessions. |
| `POLAR_CLAUDE_BIN`, `POLAR_CLAUDE_ARGS`, `POLAR_TMUX_BIN` | see reference | How task sessions start. |
| `POLAR_BIN` | `~/polar/bin/polar` | Path of the CLI as written in the task brief. |
| `POLAR_TASK_RULES` | empty | Extra rules text added to the brief of each task session. |
| `POLAR_DEFAULT_WHERE` | `server` | `laptop` makes the laptop the default task host. |
| `AGENT_TASK_MODEL` | `opus` | Model flag for task sessions. Empty means no flag. |

More variables (timeouts, triage, Gmail) are in `docs/reference.md` and the source.

## Security model

- Only `POLAR_OWNER_ID` can act. Clicks, modal submits, DMs and the web API all check this id.
- The agent never gets a Slack token. The CLI only writes SQLite.
- The user token posts only after a click on Send (or a web action that calls the same code).
- Polar does not print or log tokens. `polar doctor` shows team and user names only.
- Token files must be 0600 in a 0700 folder.
- The web API binds to 127.0.0.1, needs a bearer token (`data/web-token`, mode 0600), checks the `Host` header and limits the body size.
  Sending to other people and production steps from the web are off unless you set `POLAR_WEB_ALLOW`.
- A stale click on a card that is already decided does nothing.
- Polar never starts a session with `--dangerously-skip-permissions`.

Slack scopes in the manifest and why:

| Scope | Why |
| --- | --- |
| bot `chat:write`, `im:write` | Send you cards and notices. |
| bot `im:history`, `app_mentions:read` | Read your DMs and mentions (tasks). |
| bot `files:read`, `files:write` | Download your attachments. Upload files to a card thread. |
| user `chat:write` | Post your approved reply as you. |
| user history and read scopes, `search:read`, `users:read` | Read threads and channel names to quote context in a card. |
| user `files:read` | Download files of messages where the bot is not a member. |

Read `SECURITY.md` to report a problem.

## Limitations

- Card text, buttons and notices are in Spanish today. There is no translation layer yet.
- Single owner. There is no multi-user or multi-workspace mode.
- macOS first: the keychain lookup, the LaunchAgent and the free-memory check use macOS tools. Token files and env vars work elsewhere.
- Some code is tied to the author's setup (see below). It works without that setup, but the names are still in the code.
- The tasks feature needs `tmux` and the `claude` CLI. It is optional.
- Slack user tokens have broad access. Protect the machine that runs Polar.
- The test suite uses fake Slack clients. There is no end-to-end test against real Slack.

## Optional integrations (examples)

Polar was built next to a private agent harness. These pieces are optional. Without them, Polar works as described above.

- Requester strings such as `prod-op ...` and `release-desk` change how Polar routes an outcome and gate the web `prod` action.
- `agent-bus` (binary path in `AGENT_BUS_BIN`) delivers text to parked sessions. `herdr-deliver` is used for tmux sessions. `polar-notify` is the relay that types outcomes into sessions.
- A heartbeat watchdog reads `POLAR_COORD_HEARTBEAT`. It stays quiet when the file is missing.

Treat these as examples. Replace them with your own relay.

## Tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest
```

Tests use no network.

## License

MIT. See `LICENSE`. Author: Cris Orona.
