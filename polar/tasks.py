"""Owner tasks from Slack DMs: parse messages, start Claude sessions in tmux, relay steering.

The owner writes to Polar in a DM. A top-level message is a task. A thread reply is steering.
`→ 3 text` steers laptop tab 3. Polar starts each task as `task-<id>` in tmux on this machine.
All outside calls (tmux, vm_stat) go through `Tasks.sh`, so tests replace them."""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from polar import blocks, db, files

log = logging.getLogger("polar")

# Where a new session runs when the router does not say: the always-on server (POLAR_DEFAULT_WHERE=laptop: your laptop).
# Name of the always-on host. It shows in owner texts («en <name>») and is stored as the task host.
# Unset: «el servidor» in texts and `server` as host (POLAR_SERVER_NAME).
SERVER_NAME = (os.environ.get("POLAR_SERVER_NAME") or "").strip()
SERVER_LABEL = SERVER_NAME or "el servidor"
# launchd label of the coord-tick agent: the watchdog text tells the owner how to kick it.
COORD_TICK_LABEL = os.environ.get("POLAR_COORD_TICK_LABEL") or "coord-tick"
DEFAULT_WHERE = "laptop" if os.environ.get("POLAR_DEFAULT_WHERE") == "laptop" else "server"
TITLE_MAX = 60
MIN_ITEMS = 3
MAX_ITEMS = 40
MAX_ITEM_CHARS = 300
MAX_STEER_CHARS = 4000
MAX_CARD_QUOTE = 1500
RELAY_POLL = 20.0            # seconds: the laptop relay (server-tasks, run by slack-watch) polls `polar steers`
STEER_EXPIRE = 24 * 3600     # a steer that nobody took in this time is stale: no message
SLOT_GRACE = 300             # seconds a new task-* tmux session holds a slot before the bus knows it
BUS_TIMEOUT = 120            # seconds for `agent-bus deliver` (it may resume a session)
SENDING_STALE = 600          # a steer claimed this long ago and never finished: tell the owner
NO_REPORT_TEXT = "✅ Terminó · sin informe todavía"
ROUTER_HAS_IT = "↪️ La sesión no tiene una pantalla abierta. Se lo pasé al router para que la encuentre."
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def has_session(who: Optional[str]) -> bool:
    """True when something reads the answer of a card with this requester. It is the rule of polar-notify:
    «<session id> <pane>» or «prod-op <op> <session id> …» names a Claude session, and the release desk reads its
    own cards. A script name («watchdog»), «unknown-session», a short id or an empty text has no reader."""
    parts = (who or "").split()
    if parts[:1] == ["prod-op"]:
        parts = parts[2:]
    return bool(parts) and (parts[0] == "release-desk" or bool(SESSION_ID.fullmatch(parts[0])))


def real_requester(who: Optional[str]) -> str:
    """«prod-op <op> <session> <pane>» names the session after the op id: return «<session> <pane>»."""
    parts = (who or "").split()
    return " ".join(parts[2:]) if parts[:1] == ["prod-op"] else (who or "").strip()


def card_has_reader(req: Optional[Dict[str, Any]]) -> bool:
    """True when a session reads the owner's text on this card. An «Abrir agente» card never has a reader: the
    router run that made it ended when it sent the card, even when Polar wrote that run's session id on it."""
    return bool(req) and not req.get("agent_where") and has_session(req.get("requester"))
OWNER_DM = os.environ.get("POLAR_OWNER_DM") or "D0000000000"  # the owner's DM channel with the bot
COORD_CHECK_EVERY = 300      # seconds between two looks at the coord-tick heartbeat
COORD_STALE = 15 * 60        # coord-tick ticks every 20 s: 15 min without a beat means it stopped
COORD_REPEAT = 2 * 3600      # at most one alarm DM per 2 h while it stays stopped
CATCHUP_EVERY = 300          # seconds between two catch-up reads of the owner's DM (top level)
CATCHUP_THREADS_EVERY = 900  # seconds between two catch-up reads that also read the open threads
CATCHUP_SETTLE = 20          # a message younger than this may still arrive as a live event
CATCHUP_PAGES = 3
CATCHUP_TASK_DAYS = 2
CATCHUP_MAX_AGE = 24 * 3600   # a round never reads further back than this
CATCHUP_MAX_THREADS = 25      # one Slack call per thread: stay far under the rate limit
OPEN_TASK_STATUSES = ("triage", "matched", "confirm", "routed", "queued", "running", "parked")
ARROW = re.compile(r"^\s*(?:→|->)\s*(\d+)\b[\s:.\-]*(.*)$", re.S)
ITEM_PREFIX = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s+")
FINAL = ("sent", "chosen", "discarded", "expired", "failed", "answered", "skipped", "revise", "replaced")

ONLY_FILES = "Revisa los archivos adjuntos."
OK_SUBTYPES = (None, "file_share")  # a message with a file comes as subtype file_share

ANSWER_RULE = ("To answer the owner's question, use `polar report --thread {thread} --channel {channel} "
               "--requester …` (he reads it in the thread and in Mando). `polar ask reply` is only for a "
               "message to ANOTHER person.")

RESUME_PROMPT = ("El dueño pidió reanudar desde Polar. Retoma donde quedaste y termina con polar report. "
                 + ANSWER_RULE.format(thread="<thread>", channel="<channel>"))


def task_model(text: str = "") -> str:
    """The model of a session Polar starts; Env AGENT_TASK_MODEL wins
    (empty: no --model flag). Else fable for a hard task ([difícil], [dificil] or [fable] in `text`), else opus.
    Without a --model flag the session takes the default of ~/.claude/settings.json."""
    env = os.environ.get("AGENT_TASK_MODEL")
    if env is not None:
        return env.strip()
    low = (text or "").lower()
    return "fable" if any(m in low for m in ("[difícil]", "[dificil]", "[fable]")) else "opus"


def open_model(m: str) -> str:
    """opus instead of fable while the weekly Fable limit is used up. The check is a command you give in
    POLAR_MODEL_FULL_CMD: it exits 0 when the weekly limit of that model is used up. It gets the model name as the
    last argument (for example `my-tool model-full fable`: set POLAR_MODEL_FULL_CMD="my-tool model-full").
    Unset, or any error: keep `m`."""
    cmd = os.environ.get("POLAR_MODEL_FULL_CMD", "").strip()
    if not cmd or not m or "fable" not in m.lower():
        return m
    try:
        full = subprocess.run([os.path.expanduser(x) for x in shlex.split(cmd)] + ["fable"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        full = False
    return "opus" if full else m


def model_flag(text: str = "") -> str:
    m = open_model(task_model(text))
    return ("--model %s " % shlex.quote(m)) if m else ""

# Path of the polar command inside a task session. POLAR_TASK_RULES adds your own rules to the brief (after «no browser»).
POLAR_BIN = os.environ.get("POLAR_BIN") or "~/polar/bin/polar"

def _extra_rules() -> str:
    """POLAR_TASK_RULES goes right after «no browser». It may start with «;» or «.»; else a «. » is added."""
    x = (os.environ.get("POLAR_TASK_RULES") or "").strip()
    if not x:
        return "."
    return x if x[0] in ";.," else ". " + x


BRIEF = (
    "Owner task from Polar (task {id}): {text}\n\n"
    "Rules: read ~/.claude/CLAUDE.md. You run on {where}: no browser{extra_rules} "
    "Start with memory recall, then investigate and write a short plan. "
    "A card that asks the owner to approve or choose must explain the proposal in the card: --text-file with what it is, what changes for whom, a real example and the risk (5-8 lines, Spanish), plus --link to the PR or doc. Never send him to the terminal. "
    "Send reading-heavy work (repo exploration, log scans, wide queries) to subagents on Sonnet "
    "(general-purpose is Sonnet; pass model: \"sonnet\" to Explore); keep decisions and critical edits here. "
    "Ask the owner only through `{polar_bin} ask ... --thread {thread} --channel {channel} "
    "--requester \"{session} task-{id}\"` so cards land in the task thread and the answer is typed into this session. "
    + ANSWER_RULE + " "
    "If a step needs the laptop (browser, a laptop-only MCP), hand it off: polar handoff --title T --text-file F --requester \"<your session id> <your tmux name>\"; the laptop session sends you the result. "
    "When you finish (or stop for good), send one report: polar report --title \"<qué quedó, en español>\" "
    "--text-file <md> [--link \"PR=<url>\"] [--link \"Documento=<url>\"] [--file <pdf>] --requester <same as above>. "
    "Body: 3 to 6 lines in Spanish: what you did, what is in staging or production, what is left and who has it. "
    "Put documents and artifacts as links (PR, Wiki, claude.ai artifact) or --file for a PDF; never only a local path."
)


# -- parsing ---------------------------------------------------------------
def clip_title(text: str) -> str:
    first = (text.strip().splitlines() or [""])[0].strip()
    return first if len(first) <= TITLE_MAX else first[: TITLE_MAX - 1] + "…"


def split_items(text: str) -> List[str]:
    """Return the items of a list-like message, or [] when it is not a list."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_ITEMS or len(lines) > MAX_ITEMS:
        return []
    items = [ITEM_PREFIX.sub("", ln).strip() for ln in lines]
    if any(not it or len(it) > MAX_ITEM_CHARS for it in items):
        return []
    return items


def parse_free_gb(vm_stat_out: str) -> Optional[float]:
    """Free + inactive + speculative memory in GB from `vm_stat` output."""
    m = re.search(r"page size of (\d+) bytes", vm_stat_out)
    size = int(m.group(1)) if m else 4096
    pages = 0
    found = 0
    for key in ("free", "inactive", "speculative"):
        mm = re.search(r"Pages %s:\s+(\d+)" % key, vm_stat_out)
        if mm:
            pages += int(mm.group(1))
            found += 1
    return pages * size / 1024 ** 3 if found else None


def _find(name: str, extra: Tuple[str, ...] = ()) -> Optional[str]:
    for d in extra:
        p = os.path.join(os.path.expanduser(d), name)
        if os.access(p, os.X_OK):
            return p
    return shutil.which(name, path=os.pathsep.join(
        [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def default_sh(argv: List[str], timeout: float = 15) -> Tuple[Optional[int], str]:
    """Run a command. Return (exit code, stdout). Return (None, "") when it cannot run."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception:
        return None, ""


HANDOFF_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # a Claude session id in --requester


class Tasks:
    def __init__(self, conn: Any, bot: Any, owner_id: str,
                 sh: Optional[Callable[..., Tuple[Optional[int], str]]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 fetch: Optional[files.Fetch] = None) -> None:
        self.conn = conn
        self.bot = bot
        self.owner_id = owner_id
        self.sh = sh or default_sh
        self.fetch = fetch  # None: files.download (Slack, bot token then user token)
        self.sleep = sleep
        self._coord_checked = time.time()  # first look after COORD_CHECK_EVERY: coord-tick beats again after a reboot
        self._coord_alert_at: Optional[float] = None  # when the last alarm went; None: no alarm open
        self.intake: Callable[[Dict[str, Any]], Any] = self.on_message  # Polar sets its own entry here
        self._type_fails: Dict[str, int] = {}  # request id -> failed tries to type its answer
        self._catchup_at: Optional[float] = None          # last catch-up round; None: not run yet
        self._catchup_threads_at: Optional[float] = None  # last round that read threads

    # -- config ------------------------------------------------------------
    def max_tasks(self) -> int:
        return int(env_float("POLAR_TASK_MAX", 4))

    def min_free_gb(self) -> float:
        return env_float("POLAR_TASK_MIN_FREE_GB", 2.5)

    def work_dir(self) -> Path:
        return Path(os.environ.get("POLAR_WORK_DIR") or Path.home() / "work").expanduser()

    def claude_bin(self) -> Optional[str]:
        return os.environ.get("POLAR_CLAUDE_BIN") or _find("claude", ("~/.local/bin",))

    def tmux_bin(self) -> str:
        return os.environ.get("POLAR_TMUX_BIN") or _find("tmux", ("/opt/homebrew/bin",)) or "tmux"

    def laptop_grace(self) -> float:
        return env_float("POLAR_LAPTOP_GRACE", 120)

    def triage_on(self) -> bool:
        return os.environ.get("POLAR_TRIAGE", "1") != "0"

    def triage_timeout(self) -> float:
        return env_float("POLAR_TRIAGE_TIMEOUT", 300)

    def confirm_timeout(self) -> float:
        return env_float("POLAR_CONFIRM_TIMEOUT", 600)

    def plan_timeout(self) -> float:
        return env_float("POLAR_PLAN_TIMEOUT", 600)

    def parked_dir(self) -> Path:
        return Path(os.environ.get("POLAR_PARKED_DIR")
                    or Path.home() / ".local/state/agent-bus/parked").expanduser()

    # -- Slack helpers -----------------------------------------------------
    def say(self, channel: str, thread_ts: Optional[str], text: str,
            blocks_: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
        try:
            kw: Dict[str, Any] = {"channel": channel, "text": text}
            if thread_ts:
                kw["thread_ts"] = thread_ts
            if blocks_:
                kw["blocks"] = blocks_
            return self.bot.chat_postMessage(**kw)["ts"]
        except Exception as exc:
            log.error("task notice failed: %s", _code(exc))
            return None

    def status_line(self, tid: Optional[str], text: str, blocks_: Optional[List[Dict[str, Any]]] = None) -> bool:
        """The one status message of a task: post it once, then edit it in place (an edit sends no notification).
        Skip Slack when the text is the same. When the edit fails, or an old task has no message yet, post a new
        one. Return True when the owner can read `text`."""
        t = self.get(tid) if tid else None
        if not t:
            return False
        if t.get("status_ts") and t.get("status_text") == text:
            return True
        bl = blocks_ or [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        if t.get("status_ts"):
            try:
                self.bot.chat_update(channel=t["channel"], ts=t["status_ts"], text=text, blocks=bl)
                self.update(tid, status_text=text)
                return True
            except Exception as exc:
                log.warning("status edit failed for task %s, posting a new one: %s", tid, _code(exc))
        ts = self.say(t["channel"], t["thread_ts"], text, blocks_)
        if not ts:
            return False
        self.update(tid, status_ts=ts, status_text=text)
        return True

    def report_for(self, t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The newest report card of the session of a task, or None. Only a report made after the task started: a
        task sent to a session that already ran must not show an older report of that session."""
        sql = "SELECT * FROM requests WHERE kind='report' AND created_at>=? AND (requester LIKE ?"
        args: List[Any] = [t.get("created_at") or "", "%task-" + t["id"]]
        if t.get("session_id"):
            sql += " OR requester LIKE ?"
            args.append(t["session_id"] + "%")
        r = self.conn.execute(sql + ") ORDER BY created_at DESC, rowid DESC LIMIT 1", args).fetchone()
        return dict(r) if r else None

    def done_text(self, t: Dict[str, Any]) -> str:
        r = self.report_for(t)
        if not r:
            return NO_REPORT_TEXT
        url = blocks.report_web_url(r["id"])
        if not url and r.get("dm_channel") and r.get("dm_ts"):
            url = blocks.SLACK_URL + "/archives/%s/p%s" % (r["dm_channel"], r["dm_ts"].replace(".", ""))
        title = clip_title(r["title"])
        if not url:
            return "✅ Terminó: %s" % title
        safe = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "/")
        return "✅ Terminó: <%s|%s>" % (url, safe)

    def finish_line(self, t: Dict[str, Any]) -> None:
        self.status_line(t["id"], self.done_text(t))

    def refresh_final(self) -> None:
        """A task that ended before its report came shows «sin informe todavía». Fix the line when the report
        arrives, for tasks that ended in the last 24 hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for t in self.conn.execute(
                "SELECT * FROM tasks WHERE kind='task' AND status='done' AND status_ts IS NOT NULL"
                " AND status_text=? AND COALESCE(finished_at, updated_at)>=?", (NO_REPORT_TEXT, cutoff)).fetchall():
            t = dict(t)
            text = self.done_text(t)
            if text != NO_REPORT_TEXT:
                self.status_line(t["id"], text)

    # -- rows --------------------------------------------------------------
    def get(self, tid: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def rows(self, kind: str, *statuses: str) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE kind=?"
        args: List[Any] = [kind]
        if statuses:
            sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
            args += list(statuses)
        sql += " ORDER BY created_at, rowid"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def update(self, tid: str, **fields: Any) -> None:
        fields["updated_at"] = db.now_iso()
        self.conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(k + "=?" for k in fields),
                          list(fields.values()) + [tid])

    def _insert(self, kind: str, status: str, title: str, text: str, channel: str,
                thread_ts: Optional[str], msg_ts: Optional[str], **extra: Any) -> Optional[str]:
        tid = uuid.uuid4().hex[:8]
        now = db.now_iso()
        cols = dict(id=tid, kind=kind, status=status, title=title, text=text, channel=channel,
                    thread_ts=thread_ts, msg_ts=msg_ts, created_at=now, updated_at=now, **extra)
        try:
            self.conn.execute("INSERT INTO tasks (%s) VALUES (%s)" % (
                ",".join(cols), ",".join("?" * len(cols))), list(cols.values()))
        except sqlite3.IntegrityError:
            return None  # the same Slack event came twice
        return tid

    def create_task(self, channel: str, thread_ts: str, msg_ts: Optional[str], text: str,
                    where: str = DEFAULT_WHERE) -> Optional[str]:
        return self._insert("task", "triage" if self.triage_on() else "queued", clip_title(text), text, channel,
                            thread_ts, msg_ts, where_hint=where if where in WHERE_VALUES else DEFAULT_WHERE)

    def create_handoff(self, title: str, text: str, requester: str) -> Optional[str]:
        """A server session hands one step to a new laptop session (browser, laptop-only MCP). Only the laptop
        claims it (where_hint laptop-only: launch_queued never starts it on the server)."""
        parts = (requester or "").split()
        sid = parts[0] if parts and HANDOFF_SID.fullmatch(parts[0]) else ""
        back = parts[1] if len(parts) > 1 else ""
        body = ("[Paso delegado por una sesión del servidor%s] %s\n\n"
                "Haz solo lecturas y revisiones (navegador, MCP de la laptop). No mandes mensajes, no envíes "
                "formularios y no cambies datos; si hace falta, pídelo al dueño con polar ask. " % (
                    " (" + back + ")" if back else "", text.strip()))
        if sid:
            # By session id, not by tmux name: agent-bus resumes a parked or closed session under a new name
            # (task-w<sid8>), and it follows a handoff successor. The tmux name is only a hint (--place).
            ref = "handoff:" + uuid.uuid4().hex[:12]
            body += ("Al terminar, manda el resultado (máx. 15 líneas) a esa sesión por su id (le llega aunque "
                     "la sesión cambió de nombre, se estacionó o se cerró):\n"
                     "~/.local/bin/agent-bus deliver " + sid + " --ref " + ref + " --from handoff"
                     + (" --place " + back if back else "") + " --text-file - <<'RESULTADO'\n"
                     "<resultado>\nRESULTADO\n"
                     "Revisa que la salida diga «delivered», «queued» o «met»; luego termina con /exit.")
        elif back.startswith(("task-", "release-desk")):
            body += ("Al terminar, manda el resultado (máx. 15 líneas) a esa sesión: "
                     "printf '%s' '<resultado>' | ~/.local/bin/herdr-deliver --tmux " + back + " - ; "
                     "luego termina con /exit.")
        return self._insert("task", "queued", clip_title(title or text), body, OWNER_DM, "", None,
                            where_hint="laptop-only", requester=requester or None)

    def create_agent_task(self, channel: str, thread_ts: str, text: str,
                          where: str = DEFAULT_WHERE) -> Optional[str]:
        """An «Abro un agente» card, owner chose yes. The router already found no owner: skip triage."""
        return self._insert("task", "queued", clip_title(text), text, channel, thread_ts, thread_ts,
                            where_hint=where if where in WHERE_VALUES else DEFAULT_WHERE)

    def create_steer(self, channel: str, thread_ts: Optional[str], msg_ts: Optional[str], text: str,
                     **target: Any) -> Optional[str]:
        return self._insert("steer", "new", clip_title(text), text, channel, thread_ts, msg_ts, **target)

    def orphan_card(self, req: Dict[str, Any], text: str, msg_ts: Optional[str] = None) -> Optional[str]:
        """The owner wrote `text` for a card that names no session (a script, the router, an old card). No session
        would get it, so it starts a task in the card's thread. An «Abrir agente» card starts its agent with the
        text. Any other card goes to triage, which finds the session that owns the subject or opens a new one.
        Return the task id, or None when the card or the message already has its task."""
        channel, thread = req.get("dm_channel") or OWNER_DM, req.get("dm_ts") or ""
        text = text.strip()[:MAX_STEER_CHARS]
        if not thread:
            # A silent report has no DM and so no thread: the notice that starts the task is the thread root.
            thread = self.say(channel, None, self.orphan_said(req))
            if not thread:
                return None
        if req.get("agent_where"):
            brief = req.get("draft_text") or req["title"]
            return self.create_agent_task(channel, thread, "%s\n\nInstrucción del dueño al abrir el agente: %s" % (
                brief, text), req["agent_where"])
        said = "\n".join(x.strip() for x in (req.get("context"), req.get("draft_text")) if x and x.strip())
        who = (req.get("requester") or "").strip()
        body = ("Sobre la tarjeta «%s»: %s\n\n"
                "[Tarjeta de Polar %s (%s) sin sesión%s. El dueño escribió esto en ella y nadie lo recibió. "
                "Si una sesión ya lleva este tema, es de ella. Lee `polar status %s`; si haces una tarjeta nueva "
                "en su lugar, usa --replaces %s.]" % (
                    req["title"], text, req["id"], req["kind"],
                    ", hecha por «%s»" % who if who else "", req["id"], req["id"]))
        if said:
            body += "\n\nLa tarjeta decía:\n" + said[:MAX_CARD_QUOTE]
        return self._insert("task", "triage" if self.triage_on() else "queued", clip_title(body), body, channel,
                            thread, msg_ts or "card:" + req["id"], where_hint=DEFAULT_WHERE)

    def orphan_said(self, req: Dict[str, Any]) -> str:
        """What the owner reads in the card's thread when his text started a task."""
        if req.get("agent_where"):
            return "🤖 Esta tarjeta no tenía sesión: abro el agente con tu instrucción."
        return self.received("🧭 Esta tarjeta no tenía sesión: tu texto abre una tarea.")

    def received(self, head: str) -> str:
        return head + (" Reviso si ya hay una sesión para esto…" if self.triage_on() else " Abro una sesión…")

    def attach(self, found: List[Dict[str, Any]], key: str) -> Tuple[str, str]:
        """Download the files of a message. Return (note for the session, words for the owner's notice)."""
        if not found:
            return "", ""
        try:
            rows = files.save(found, key, self.fetch)
        except Exception as exc:  # a full disk, a bad folder: the message must still reach a session
            log.error("files of %s not saved: %s", key, type(exc).__name__)
            n = len(found)
            return ("Archivos adjuntos que no pude bajar (%d): error al guardarlos en %s." % (n, SERVER_LABEL),
                    " (⚠️ no pude bajar %d)" % n)
        ok = len([r for r in rows if r.get("path")])
        bad = len(rows) - ok
        seen = []
        if ok:
            seen.append("📎 %d archivo%s" % (ok, "" if ok == 1 else "s"))
        if bad:
            seen.append("⚠️ no pude bajar %d" % bad)
        return files.describe(rows, key), " (" + ", ".join(seen) + ")"

    # -- Slack events ------------------------------------------------------
    def on_message(self, ev: Dict[str, Any]) -> None:
        """One `message` event from the owner's DM with Polar. Ignore anything else."""
        if ev.get("subtype") not in OK_SUBTYPES or ev.get("bot_id") or ev.get("user") != self.owner_id:
            return
        if ev.get("channel_type") not in (None, "im"):
            return
        text = (ev.get("text") or "").strip()
        shared = forwarded_text(ev)
        if shared:
            text = (text + "\n\n" if text else "Encárgate de este mensaje reenviado.\n\n") + shared
        found = files.message_files(ev)
        channel, ts, thread = ev.get("channel"), ev.get("ts"), ev.get("thread_ts")
        if not (text or found) or not channel or not ts:
            return
        if self.conn.execute("SELECT 1 FROM tasks WHERE msg_ts=?", (ts,)).fetchone() or \
                self.conn.execute("SELECT 1 FROM picks WHERE source_ts=?", (ts,)).fetchone():
            return
        note, seen = self.attach(found, ts)
        m = ARROW.match(text)
        if m:
            rest = m.group(2).strip()
            if rest or note:
                self.create_steer(channel, thread or ts, ts, with_note(rest, note), target_tab=int(m.group(1)))
            return
        if thread and thread != ts:
            self._steer_thread(channel, thread, ts, with_note(text, note))
            return
        items = [] if note else split_items(text)  # a message with files is one task, never a pick list
        if items:
            self._post_pick(channel, ts, items)
            return
        tid = self.create_task(channel, ts, ts, with_note(text, note))
        if tid:
            self.status_line(tid, self.received("🧭 Recibido%s." % seen))

    def _caught(self, key: str, now: float) -> float:
        """The time up to which a catch-up round already read the DM. First run: one hour back."""
        try:
            return float(db.get_meta(self.conn, key))
        except (TypeError, ValueError):
            return now - 3600

    def _count_taken(self) -> int:
        return self.conn.execute(
            "SELECT (SELECT count(*) FROM tasks) + (SELECT count(*) FROM picks)").fetchone()[0]

    def _catch_up_take(self, m: Dict[str, Any], oldest: float, latest: float, parent: Optional[str] = None) -> bool:
        """Give one missed message to the intake. True when it made a task, a steer or a pick."""
        ts = m.get("ts")
        try:
            t = float(ts)
        except (TypeError, ValueError):
            return False
        if m.get("user") != self.owner_id or m.get("bot_id") or not (oldest < t <= latest) or ts == parent:
            return False
        ev = dict(m, channel=OWNER_DM, channel_type="im")
        if ev.get("files") and not ev.get("subtype"):
            ev["subtype"] = "file_share"  # history leaves the subtype out
        before = self._count_taken()
        self.intake(ev)
        if self._count_taken() > before:
            log.info("catch-up took %s", ts)
            return True
        return False

    def _catch_up_threads(self) -> List[str]:
        found: List[Tuple[str, str]] = []
        for r in self.conn.execute(
                "SELECT dm_ts, created_at FROM requests WHERE status IN ('shown','noted') AND dm_ts IS NOT NULL"
                " AND dm_ts != ''").fetchall():
            found.append((r["created_at"] or "", r["dm_ts"]))
        # «routed» and «parked» stay for ever: read only the threads of tasks that moved in the last 2 days.
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - CATCHUP_TASK_DAYS * 86400))
        for r in self.conn.execute(
                "SELECT thread_ts, created_at FROM tasks WHERE kind='task' AND thread_ts IS NOT NULL"
                " AND thread_ts != '' AND updated_at >= ? AND status IN (%s)" % ",".join("?" * len(OPEN_TASK_STATUSES)),
                (recent,) + OPEN_TASK_STATUSES).fetchall():
            found.append((r["created_at"] or "", r["thread_ts"]))
        out: List[str] = []
        for _, ts in sorted(found, key=lambda x: x[0], reverse=True):
            if ts not in out:
                out.append(ts)
        return out[:CATCHUP_MAX_THREADS]

    def catch_up(self, now: Optional[float] = None, threads: bool = True) -> int:
        """Read the owner's DM again for messages that no live event brought (Polar was down, or the connection
        dropped). Return how many made a task, a steer or a pick. A message Polar already took changes nothing.

        Two marks say how far a round already read: `dm_caught` (top level) and `dm_caught_threads`. Only a round
        that ended with no error moves its mark. A live event never moves them: a message that came live says
        nothing about an older one that was lost."""
        now = time.time() if now is None else now
        latest = now - CATCHUP_SETTLE
        taken = 0
        try:
            # At most one day back: after a long stop, old messages must not start as new tasks.
            oldest = max(self._caught("dm_caught", now), now - CATCHUP_MAX_AGE)
            msgs: List[Dict[str, Any]] = []
            cursor = None
            for _ in range(CATCHUP_PAGES):
                kw: Dict[str, Any] = dict(channel=OWNER_DM, oldest="%.6f" % oldest, limit=200)
                if cursor:
                    kw["cursor"] = cursor
                resp = self.bot.conversations_history(**kw)
                msgs += resp.get("messages") or []
                cursor = (resp.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            for m in sorted(msgs, key=lambda x: float(x.get("ts") or 0)):
                taken += self._catch_up_take(m, oldest, latest)
            if cursor and msgs:
                # More pages than we read (Slack gives the newest first): the older part is still unread, so
                # the mark stays at the oldest message of this round. The next round reads on from there.
                reached = min(float(m.get("ts") or 0) for m in msgs)
                db.set_meta(self.conn, "dm_caught", "%.6f" % max(oldest, min(reached, latest)))
            else:
                db.set_meta(self.conn, "dm_caught", "%.6f" % max(oldest, latest))
            if threads:
                oldest = max(self._caught("dm_caught_threads", now), now - CATCHUP_MAX_AGE)
                for parent in self._catch_up_threads():
                    try:
                        resp = self.bot.conversations_replies(channel=OWNER_DM, ts=parent,
                                                              oldest="%.6f" % oldest, limit=50)
                    except Exception as exc:
                        code = _code(exc)
                        if code == "ratelimited":
                            raise  # stop the round; the mark stays, so the next round reads all threads again
                        # One bad thread (its card message was deleted) must not stop the others.
                        log.warning("catch-up skipped thread %s: %s", parent, code)
                        continue
                    for m in sorted(resp.get("messages") or [], key=lambda x: float(x.get("ts") or 0)):
                        taken += self._catch_up_take(m, oldest, latest, parent)
                db.set_meta(self.conn, "dm_caught_threads", "%.6f" % max(oldest, latest))
        except Exception as exc:
            log.warning("catch-up stopped: %s", _code(exc))
        return taken

    def catch_up_due(self, now: Optional[float] = None) -> None:
        """Run catch_up on the first tick, then top level every 300 s and with threads every 900 s."""
        now = time.time() if now is None else now
        if self._catchup_at is None or self._catchup_threads_at is None \
                or now - self._catchup_threads_at >= CATCHUP_THREADS_EVERY:
            self._catchup_at = self._catchup_threads_at = now
            self.catch_up(now=now, threads=True)
        elif now - self._catchup_at >= CATCHUP_EVERY:
            self._catchup_at = now
            self.catch_up(now=now, threads=False)

    def replay(self, ts: str, channel: str = OWNER_DM) -> bool:
        """Read one DM of the owner again (Polar was down, or an old version dropped it). True when it made a
        task, a steer or a pick. A message that Polar already took changes nothing."""
        resp = self.bot.conversations_history(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
        found = [m for m in resp.get("messages") or [] if m.get("ts") == ts]
        if not found:
            return False
        ev = dict(found[0], channel=channel, channel_type="im")
        if ev.get("files") and not ev.get("subtype"):
            ev["subtype"] = "file_share"  # history leaves the subtype out
        before = self.conn.execute("SELECT (SELECT count(*) FROM tasks) + (SELECT count(*) FROM picks)").fetchone()[0]
        self.on_message(ev)
        after = self.conn.execute("SELECT (SELECT count(*) FROM tasks) + (SELECT count(*) FROM picks)").fetchone()[0]
        return after > before

    def on_mention(self, ev: Dict[str, Any]) -> None:
        """The owner tags @Polar in a channel or thread: the task opens in the owner's DM, with the thread link."""
        if ev.get("bot_id") or ev.get("user") != self.owner_id:
            return
        channel, ts = ev.get("channel"), ev.get("ts")
        if not channel or not ts:
            return
        if self.conn.execute("SELECT 1 FROM tasks WHERE msg_ts=?", (ts,)).fetchone():
            return
        root = ev.get("thread_ts") or ts
        ask = re.sub(r"<@[A-Z0-9]+>", "", ev.get("text") or "").strip() or "Encárgate de este hilo."
        link = blocks.SLACK_URL + "/archives/%s/p%s" % (channel, ts.replace(".", ""))
        if root != ts:
            link += "?thread_ts=%s&cid=%s" % (root, channel)
        text = ("%s\n\nHilo donde me etiquetaste: %s (canal %s, hilo %s). Léelo con "
                "`%s thread %s %s`; si un mensaje del hilo trae `files`, baja cada uno con "
                "`polar files <id del archivo>`." % (ask, link, channel, root, POLAR_BIN, channel, root))
        note, _ = self.attach(files.message_files(ev), ts)
        if note:
            text += "\n\n" + note
        head = self.say(OWNER_DM, None, "🧭 Me etiquetaste: %s\n%s" % (clip_title(ask), link))
        if not head:
            return
        tid = self.create_task(OWNER_DM, head, ts, text)
        if tid:
            self.status_line(tid, self.received("🧭 Recibido."))

    def _steer_thread(self, channel: str, thread: str, ts: str, text: str) -> None:
        text = text[:MAX_STEER_CHARS]
        task = self.conn.execute(
            "SELECT id FROM tasks WHERE kind='task' AND thread_ts=? ORDER BY created_at DESC, rowid DESC",
            (thread,)).fetchone()
        if task:
            self.create_steer(channel, thread, ts, text, target_task=task["id"])
            return
        req = self.conn.execute("SELECT * FROM requests WHERE dm_ts=?", (thread,)).fetchone()
        who = (req["requester"] if req else "") or ""
        if not card_has_reader(dict(req) if req else None):
            if not req:
                return  # not a card thread
            # No session would read it: the message starts a task (or the card's agent) in this thread.
            req = dict(req)
            if self.orphan_card(req, text, msg_ts=ts):
                self.say(channel, thread, self.orphan_said(req))
            return
        who = real_requester(who)  # a prod-op card: the session after the op id
        sid = who.split()[0]
        owner = self.conn.execute(
            "SELECT id FROM tasks WHERE kind='task' AND session_id=?", (sid,)).fetchone()
        if owner:  # the card came from a server task: type into its tmux session
            self.create_steer(channel, thread, ts, text, target_task=owner["id"])
        else:
            self.create_steer(channel, thread, ts, text, requester=who)

    # -- pick card ---------------------------------------------------------
    def _post_pick(self, channel: str, ts: str, items: List[str]) -> None:
        pid = uuid.uuid4().hex[:8]
        try:
            self.conn.execute(
                "INSERT INTO picks (id, channel, source_ts, items_json, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (pid, channel, ts, json.dumps(items, ensure_ascii=False), "planning", db.now_iso()))
        except sqlite3.IntegrityError:
            return
        note = self.say(channel, ts, "🧭 Recibí %d tareas. Las ordeno con lo que ya estamos haciendo…" % len(items))
        if note:
            self.conn.execute("UPDATE picks SET card_ts=? WHERE id=?", (note, pid))

    def get_pick(self, pid: str) -> Optional[Dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM picks WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["items"] = json.loads(d["items_json"])
        d["plan"] = json.loads(d["plan_json"]) if d.get("plan_json") else None
        return d

    def planning_picks(self) -> List[Dict[str, Any]]:
        return [{"id": p["id"], "items": json.loads(p["items_json"]), "created_at": p["created_at"],
                 "source_ts": p["source_ts"]}
                for p in self.conn.execute(
                    "SELECT * FROM picks WHERE status='planning' ORDER BY created_at, rowid").fetchall()]

    def plan_set(self, pid: str, raw: Any) -> Dict[str, Any]:
        """Store the plan of a pick. Raise ValueError with a clear message when it is not valid."""
        pick = self.get_pick(pid)
        if not pick:
            raise ValueError("unknown pick %s" % pid)
        if pick["status"] not in ("planning", "open", "plan_ready"):
            raise ValueError("pick %s has status %s; only planning or open accept a plan" % (pid, pick["status"]))
        plan = validate_plan(raw, len(pick["items"]))
        self.conn.execute(
            "UPDATE picks SET plan_json=?, status='plan_ready', old_status=CASE WHEN status='plan_ready'"
            " THEN old_status ELSE status END WHERE id=?",
            (json.dumps(plan, ensure_ascii=False), pid))
        return plan

    def process_plans(self) -> None:
        """Post the planned card for each pick whose plan arrived."""
        for r in self.conn.execute("SELECT id FROM picks WHERE status='plan_ready' ORDER BY created_at").fetchall():
            pick = self.get_pick(r["id"])
            text = "🧭 Ordené tus %d tareas. ¿Cuáles arranco?" % len(pick["items"])
            bl = blocks.plan_blocks(pick)
            if pick["old_status"] == "open":  # an old plain card exists: keep it, post the plan below it
                card = self.say(pick["channel"], pick["source_ts"], text, bl)
                if not card:
                    continue
                self._edit(pick["channel"], pick["card_ts"], "Tareas",
                           [{"type": "section", "text": {"type": "mrkdwn", "text":
                             "🧭 Encontré %d tareas. _Reemplazada por el plan de abajo._" % len(pick["items"])}}])
                self.conn.execute("UPDATE picks SET status='planned', old_card_ts=card_ts, card_ts=? WHERE id=?",
                                  (card, pick["id"]))
            else:
                if not (pick["card_ts"] and self._edit(pick["channel"], pick["card_ts"], text, bl)):
                    card = self.say(pick["channel"], pick["source_ts"], text, bl)
                    if not card:
                        continue
                    self.conn.execute("UPDATE picks SET card_ts=? WHERE id=?", (card, pick["id"]))
                self.conn.execute("UPDATE picks SET status='planned' WHERE id=?", (pick["id"],))

    def _edit(self, channel: str, ts: Optional[str], text: str, bl: List[Dict[str, Any]]) -> bool:
        if not ts:
            return False
        try:
            self.bot.chat_update(channel=channel, ts=ts, text=text, blocks=bl)
            return True
        except Exception as exc:
            log.error("pick card update failed: %s", _code(exc))
            return False

    def expire_plans(self) -> None:
        """No plan in time: show the plain list."""
        now = time.time()
        for r in self.conn.execute("SELECT id FROM picks WHERE status='planning'").fetchall():
            pick = self.get_pick(r["id"])
            if now - _epoch(pick["created_at"]) < self.plan_timeout():
                continue
            if not self.conn.execute("UPDATE picks SET status='open' WHERE id=? AND status='planning'",
                                     (pick["id"],)).rowcount:
                continue
            text = "🧭 Encontré %d tareas. ¿Cuáles arranco?" % len(pick["items"])
            bl = blocks.pick_blocks(pick, intro="No llegó el plan; aquí está la lista tal cual.")
            if not self._edit(pick["channel"], pick["card_ts"], text, bl):
                card = self.say(pick["channel"], pick["source_ts"], text, bl)
                if card:
                    self.conn.execute("UPDATE picks SET card_ts=? WHERE id=?", (card, pick["id"]))

    def post_original(self, pid: str) -> bool:
        pick = self.get_pick(pid)
        if not pick:
            return False
        lines = ["%d. %s" % (i + 1, " ".join(t.split())) for i, t in enumerate(pick["items"])]
        return bool(self.say(pick["channel"], pick["source_ts"],
                             blocks.clip("📋 *Lista original*\n" + "\n".join(lines), 3000)))

    def start_pick(self, pid: str, chosen: List[int]) -> Optional[List[str]]:
        """Start one task for each checked item. Return the task ids, or None when not possible."""
        pick = self.get_pick(pid)
        if not pick or pick["status"] not in ("open", "planned"):
            return None
        idx = sorted({i for i in chosen if 0 <= i < len(pick["items"])})
        if not idx:
            return None
        if not self.conn.execute("UPDATE picks SET status='started' WHERE id=? AND status IN ('open','planned')",
                                 (pid,)).rowcount:
            return None
        planned = {}
        for g in (pick["plan"] or {}).get("groups", []):
            for it in g["items"]:
                planned[it["i"]] = it
        ids = []
        for i in idx:
            text = pick["items"][i]
            it = planned.get(i) or {}
            sess = it.get("session") or ""
            head = "🧭 Recibido: %s." % clip_title(text)
            root = self.say(pick["channel"], None,
                            head + " Ya tiene sesión; la mando…" if sess else self.received(head))
            if not root:
                continue
            if sess:  # the plan chose the session: skip triage (process_triage posts the route)
                tid = self._insert("task", "matched", clip_title(text), text, pick["channel"], root, None,
                                   match_session=sess, match_label=(it.get("label") or sess)[:80],
                                   match_reason=(it.get("reason") or "")[:160], match_conf="high",
                                   where_hint=it.get("where") if it.get("where") in WHERE_VALUES else DEFAULT_WHERE)
            else:
                tid = self.create_task(pick["channel"], root, None, text, it.get("where") or DEFAULT_WHERE)
            if tid:
                ids.append(tid)
        return ids

    # -- memory and tmux ---------------------------------------------------
    def free_gb(self) -> Optional[float]:
        rc, out = self.sh(["/usr/bin/vm_stat"], 10)
        return parse_free_gb(out) if rc == 0 else None

    def tmux(self, *args: str) -> Tuple[Optional[int], str]:
        return self.sh([self.tmux_bin(), *args], 15)

    def live_sessions(self) -> Optional[List[str]]:
        """Names of tmux sessions that start with task-. None when tmux cannot run."""
        rc, out = self.tmux("list-sessions", "-F", "#{session_name}")
        if rc is None:
            return None
        if rc != 0:
            return []  # no tmux server: no sessions
        return [n for n in out.split() if n.startswith("task-")]

    def slots_used(self, live: List[str]) -> int:
        """How many of the task-* tmux sessions hold a slot. Below the limit: all of them (no bus call). At the
        limit, a session holds a slot only when the bus has it as live, or when it is younger than SLOT_GRACE (it
        did not register yet). A resume that stopped at a start question, or a tmux whose Claude ended, holds no
        slot: on 2026-11-23 three such sessions stopped the queue for 5 hours. When the bus or tmux gives no
        answer, every session counts (the old rule). The free-memory limit stays the same."""
        if len(live) < self.max_tasks():
            return len(live)
        rc, out = self.sh([self.bus_bin(), "sessions", "--json", "--days", "7"], 20)
        if rc != 0:
            return len(live)
        known, rows = set(), 0  # tmux names the bus has as live
        for ln in (out or "").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            rows += 1
            if r.get("state") == "live" and r.get("tmux"):
                known.add(r["tmux"])
        if not rows:
            return len(live)  # an empty answer is not «no session is live»
        rc, out = self.tmux("list-sessions", "-F", "#{session_name} #{session_created}")
        born: Dict[str, float] = {}
        for ln in (out or "").splitlines() if rc == 0 else []:
            name, _, at = ln.strip().partition(" ")
            if at.isdigit():
                born[name] = float(at)
        now = time.time()
        held = [n for n in live if n in known or n not in born or now - born[n] < SLOT_GRACE]
        free = sorted(set(live) - set(held))
        if free != getattr(self, "_no_slot", []):
            self._no_slot = free  # one line for each change, not one for each pass
            log.info("no slot for %d session(s) the bus does not have as live: %s", len(free), " ".join(free) or "-")
        return len(held)

    def ensure_trust(self, work: Path) -> None:
        """Pre-accept Claude Code's folder trust for the work folder (how Claude Code stores it)."""
        path = Path(os.environ.get("POLAR_CLAUDE_JSON") or Path.home() / ".claude.json")
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            proj = data.setdefault("projects", {}).setdefault(str(work), {})
            if proj.get("hasTrustDialogAccepted") and proj.get("hasCompletedProjectOnboarding"):
                return
            proj["hasTrustDialogAccepted"] = True
            proj["hasCompletedProjectOnboarding"] = True
            tmp = path.with_name(path.name + ".polar-tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, path)
        except Exception as exc:
            log.warning("could not pre-accept folder trust: %s", _code(exc))

    # -- triage: does the task belong to an existing session? ----------------
    def triage_set(self, tid: str, match: Optional[str], label: str = "", confidence: str = "",
                   reason: str = "", where: str = DEFAULT_WHERE) -> bool:
        """The laptop router answers. `match=None` means a new session. Only a `triage` task accepts it.
        `where` says where a new session runs: `laptop` (default, with grace) or `server` (no grace)."""
        t = self.get(tid)
        if not t or t["status"] != "triage":
            return False
        if where not in WHERE_VALUES:
            return False
        if not match or confidence not in ("high", "medium"):
            self.update(tid, status="queued", queue_notified=0, where_hint=where)
            return True
        self.update(tid, status="matched", where_hint=where, match_session=match, match_label=label[:80],
                    match_reason=reason[:160], match_conf=confidence)
        return True

    def process_triage(self) -> None:
        for t in self.rows("task", "routefail"):
            self.update(t["id"], status="queued", queue_notified=0)
            self._card_done(t, "No pude entregarlo a %s; abro sesión nueva." % t["match_label"])
        """Post the thread message for each answered triage: route at once (high) or ask (medium)."""
        for t in self.rows("task", "matched"):
            if t["match_conf"] == "high":
                self.update(t["id"], status="routed", route_done=0)
                text = "➡️ Lo mandé a %s: %s" % (t["match_label"], t["match_reason"])
                self.status_line(t["id"], text, blocks.route_blocks(t))  # the «No, sesión nueva» button stays
                ts = (self.get(t["id"]) or {}).get("status_ts")
            else:
                self.update(t["id"], status="confirm")
                ts = self.say(t["channel"], t["thread_ts"],
                              "¿Es de %s? %s" % (t["match_label"], t["match_reason"]),
                              blocks.confirm_blocks(t))
            if ts:
                self.update(t["id"], card_ts=ts)

    def route_failed(self, tid: str) -> bool:
        """The laptop cannot deliver a routed task (the session is gone). Polar opens a new session."""
        cur = self.conn.execute(
            "UPDATE tasks SET status='routefail', updated_at=? WHERE id=? AND kind='task' AND status='routed'"
            " AND route_done=0", (db.now_iso(), tid))
        return cur.rowcount == 1

    def expire_triage(self) -> None:
        now = time.time()
        for t in self.rows("task", "triage"):
            if now - _epoch(t["created_at"]) >= self.triage_timeout():
                self.update(t["id"], status="queued", queue_notified=0)
                self.say(t["channel"], t["thread_ts"], "Sin respuesta de la laptop; abro sesión nueva.")
        for t in self.rows("task", "confirm"):
            if now - _epoch(t["updated_at"]) >= self.confirm_timeout():
                self.update(t["id"], status="queued", queue_notified=0)
                self._card_done(t, "Sin respuesta; abro sesión nueva.")

    def _card_done(self, t: Dict[str, Any], text: str) -> None:
        if not t.get("card_ts"):
            self.say(t["channel"], t["thread_ts"], text)
            return
        try:
            self.bot.chat_update(channel=t["channel"], ts=t["card_ts"], text=text,
                                 blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
            if t["card_ts"] == t.get("status_ts"):
                self.update(t["id"], status_text=text)  # the card is the status message
        except Exception as exc:
            log.error("route card update failed: %s", _code(exc))

    def route_new(self, tid: str) -> bool:
        """The owner says «Sesión nueva» or «No, sesión nueva»."""
        t = self.get(tid)
        if not t or t["status"] not in ("confirm", "routed") or t["route_done"]:
            return False
        self.update(tid, status="queued", queue_notified=0)
        self._card_done(t, "🧭 Sesión nueva. La abro…")
        self.status_line(tid, "🧭 Sesión nueva. La abro…")
        return True

    def route_yes(self, tid: str) -> bool:
        t = self.get(tid)
        if not t or t["status"] != "confirm":
            return False
        self.update(tid, status="routed", route_done=0)
        text = "➡️ Lo mandé a %s: %s" % (t["match_label"], t["match_reason"])
        self._card_done(t, text)
        self.status_line(tid, text)
        return True

    def mark_route_done(self, tid: str) -> bool:
        cur = self.conn.execute(
            "UPDATE tasks SET route_done=1, updated_at=? WHERE id=? AND kind='task' AND status='routed'"
            " AND route_done=0", (db.now_iso(), tid))
        return cur.rowcount == 1

    # -- the loop ----------------------------------------------------------
    def tick(self) -> None:
        for step in (self.catch_up_due, self.reap, self.process_triage, self.expire_triage, self.process_plans, self.expire_plans, self.launch_queued, self.deliver_steers, self.sweep_steers, self.notify_steers, self.notify_claims,
                     self.refresh_final, self.notify_outcomes, self.watch_coord):
            try:
                step()
            except Exception as exc:  # one step must not stop the others
                log.error("task step %s failed: %s", step.__name__, _code(exc))

    def coord_heartbeat(self) -> Path:
        return Path(os.environ.get("POLAR_COORD_HEARTBEAT")
                    or Path.home() / ".local/state/coord-tick/heartbeat").expanduser()

    def watch_coord(self, now: Optional[float] = None) -> None:
        """Watch the watcher: coord-tick touches its heartbeat on each tick. When the beat is stale, DM the owner
        (at most once per COORD_REPEAT), and once more when it beats again. No heartbeat file: not installed, quiet."""
        now = time.time() if now is None else now
        if now - self._coord_checked < COORD_CHECK_EVERY:
            return
        self._coord_checked = now
        try:
            age = now - self.coord_heartbeat().stat().st_mtime
        except OSError:
            return
        if age < COORD_STALE:
            if self._coord_alert_at is not None:
                self._coord_alert_at = None
                self.say(OWNER_DM, None, "✅ Watchdog: coord-tick volvió a correr en %s." % SERVER_LABEL)
            return
        if self._coord_alert_at is not None and now - self._coord_alert_at < COORD_REPEAT:
            return
        self._coord_alert_at = now
        log.error("coord-tick heartbeat is %d min old", int(age // 60))
        self.say(OWNER_DM, None,
                 f"⚠️ Watchdog: coord-tick lleva {int(age // 60)} min sin correr en {SERVER_LABEL}. Sin él no llegan "
                 "respuestas de Slack ni de Polar a las sesiones. Revisa con: "
                 f"`launchctl kickstart -k gui/$(id -u)/{COORD_TICK_LABEL}` en {SERVER_LABEL}.")

    def reap(self) -> None:
        live = self.live_sessions()
        if live is None:
            return
        for t in self.rows("task", "running"):
            if t["host"] == "laptop" or t["tmux"] in live:
                continue  # a laptop task has no tmux session here: the laptop reports its end
            marker = self.parked_dir() / t["tmux"]
            if marker.exists():
                self.update(t["id"], status="parked", finished_at=db.now_iso())
                try:
                    marker.unlink()
                except OSError:
                    pass
                self.status_line(t["id"], "🅿️ En pausa. Se reanuda cuando llegue su espera.")
            else:
                self.update(t["id"], status="done", finished_at=db.now_iso())
                self.finish_line(self.get(t["id"]) or t)

    def launch_queued(self) -> None:
        live = self.live_sessions()
        if live is None:
            return
        queued = self.rows("task", "queued")
        now = time.time()
        used: Optional[int] = None  # read once each tick, and only when a task can start
        for pos, t in enumerate(queued):
            if t.get("where_hint") == "laptop-only":
                continue  # a handoff: only the laptop takes it
            if t.get("where_hint") != "server" and now - _epoch(t["updated_at"]) < self.laptop_grace():
                continue  # the laptop may claim it first
            free = self.free_gb()
            if used is None:
                used = self.slots_used(live)
            ok = used < self.max_tasks() and (free is None or free >= self.min_free_gb())
            if ok:
                self.launch(t)
                return  # one launch each tick: memory settles between starts
            if not t["queue_notified"]:
                self.update(t["id"], queue_notified=1)
                self.status_line(t["id"], "⏳ En cola: %d antes" % (used + pos))

    def launch(self, t: Dict[str, Any]) -> bool:
        claude = self.claude_bin()
        work = self.work_dir()
        name = "task-" + t["id"]
        if not claude:
            self.update(t["id"], status="failed", finished_at=db.now_iso())
            self.say(t["channel"], t["thread_ts"], "⚠️ No encuentro claude en %s." % SERVER_LABEL)
            return False
        # Take the task first: the laptop may claim it in the same moment (overflow). Only one of the two wins.
        cur = self.conn.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=? AND kind='task'"
                                " AND status='queued'", (db.now_iso(), t["id"]))
        if cur.rowcount != 1:
            return False
        work.mkdir(parents=True, exist_ok=True)
        self.ensure_trust(work)
        sid = str(uuid.uuid4())
        brief = BRIEF.format(id=t["id"], text=t["text"], thread=t["thread_ts"],
                             channel=OWNER_DM, session=sid,
                             polar_bin=POLAR_BIN, where=SERVER_NAME or "a headless server",
                             extra_rules=_extra_rules())
        bdir = db.data_dir() / "briefs"
        bdir.mkdir(parents=True, exist_ok=True)
        bfile = bdir / (name + ".txt")
        bfile.write_text(brief)
        path_env = os.pathsep.join([os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin",
                                    "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        extra = os.environ.get("POLAR_CLAUDE_ARGS", "--permission-mode auto")  # same mode as laptop sessions
        cmd = "env PATH=%s POLAR_TASK_ID=%s POLAR_SESSION_ID=%s %s %s %s--session-id %s \"$(cat %s)\"" % (
            shlex.quote(path_env), t["id"], sid, shlex.quote(claude), extra, model_flag(t.get("text") or ""), sid,
            shlex.quote(str(bfile)))
        rc, _ = self.tmux("new-session", "-d", "-s", name, "-c", str(work), cmd)
        if rc != 0:
            self.update(t["id"], status="failed", finished_at=db.now_iso())
            self.say(t["channel"], t["thread_ts"], "⚠️ No pude abrir la sesión en %s." % SERVER_LABEL)
            return False
        self.update(t["id"], status="running", tmux=name, session_id=sid, started_at=db.now_iso())
        self.status_line(t["id"], "▶️ Abrí \"%s\" en %s (%s). En la laptop aparece como pestaña ☁️." % (
            t["title"], SERVER_LABEL, name))
        return True

    def resume_session(self, session_id: str) -> bool:
        """Start `claude --resume <id>` in tmux `task-<first 8>`. Do nothing when that session lives."""
        if not db.SESSION_ID_RE.match(session_id or ""):
            return False
        name = "task-" + session_id[:8]
        live = self.live_sessions()
        claude = self.claude_bin()
        if live is None or name in live or not claude:
            return False
        work = self.work_dir()
        work.mkdir(parents=True, exist_ok=True)
        self.ensure_trust(work)
        path_env = os.pathsep.join([os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin",
                                    "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        extra = os.environ.get("POLAR_CLAUDE_ARGS", "--permission-mode auto")
        cmd = "env PATH=%s POLAR_SESSION_ID=%s %s %s %s--resume %s %s" % (
            shlex.quote(path_env), shlex.quote(session_id), shlex.quote(claude), extra, model_flag(),
            shlex.quote(session_id), shlex.quote(RESUME_PROMPT))
        rc, _ = self.tmux("new-session", "-d", "-s", name, "-c", str(work), cmd)
        return rc == 0

    # -- laptop placement -----------------------------------------------------
    def queue_list(self) -> List[Dict[str, Any]]:
        # overflow: the server could not start it (slots full or low memory), so the laptop may take it even when
        # where_hint is server (coordination slice 6).
        return [dict(public_triage(t), where_hint=t.get("where_hint") or DEFAULT_WHERE,
                     overflow=bool(t.get("queue_notified")))
                for t in self.rows("task", "queued")]

    def claim(self, tid: str, host: str, tab: str, session: str, pane: str = "") -> bool:
        """The laptop takes a queued task. Atomic: only one of laptop and server wins."""
        now = db.now_iso()
        cur = self.conn.execute(
            "UPDATE tasks SET status='running', host=?, session_id=?, requester=?, laptop_tab=?, notified=0,"
            " started_at=?, updated_at=? WHERE id=? AND kind='task' AND status='queued'",
            (host, session, ("%s %s" % (session, pane)).strip(), tab, now, now, tid))
        return cur.rowcount == 1  # notify_claims posts the thread message (the CLI has no Slack client)

    def task_done(self, tid: str) -> bool:
        """The laptop reports that the tab of a laptop task closed."""
        cur = self.conn.execute(
            "UPDATE tasks SET status='done', notified=0, finished_at=?, updated_at=? WHERE id=?"
            " AND kind='task' AND host='laptop' AND status='running'", (db.now_iso(), db.now_iso(), tid))
        return cur.rowcount == 1

    def notify_claims(self) -> None:
        """Post the thread message for a laptop task that started or ended."""
        for t in self.rows("task", "running", "done"):
            if t["host"] != "laptop" or t["notified"]:
                continue
            self.update(t["id"], notified=1)
            if t["status"] == "running":
                self.status_line(t["id"], "▶️ Abrí \"%s\" en la laptop (pestaña %s)." % (t["title"], t["laptop_tab"]))
            else:
                self.finish_line(self.get(t["id"]) or t)

    def laptop_steers(self) -> List[Dict[str, Any]]:
        """Undelivered steers for the laptop: tab steers, and steers for a task that runs on the laptop."""
        out = []
        for s in self.rows("steer", "new"):
            if s["target_task"]:
                t = self.get(s["target_task"])
                if not t or t["host"] != "laptop" or t["status"] != "running":
                    continue
                s["requester"] = t["requester"]
            out.append(public_steer(s))
        return out

    # -- steering ----------------------------------------------------------
    def type_into(self, name: str, text: str) -> bool:
        """Type one line into a tmux session and press Enter."""
        line = " ".join(text.split())[:MAX_STEER_CHARS]
        rc, _ = self.tmux("send-keys", "-t", "=" + name + ":", "-l", "--", line)
        if rc != 0:
            return False
        self.sleep(0.5)
        rc, _ = self.tmux("send-keys", "-t", "=" + name + ":", "Enter")
        return rc == 0

    def deliver_steers(self) -> None:
        for s in self.rows("steer", "new"):
            if not s["target_task"]:
                continue  # a laptop steer: the laptop relay takes it
            t = self.get(s["target_task"])
            if t and t["host"] == "laptop":
                continue  # the laptop delivers it (`polar steers`)
            if not t:
                self.rescue_steer(s, "la tarea ya no existe")
                continue
            if t["status"] == "queued":
                continue
            if t["status"] in ("parked", "done") and t.get("session_id"):
                self.send_via_bus(s, t["session_id"], t.get("tmux") or "")  # the bus resumes it
                continue
            if t["status"] != "running":
                self.rescue_steer(s, "la sesión está en estado «%s»" % t["status"], t)
                continue
            if self.type_into(t["tmux"], s["text"]):
                self.update(s["id"], status="delivered", notified=1)
                self.update(t["id"])  # new activity: the laptop may open its tab again
                self.say(s["channel"], s["thread_ts"], "↪️ Enviado a la sesión")

    # -- steers that no relay took ----------------------------------------------
    def relay_wait(self) -> float:
        """How long a steer waits for the laptop relay: three of its polls (a poll that is slow or skipped, then
        one more), so a live laptop session is never typed into twice."""
        return 3 * env_float("POLAR_RELAY_POLL", RELAY_POLL)

    def bus_bin(self) -> str:
        return os.environ.get("AGENT_BUS_BIN") or str(Path.home() / ".local/bin/agent-bus")

    def bus_deliver(self, sid: str, text: str, place: str, ref: str,
                    channel: str = "", thread: str = "") -> Tuple[bool, str]:
        """Hand `text` to a session through `agent-bus deliver`. It types into a live session, queues for the laptop,
        or resumes a parked or closed one. Return (the bus took it, the reason in Spanish when not). «queued»
        means the bus holds it and tries again by itself; --ref makes a repeat do nothing. «router inbox» means
        the bus found no place to type and gave the text to the Slack router, which finds the session: the reason
        is then ROUTER_HAS_IT and the owner must not write it again (two copies)."""
        argv = [self.bus_bin(), "deliver", sid, "--from", "polar", "--ref", ref,
                "--text", "Polar (owner): " + " ".join(text.split())[:MAX_STEER_CHARS]
                + (("\n\n[" + ANSWER_RULE.format(thread=thread, channel=channel) + "]") if thread and channel
                   else "")]
        if place and re.fullmatch(r"[\w.:-]+", place):
            argv += ["--place", place]
        rc, out = self.sh(argv, BUS_TIMEOUT)
        out = (out or "").strip()
        if rc is None:
            return False, "el bus no contestó a tiempo; puede que sí llegara"
        if rc != 0:
            return False, "el bus no conoce esa sesión"
        if out.startswith(("delivered", "left in", "met ", "queued", "not typed")):
            return True, ""
        if out.startswith("duplicate"):  # «duplicate of N: <first outcome>»
            return True, ROUTER_HAS_IT if "router inbox" in out else ""
        if out.startswith("router inbox"):
            return True, ROUTER_HAS_IT
        return False, "no encontré dónde escribirle a esa sesión"

    def send_via_bus(self, s: Dict[str, Any], sid: str, place: str = "") -> bool:
        """Claim one steer (atomic: `polar steer-done` and the sweep cannot both take it), hand it to the bus, and
        mark it. On failure the owner reads a line and the steer is dropped. Return True when the bus took it."""
        cur = self.conn.execute(
            "UPDATE tasks SET status='sending', updated_at=? WHERE id=? AND kind='steer' AND status='new'",
            (db.now_iso(), s["id"]))
        if cur.rowcount != 1:
            return False
        ok, why = self.bus_deliver(sid, s["text"], place, "polar-steer:" + s["id"],
                                  s.get("channel") or "", s.get("thread_ts") or "")
        if ok:
            self.update(s["id"], status="delivered", notified=1)
            self.say(s["channel"], s["thread_ts"], why or "↪️ Enviado a la sesión")
            return True
        self.rescue_steer(s, why)
        return False

    def _steer_context(self, s: Dict[str, Any], t: Optional[Dict[str, Any]]) -> Tuple[str, str]:
        """(what the owner answers, how it ended) for a steer whose session cannot take it."""
        if t is None and s.get("target_task"):
            t = self.get(s["target_task"])
        if t:
            r = self.report_for(t)
            last = ("informe «%s»: %s" % (r["title"], (r.get("draft_text") or "")[:MAX_CARD_QUOTE // 3])) if r \
                else "sin informe (estado %s)" % t["status"]
            return "la tarea «%s»" % t["title"], last
        req = self.conn.execute("SELECT * FROM requests WHERE dm_ts=?", (s.get("thread_ts"),)).fetchone() \
            if s.get("thread_ts") else None
        if req:
            req = db._row(req)
            return "la tarjeta «%s»" % req["title"], outcome_line(req)
        return "este hilo", "sin dato"

    def rescue_steer(self, s: Dict[str, Any], why: str = "", t: Optional[Dict[str, Any]] = None) -> bool:
        """A thread reply never drops. When no session can take the steer, the text opens a new task in the same
        thread (title and last outcome of the old task as context) and the owner reads which way it went. Only a
        hard error leaves the text in the thread so it is not lost. Return True when a task started."""
        cur = self.conn.execute(
            "UPDATE tasks SET status='rescued', notified=1, updated_at=? WHERE id=? AND kind='steer'"
            " AND status IN ('new','sending')", (db.now_iso(), s["id"]))
        if cur.rowcount != 1:
            return False
        tid: Optional[str] = None
        try:
            what, last = self._steer_context(s, t)
            text = (s["text"] or "").strip()[:MAX_STEER_CHARS]
            body = ("Sobre %s: %s\n\n[Polar: el dueño escribió esto en el hilo de %s, pero su sesión ya no lo recibe%s. "
                    "Último resultado: %s. Si una sesión ya lleva este tema, es de ella.]" % (
                        what, text, what, " (%s)" % why if why else "", last))
            if s.get("thread_ts"):
                tid = self._insert("task", "triage" if self.triage_on() else "queued", clip_title(body), body,
                                   s["channel"], s["thread_ts"], "steer:" + s["id"], where_hint=DEFAULT_WHERE)
        except Exception:
            log.exception("steer %s could not open a task", s["id"])
        if tid:
            self.status_line(tid, self.received(
                "🧭 La sesión de esta conversación ya no recibe mensajes: tu texto abre una tarea nueva aquí."))
            return True
        self.update(s["id"], status="dropped", notified=1)
        self.say(s["channel"], s["thread_ts"],
                 "⚠️ Error: no pude abrir una tarea con tu mensaje. Tu texto, para que no se pierda:\n> %s"
                 % " ".join((s["text"] or "").split())[:MAX_STEER_CHARS])
        return False

    def sweep_steers(self) -> None:
        """A steer for a laptop session that the relay did not take within `relay_wait` goes through the bus. A steer
        older than 24 hours becomes `expired` with no message. Never lose a steer in silence."""
        now = time.time()
        for s in self.rows("steer", "sending"):
            if now - _epoch(s["updated_at"]) >= SENDING_STALE:  # the process died after the claim
                self.rescue_steer(s, "no pude confirmar el envío")
        for s in self.rows("steer", "new"):
            age = now - _epoch(s["created_at"])
            if age >= STEER_EXPIRE:
                self.rescue_steer(s, "nadie lo tomó en 24 horas")
                continue
            if age < self.relay_wait() or s["target_tab"]:
                continue  # a tab steer names a pane number: only the relay can type it
            t = self.get(s["target_task"]) if s["target_task"] else None
            if s["target_task"]:
                if not t or t["host"] != "laptop":
                    continue  # a server task: deliver_steers owns it
                req = (t.get("requester") or "").split()
                sid, place = t.get("session_id") or "", (req[1] if len(req) > 1 else "")
            else:
                parts = real_requester(s["requester"]).split()
                sid, place = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
            if not SESSION_ID.fullmatch(sid):
                self.rescue_steer(s, "la tarjeta no dice quién la hizo", t)
                continue
            self.send_via_bus(s, sid, place)

    def notify_steers(self) -> None:
        """Tell the owner about steers that the laptop delivered."""
        for s in self.conn.execute(
                "SELECT * FROM tasks WHERE kind='steer' AND status='delivered' AND notified=0").fetchall():
            s = dict(s)
            where = "a la pestaña %d" % s["target_tab"] if s["target_tab"] else "a la sesión"
            self.update(s["id"], notified=1)
            self.say(s["channel"], s["thread_ts"], "↪️ Enviado %s" % where)

    def mark_steer_done(self, sid: str) -> bool:
        cur = self.conn.execute(
            "UPDATE tasks SET status='delivered', updated_at=? WHERE id=? AND kind='steer' AND status='new'",
            (db.now_iso(), sid))
        return cur.rowcount == 1

    # -- card outcomes for server sessions ----------------------------------
    def notify_outcomes(self) -> None:
        """Type the owner's answer into the session that asked (requester «<sid> task-<id>»)."""
        for t in self.rows("task", "running"):
            if t["host"] == "laptop":
                continue
            qs = ",".join("?" * len(FINAL))
            rows = self.conn.execute(
                "SELECT * FROM requests WHERE requester LIKE ? AND status IN (%s)"
                " AND id NOT IN (SELECT request_id FROM task_notes)" % qs,
                ["%task-" + t["id"]] + list(FINAL)).fetchall()
            for r in rows:
                r = db._row(r)
                if r["kind"] == "report" and r["status"] in ("chosen", "discarded", "expired"):
                    # «Visto» or «Reanudar» on a report: nothing for the session to do
                    self.conn.execute("INSERT OR IGNORE INTO task_notes VALUES (?)", (r["id"],))
                    continue
                if not self.type_into(t["tmux"], outcome_line(r)):
                    n = self._type_fails.get(r["id"], 0)
                    self._type_fails[r["id"]] = n + 1
                    if n % 60 == 0:  # first failure, then every 60th
                        log.warning("could not type the answer of card %s into %s (%d failures)",
                                    r["id"], t["tmux"], n + 1)
                    continue  # not noted: the next tick tries again
                self._type_fails.pop(r["id"], None)
                self.conn.execute("INSERT OR IGNORE INTO task_notes VALUES (?)", (r["id"],))
                self.update(t["id"])


def with_note(text: str, note: str) -> str:
    """The owner's text and the files note, inside the steer limit: the text is cut, never the note."""
    text = text.strip()
    if not note:
        return text[:MAX_STEER_CHARS]
    room = max(0, MAX_STEER_CHARS - len(note) - 2)
    return (text or ONLY_FILES)[:room] + "\n\n" + note


def forwarded_text(ev: Dict[str, Any]) -> str:
    """The messages the owner forwarded into the DM (Slack «Reenviar» puts them in attachments)."""
    parts = []
    for a in ev.get("attachments") or []:
        if not (a.get("is_share") or a.get("is_msg_unfurl") or a.get("from_url")):
            continue
        body = (a.get("text") or a.get("fallback") or "").strip()
        if not body:
            continue
        who = a.get("author_name") or a.get("author_subname") or "alguien"
        where = (" en #" + a["channel_name"]) if a.get("channel_name") else ""
        link = a.get("from_url") or ""
        parts.append("Mensaje reenviado de %s%s%s: «%s»" % (who, where, (" (" + link + ")") if link else "",
                                                          body[:3000]))
    return "\n\n".join(parts)


def outcome_line(r: Dict[str, Any]) -> str:
    res = r.get("result") or {}
    st = r["status"]
    if st == "chosen":
        what = "el dueño eligió «%s»" % res.get("option")
    elif st == "sent":
        what = "enviado %s" % (res.get("permalink") or "")
    elif st == "revise":
        what = "pide cambios: %s. Escribe una tarjeta nueva con --replaces %s" % (
            res.get("instructions") or "", r["id"])
    elif st == "answered":
        parts = ["%s = %s" % (q["key"], (res.get("answers", {}).get(q["key"]) or {}).get("text", "(sin respuesta)"))
                 for q in r.get("options") or []]
        what = "; ".join(parts) + (("; nota: " + res["note"]) if res.get("note") else "")
    else:
        what = st
    return "Polar: tarjeta %s «%s» → %s" % (r["id"], r["title"], what.strip())


PLAN_KEYS = ("now", "running", "later", "done", "drop")
ITEM_FIELDS = {"i", "title", "reason", "session", "label", "checked", "where"}
WHERE_VALUES = ("laptop", "server")


def validate_plan(raw: Any, n: int) -> Dict[str, Any]:
    """Check a plan and return it clean (texts clipped). Raise ValueError with a clear message."""
    if not isinstance(raw, dict):
        raise ValueError("plan must be a JSON object")
    extra = set(raw) - {"summary", "groups"}
    if extra:
        raise ValueError("unknown plan field(s): %s" % ", ".join(sorted(extra)))
    if not isinstance(raw.get("groups"), list):
        raise ValueError("plan needs a `groups` list")
    summary = raw.get("summary") or ""
    if not isinstance(summary, str):
        raise ValueError("`summary` must be text")
    groups: List[Dict[str, Any]] = []
    seen: List[int] = []
    for g in raw["groups"]:
        if not isinstance(g, dict) or set(g) - {"key", "items"}:
            raise ValueError("each group needs only `key` and `items`")
        if g.get("key") not in PLAN_KEYS:
            raise ValueError("unknown group key %r; use one of %s" % (g.get("key"), ", ".join(PLAN_KEYS)))
        if not isinstance(g.get("items"), list):
            raise ValueError("group %s needs an `items` list" % g["key"])
        items = []
        for it in g["items"]:
            if not isinstance(it, dict) or set(it) - ITEM_FIELDS:
                raise ValueError("unknown item field(s) in group %s: %s" % (
                    g["key"], ", ".join(sorted(set(it) - ITEM_FIELDS)) if isinstance(it, dict) else "not an object"))
            i = it.get("i")
            if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n:
                raise ValueError("item index %r is not in 0..%d" % (i, n - 1))
            if not isinstance(it.get("title"), str) or not it["title"].strip():
                raise ValueError("item %d needs a `title`" % i)
            for f in ("reason", "session", "label"):
                if not isinstance(it.get(f, ""), str):
                    raise ValueError("item %d: `%s` must be text" % (i, f))
            if it.get("where", DEFAULT_WHERE) not in WHERE_VALUES:
                raise ValueError("item %d: `where` must be laptop or server" % i)
            seen.append(i)
            items.append({"i": i, "where": it.get("where", DEFAULT_WHERE), "title": blocks.clip(" ".join(it["title"].split()), TITLE_MAX),
                          "reason": blocks.clip(" ".join((it.get("reason") or "").split()), 140),
                          "session": (it.get("session") or "").strip(),
                          "label": blocks.clip(" ".join((it.get("label") or "").split()), 80),
                          "checked": bool(it.get("checked"))})
        groups.append({"key": g["key"], "items": items})
    dup = sorted({i for i in seen if seen.count(i) > 1})
    if dup:
        raise ValueError("item index appears more than once: %s" % ", ".join(map(str, dup)))
    missing = [i for i in range(n) if i not in seen]
    if missing:
        raise ValueError("item index missing from the plan: %s" % ", ".join(map(str, missing)))
    return {"summary": blocks.clip(" ".join(summary.split()), 300), "groups": groups}


def public_task(t: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "title", "status", "host", "tmux", "session_id", "thread_ts", "channel",
            "created_at", "started_at", "finished_at")
    d = {k: t.get(k) for k in keys}
    d["activity_at"] = t.get("updated_at")
    return d


def public_triage(t: Dict[str, Any]) -> Dict[str, Any]:
    return {k: t.get(k) for k in ("id", "text", "title", "thread_ts", "channel", "created_at")}


def public_route(t: Dict[str, Any]) -> Dict[str, Any]:
    return {k: t.get(k) for k in ("id", "text", "thread_ts", "channel", "match_session", "match_label",
                                  "match_reason", "created_at")}


def _epoch(iso: str) -> float:
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0.0


def public_steer(s: Dict[str, Any]) -> Dict[str, Any]:
    return {k: s.get(k) for k in ("id", "text", "status", "target_tab", "target_task", "requester",
                                  "channel", "thread_ts", "created_at")}


def _code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    try:
        code = resp["error"] if resp is not None else None
    except (KeyError, TypeError):
        code = None
    return str(code or type(exc).__name__)[:80]
