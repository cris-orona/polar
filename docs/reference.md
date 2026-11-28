# Polar reference

Note: card text is Spanish today. Names such as `prod-op`, `release-desk`, `agent-bus`, `herdr` and `polar-notify` are example integrations from the author's private harness; they are optional.

Reference notes for Polar (older, detailed internals; see the main README for an overview). Claude sessions queue a request.
Polar sends the owner a DM with buttons. The owner's click is the approval.
Polar, not Claude, posts the reply, with the owner's user token.

Only `POLAR_OWNER_ID`  can act on the buttons.

## Parts

- `polar/db.py`: SQLite queue (`requests` table). Status: pending, shown, sent, chosen, discarded, expired, failed, answered, skipped (old rows only), revise, replaced, noted (a posted report: no click needed, not an open card), filed (a report folded to one line after 24 h). The `meta` table holds small state: the catch-up marks, the «Pendientes» message, the group Go message and the thread fallbacks.
  `sending` is an internal status that stops a double post.
- `polar/cli.py`: the `polar` command. It writes SQLite only. It never calls Slack.
- `polar/core.py`: app logic. It takes Slack clients as arguments, so tests use fakes.
- `polar/app.py`: Socket Mode wiring, `polar serve`, `polar doctor`.
- `polar/gmail.py`: Gmail (OAuth, search, thread, draft, send). Used by the `email` card and `polar mail`.
- `polar/tasks.py`: tasks from the owner's DMs: parsing, the pick card, the tmux launcher, steering.
- `polar/blocks.py`: Block Kit messages and the edit modal (Spanish text).
- `slack/manifest.yaml`: the Slack app manifest.
- `deploy/`: LaunchAgent plist and `install.sh`.

## Commands

```sh
bin/polar ask reply --channel C… [--thread TS] (--text T | --text-file F) --title T --context T --requester R
bin/polar ask decision --title T --context T --option go:Go --option wait:Esperar --requester R
bin/polar ask decision ... --canary   # test card for a health check: no DM
bin/polar ask … --replaces <id>   # the new card replaces your earlier card <id>
bin/polar discard <id>            # withdraw your own card (shown, pending or revise)
bin/polar status <id>
bin/polar list [--status pending|open|all] [--since ISO]
bin/polar expire
bin/polar doctor
bin/polar serve
```

### Pedir cambios

Reply, decision, questions and report cards have a «🔁 Pedir cambios» button. The owner types what to change.
The card becomes «🔁 Pediste cambios: …» with status `revise`, and `polar status <id>` shows
`instructions`. The session then writes a new card with `polar ask … --replaces <id>`. Polar closes
the old card as «↪️ Reemplazada por una tarjeta nueva» (status `replaced`) and the new card shows
«Reemplaza una tarjeta anterior». `polar discard <id>` withdraws a card in status shown, pending or revise.
On a questions card, the answers and the note already given go at the end of `instructions`
(`Respuestas ya dadas: 1 = …; 3 = …. Nota: …`).

`polar report … --next "TEXT"` (repeat, max 3, 1 to 70 characters) adds a follow-up. With one
follow-up the card has a «➡️ TEXT» button; one tap moves the report to `revise` with `instructions` = TEXT and
`via` = `next`; the card shows «➡️ Seguimiento: TEXT». With 2 or 3 follow-ups the card has checkboxes and a
«▶️ Empezar marcados» button (nothing marked: «Marca al menos un seguimiento.», no change). The result keeps `via` =
`next` and adds `indexes` (ascending) and `steps` (the texts); `instructions` is the one text, or one line
«1) A 2) B» for several; the card shows «➡️ Seguimientos: A · B». `--link` (max 5), `--file` and `--resume` are the other report options.
`polar report … --silent` stores a notice that needs no action: status `noted`, `dm_done=1`, no `dm_ts`, so Polar
sends no DM. the owner's web page and the web page show it like any posted report. `--silent` refuses `--file` (no thread to attach to).
A follow-up tap (`--next`) or «Reanudar» on a silent report works from the web page only; it starts a new task thread
when the session behind the report does not read the answer.

## Web API

A local HTTP API lets a web page on the same server record the owner's decisions. Each action calls the
same `Polar.on_*` method as the Slack button, as the owner, so status checks, the DM refresh, the result for
the requester session and the Slack or Gmail post are the same. The stored result gets one extra key,
`"src":"web"`, and the DM line ends with «· desde la web».

- `polar/webapi.py` starts from `polar serve` in a daemon thread. It binds `127.0.0.1` only.
  `POLAR_WEB_PORT` sets the port (default 8791; `0` turns it off). A busy port only logs one line.
  `POLAR_WEB_ALLOW` (a comma list of `send` and `prod`) turns on the two gated actions: `send` (a message to
  another person) and the Go of a prod-op step. Default: none; the API answers 403 «… se hace desde Slack».
  `GET /health` lists what is on in `allow`.
- Auth: `Authorization: Bearer <token>`. The token is in `data/web-token` (mode 0600, made on first start).
  Wrong or missing token: 401. A `Host` other than `127.0.0.1:<port>` or `localhost:<port>`: 403. Body over 64 KB: 413.
  A reverse proxy must send one of those Host values.
- `GET /health` gives `{"ok":true}`.
- `POST /act` takes JSON `{"id": "<card id>", "action": ...}`:

| action | extra fields | card | core method |
| --- | --- | --- | --- |
| `choose` | `option` | decision, report (`ok`, `resume`) | `on_choose` |
| `answer` | `answers` {question key: option key or free text}, `note` | questions | `on_q_answer` / `on_q_submit`, then `on_q_send` |
| `send` | optional `text` (edited text) | reply, email | `on_edit_submit` (with text), then `on_send` |
| `discard` | | reply, email | `on_discard` |
| `revise` | `text` | reply, decision, email, report, questions | `on_revise_submit` |
| `next` | `index` (int) or `indexes` (list of 1 to 3 distinct ints) | report | `on_next` |
| `later` | `when`: `manana`, `3d`, `lunes` or `prod` | report; decision or questions already answered | `followups.add_later` (bus `followup add --by owner`); 200 `{ok, followup}`; 400 bad input, 409 not allowed on this card, 422 the bus refused (Spanish `error`). The status does not change |
| `fu_cancel` | `fu` (int, bus follow-up id) | any card that lists it | `followups.cancel_followup` (bus `followup cancel`); 200 `{ok, followup}` (`state` `delivered` means too late) |
| `seen` | | any | none (a report is «noted» from the start); returns the status |

`answer` needs an answer for every question (earlier answers from Slack count). `POLAR_WEB_URL` adds a
«📄 Informe completo» link button on report cards: `<POLAR_WEB_URL>/informes/<id>`.

Answer: 200 `{"ok":true,"status":"<status after>"}`. Refused: 400 (bad input) or 409 (wrong kind, wrong status,
unknown id) with `{"ok":false,"error":"<texto>"}`; 409 also has `status`. A repeat on a decided card gives 409
and changes nothing. If the post to Slack or Gmail fails: 502 with `"status":"failed"` (the card shows «Falló»).

## Web UI

`polar/web/` is a server-rendered web UI (standard library HTTP server, Markdown for card bodies). `polar serve`
starts it in a daemon thread when `POLAR_UI_PORT` is set; unset or `0` keeps it off, and a busy port only logs one
line. It binds `127.0.0.1`. The log line gives the URL, the command that prints the login link and
`POLAR_WEB_URL`; it never holds the token.

Login: `polar ui-url` prints `http://127.0.0.1:<POLAR_UI_PORT>/login?token=<token>` (exit 2 when `POLAR_UI_PORT` is
not set). Open it once: it sets the cookie `polar_ui_<port>` (`HttpOnly; SameSite=Lax; Path=/; Max-Age` 180 days).
The link is a permanent credential until the secret is rotated: delete `data/web-token` to log out every browser.
The token is `HMAC-SHA256(secret, "polar-ui-login")`, the cookie value `HMAC-SHA256(secret, "polar-ui-session")` and
the CSRF token an HMAC of the secret and the cookie value; the secret is the web API token in `data/web-token`. Without
the cookie, every route except `/login`, `/healthz` and `/static/` answers 401 (a short HTML page for pages and
files, JSON for `/api/` and POST). The login keeps out other OS users, web pages and scripts that only find the port;
a program that runs as your own OS user can read the secret and the database, so it can act as you.

| Variable | Default | Meaning |
| --- | --- | --- |
| `POLAR_UI_PORT` | none | Port of the UI. Unset or `0`: off. |
| `POLAR_UI_HOSTS` | none | Comma list of extra `Host` values (a reverse proxy host name). Origins `http://` and `https://` of each are allowed. |
| `POLAR_UI_OWNER` | none | When set, the owner header must equal it, else 403. |
| `POLAR_UI_OWNER_HEADER` | `Tailscale-User-Login` | Header that carries the owner login. |
| `POLAR_UI_LANG` | `en` | `en` or `es`. All UI strings are in `polar/web/strings.py`. |
| `POLAR_TZ` | system zone | Time zone of the dates on the pages. |
| `POLAR_WEB_URL` | none | The URL you open the UI at. Reminders and report cards link to `<POLAR_WEB_URL>/?c=<id>` and `/informes/<id>`. |
| `POLAR_WEB_ALLOW` | none | `send` shows and allows Send on reply and email cards. |

Routes:

| Route | What |
| --- | --- |
| `GET /login?token=` | Right token: sets the session cookie, 303 to `/`. Else 403. |
| `GET /` | Pending: open action cards (oldest first), then reports not seen. `?c=<id>` puts that card first; a closed card shows read-only. |
| `GET /history` | Decided cards by day, newest first, `?page=N`. Reports that were filed, replaced or expired are left out. |
| `GET /reports`, `/informes` | All reports, newest first, `?q=` searches title, context and text, `?page=N`. |
| `GET /reports/<id>`, `/informes/<id>` | One full report. |
| `GET /files/<id>/<n>` | File `n` of the card, only inside `data/files` or `data/uploads`. |
| `GET /api/state` | `{"v": <version stamp>, "waiting": n}`. The page polls it every 5 s. |
| `GET /healthz` | `{"ok": true}`. |
| `POST /api/act` | `{"id", "action", ...}` with `X-CSRF-Token`. Runs `webapi.perform` in process. `seen` maps to `choose` `ok` on a report. |

`polar demo [--port 8792] [--lang en|es]` starts only the UI on a fresh temporary data dir with invented cards. It
ignores `POLAR_DATA_DIR`, makes its own secret in that folder, prints its login link at start, and uses the real core with a fake Slack client, so actions change only
the temporary database. The folder is removed on exit.

## Tasks (send an agent from Polar)

The owner writes to Polar in the DM. Polar needs the `message.im` event, the `im:history` bot scope
and the app Home «Messages» tab (all in `slack/manifest.yaml`). Without the event, nothing breaks:
Polar only does not see DMs.

- A message with files (images, PDFs, sheets) is read too. Slack sends it with subtype `file_share`. Polar
  downloads each file into `data/files/<message ts>/` (bot scope `files:read`; the user token, also with
  `files:read`, is the fallback for files of forwarded messages and of channels where Polar is not a member).
  The task or steer text ends with «Archivos adjuntos del mensaje (N): …» and the command
  `polar files <message ts>`, which prints the local paths (the laptop wrapper copies the files to
  `~/.local/state/polar/files/` first). The notice says «🧭 Recibido (📎 2 archivos).». A message with only
  a file is a task («Revisa los archivos adjuntos.»). A message with files never becomes a pick list. HEIC
  photos become JPEG. Folders older than 30 days are deleted. `polar thread` lists the `files` of each
  message; `polar files <file id>` downloads one.
- `polar replay <message ts>` reads one DM of the owner again: for a message that came while Polar was down
  or that an old version dropped. A message that Polar already took changes nothing.
- A top-level message is a new task (kind `task`, title = first line clipped to 60). Polar answers
  «🧭 Recibido. Abro una sesión…» in the thread of the message. The thread is the task thread.
- A message of 3 or more lines (bullets, numbers or plain lines) makes one pick (status `planning`) instead.
  Polar answers «🧭 Recibí N tareas. Las ordeno…». The laptop router writes a plan (`polar picks --json`,
  then `polar plan-set <id> --file plan.json`). Polar turns the reply into a planned card: groups
  «▶️ Recomiendo empezar», «🔄 Ya en marcha», «🕒 Después», «✅ Ya hecho», «🗑️ Descartar», checkboxes (10 for each
  block), «Arrancar las marcadas» and «Ver lista original». Each checked item becomes a task with its own
  thread. An item with a `session` goes straight to `routed` (high confidence), without triage.
  No plan in `POLAR_PLAN_TIMEOUT` (600 s): the reply becomes the plain card with «No llegó el plan; aquí está la
  lista tal cual.». `plan-set` also accepts a pick in status `open`: Polar posts the plan as a new card in the
  thread and marks the old card «Reemplazada por el plan de abajo.».
  Plan JSON: `{"summary", "groups": [{"key": now|running|later|done|drop, "items": [{"i", "title", "reason",
  "session", "label", "checked"}]}]}`. Each item index appears once.
- A thread reply under a task is steering. A reply under a card whose requester is a server task also
  goes to that task. A reply under any other card goes to the requester (`polar steers`).
- A card whose requester names no session (empty, a script name such as `watchdog`, `unknown-session`) has no
  reader. When the owner writes on it («Pedir cambios» or a thread reply), the text starts a task in the card's
  thread: triage finds the session that owns the subject or opens a new one. On an «Abrir agente» card the text
  starts the agent with the brief and the owner's instruction (no triage). `tasks.has_session` holds the rule;
  it is the same rule as in `polar-notify` (a session id, or the release desk, which reads its own cards).
- `→ 3 text` or `-> 3 text` steers laptop Herdr tab 3. The laptop relay delivers it (`polar steer-done <id>`).
- Triage: a new task first has status `triage` (Polar does not launch it). The laptop router answers with
  `polar triage-set`. `--none` or low confidence: `queued` (new session). High or medium: status `routed` at once, with no question to the owner. The thread
  says «➡️ Lo mandé a <label>: <reason>» (medium: «… (coincidencia probable): <reason>») with the button
  «No, sesión nueva». Old rows in status `confirm` still work: a card «¿Es de <label>?»; no click in
  `POLAR_CONFIRM_TIMEOUT` (600 s) means a new session. No answer in `POLAR_TRIAGE_TIMEOUT` (300 s) means a new session and the thread says
  «Sin respuesta de la laptop; abro sesión nueva.». `POLAR_TRIAGE=0` turns triage off. Each pick item has its
  own task, thread and triage. The laptop delivers routed tasks (`polar routes`, then `polar route-done <id>`).
  After delivery the «No, sesión nueva» button does nothing.
- Status follows the bus: `follow_bus` (tick step, at most once each 60 s, one `agent-bus sessions --json --days 30`
  call) follows each session to its newest handoff or wake successor. Bus `closed` sets `done`; a `running` task
  whose session is `parked` sets `parked`; a `parked` task whose successor is live on the server in a `task-*` tmux
  sets `running` (with the new tmux and session id). A routed task (delivered) whose target closed sets `done` with no
  Slack change. A laptop task is `done` only when the bus ended it more than 10 min ago. Slack gets an in-place edit
  of the status message only (never a new post), at most 10 edits in one run. A bus failure changes nothing; `reap`
  asks the bus before it decides `parked` or `done`. `polar follow-bus [--dry-run]` runs one pass by hand.
- Placement: the laptop is first, the server is the fallback. A queued task waits `POLAR_LAPTOP_GRACE`
  seconds (120) for the laptop to claim it. The laptop lists the queue with `polar queue --json` (id, text,
  title, thread_ts, created_at, where_hint, overflow) and takes one with `polar claim <id> --host laptop --tab <label> --session <sid>
  [--pane <pane>]`. The claim is atomic (queued to running, column `host` = `laptop`); it exits 1 when the task
  is not queued any more (the server took it). The server tick then posts «▶️ Abrí "<title>" en la laptop (pestaña <n>).».
  `polar task-done <id>` (the laptop tab closed) sets `done` and edits the task's status message to «✅ Terminó». Polar does not reap, launch
  or tmux-steer a `host=laptop` task. A thread reply (or a card reply) for it appears in `polar steers` with
  requester `<session> <pane>`, and the laptop types it into the pane.
- After the grace, the server launcher runs in the server tick (every 5 s). For each queued task it starts
  `tmux new-session -d -s task-<id> -c ~/work "claude --session-id <uuid> \"$(cat brief)\""`
  when fewer than `POLAR_TASK_MAX` (code default 4, service file 14) `task-*` tmux sessions hold a slot and free memory
  (vm_stat free + inactive + speculative) is at least `POLAR_TASK_MIN_FREE_GB` (2.5). Else it posts
  «⏳ En cola: n antes» and marks the task `overflow`: the laptop may then claim it even when `where_hint` is
  `server` (overflow to the laptop when the server is full). A finished tmux session becomes `done` (the status message turns to «✅ Terminó: <report>», or «sin informe todavía» until a report comes); a session that parked
  (marker file in `~/.local/state/agent-bus/parked/`) becomes `parked`.
- Polar pre-accepts the Claude Code folder trust for `~/work` in `~/.claude.json`. It never uses
  `--dangerously-skip-permissions`: the session asks for permissions in its tmux pane. Attach from the laptop.
- A card with `--requester "<session id> task-<id>"` gets its outcome typed into the session by Polar.
- Env: `POLAR_LAPTOP_GRACE`, `POLAR_TASK_MAX`, `POLAR_TASK_MIN_FREE_GB`, `POLAR_WORK_DIR`, `POLAR_CLAUDE_BIN`, `POLAR_CLAUDE_ARGS`, `POLAR_TMUX_BIN`.

```sh
bin/polar tasks [--json] [--status running]   # id, title, status, tmux, session_id, thread_ts, created_at, activity_at
bin/polar queue --json                        # queued tasks for the laptop (id, text, title, thread_ts, created_at)
bin/polar claim <id> --host laptop --tab <label> --session <sid> [--pane <p>]  # exit 1 when not queued
bin/polar task-done <id>                      # the laptop tab closed
bin/polar steers                              # undelivered laptop steering (JSON list)
bin/polar triage --json                       # tasks that wait for a match (id, text, thread_ts)
bin/polar triage-set <id> --match <session_id> --label "tab 3 · title" --confidence high|medium --reason "…"
bin/polar triage-set <id> --none              # no session fits: new session
bin/polar routes                              # routed tasks not yet delivered (JSON list)
bin/polar route-done <id>                     # the laptop delivered a routed task
bin/polar route-failed <id>                   # the laptop could not deliver; opens a new session
bin/polar steer-done <id>                     # the laptop delivered it; Polar posts «↪️ Enviado …»
```

Sessions call it with `ssh <server> <polar>/bin/polar …`.
`ask` prints `{"id": "…"}`. Reply text has a limit of 2600 characters.

## Tokens

Polar reads `polar-bot-token`, `polar-app-token` and `polar-user-token` from the macOS keychain
(account = `$USER`). Env vars `POLAR_BOT_TOKEN`, `POLAR_APP_TOKEN` and `POLAR_USER_TOKEN` are the
fallback for tests. Polar never prints or logs a token.

## Test

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m pytest
```

Tests use no network.

## Token files (no keychain)

If the keychain has no token, Polar reads `~/.config/polar/{bot,app,user}-token`. The folder must be mode 0700 and each file 0600, or Polar ignores the file. Set `POLAR_TOKEN_DIR` to use another folder.

## Gmail

Polar holds the Gmail credentials once, on the server. They do not depend on a Claude account.
Without them, every `polar mail …` command and `polar ask email` exit 2 with
«Gmail no está conectado: corre polar mail auth».

Files in `~/polar/data/` (`deploy/install.sh` never overwrites `data/`):
`google-client.json` (OAuth client) and `google-token.json` (mode 0600, refreshes by itself).
Scopes: `gmail.readonly`, `gmail.compose`, `gmail.send`.

One-time setup:

1. In the Google Cloud console, create a project or use an existing one.
2. Enable the Gmail API (APIs & Services, Library).
3. Set up the OAuth consent screen. Choose «Internal» if the account is in a Workspace. Add the three scopes.
   (External + «Testing»: add your address as a test user; the token then expires after 7 days.)
4. Create credentials: OAuth client ID, type «Desktop app». Download the JSON.
5. Copy it: `scp client.json <server>:~/polar/data/google-client.json`
   then `ssh <server> chmod 600 '~/polar/data/google-client.json'`.
6. Run `polar mail auth`. Polar prints a consent URL. Open it in the laptop browser and allow access.
   The browser then shows a «localhost» error page. This is normal. Copy the full URL from the address bar
   (or only the `code=` value) and paste it at the prompt. Polar saves the token. No browser opens on the server.

Commands (JSON output):

```sh
polar mail search "from:ana newer_than:7d" [--max 20]   # id, threadId, from, to, subject, date, snippet
polar mail thread <threadId>                            # messages: from, to, cc, date, subject, body, message_id
polar mail draft --thread <threadId> --text-file F [--cc a@example.com]   # Gmail draft reply; prints {"draft_id"}
polar ask email --thread <threadId> --title T --context T --text-file F [--cc a@example.com]
polar thread <channel> <thread_ts>                      # Slack thread, JSON lines, same fields as `polar inbox`
#   rows carry sender_id and sender_name (real name, cached 7 days in slack-users.json; `polar inbox` too);
#   a voice clip in `files` has `transcript` (from the Slack captions, or the preview with « …») when Slack made one
```

The `email` card works like a `reply` card: it quotes the last message of the thread, and has Enviar, Editar,
Descartar and Pedir cambios. Text is Markdown; Polar sends plain text plus simple HTML, in the same thread, with
`In-Reply-To` and `References`. The sent card shows the Gmail link, and `polar status <id>` returns
`permalink`, `message_id` and `thread_id`. Text limit: 2600 characters, like a reply card.

The laptop `polar` wrapper reads `--text-file` only for `ask`. For `mail draft` use
`polar mail draft --thread T --text-file /dev/stdin < file`. `polar mail auth` reads the pasted URL from stdin.

## Status message and lost steers

- Each owner task has one status message (`tasks.status_ts`, `status_text`). Recibido, route, queue, start, pause and
  Terminó edit it in place (`Tasks.status_line`); same text means no Slack call. A tick step fixes «sin informe
  todavía» when the report arrives (24 h).
- A steer (owner text in a card or task thread) goes to a session or fails with a «⚠️ Tu mensaje no llegó…» line.
  Parked or done server sessions get it through `agent-bus deliver`. `Tasks.sweep_steers` takes any steer the laptop
  relay left for 3 polls (`POLAR_RELAY_POLL`, default 20 s). Steers older than 24 h become `expired` with no message.

## Web-first delivery

`POLAR_DELIVERY=web` (default `dm`) stops the card DMs. A pending card becomes `shown` (a report, `noted`) with no
`dm_channel` and no `dm_ts`; the owner's web page shows it. «Pendientes» and the group Go do nothing. Instead, `Polar.sync_reminders`
(in `tick`) sends one DM that lists the open action cards (never reports) at 30 min, 1 h and 4 h of age, each once:
`⏰ 1 h · title → <POLAR_WEB_URL/?c=id|Abrir>`. A card already past several ages gets only the highest. No reminder from
22:00 to 08:00 in `POLAR_TZ` (default UTC); at 08:00 one message lists what crossed an age at night.
State is the meta key `reminders` (`sent`: card → highest minutes sent; `msgs`: channel, ts, card ids). A message is
deleted when none of its cards is open. Switch back with `POLAR_DELIVERY=dm`.
`POLAR_SLACK_REMINDERS=off` (default `on`) stops the reminder DMs too; the owner's web page pushes to the phone instead. Old
reminders are still deleted when their cards close.
