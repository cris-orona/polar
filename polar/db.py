"""The request queue. SQLite only. The CLI and the app share this module."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

KINDS = ("reply", "decision", "questions", "email", "report")
REPLY_KINDS = ("reply", "email")  # cards with text that the owner can send or edit
OPEN_STATUSES = ("pending", "shown")
FINAL_STATUSES = ("sent", "chosen", "discarded", "expired", "failed", "answered", "skipped",
                  "revise", "replaced", "filed")
DECIDED_STATUSES = ("sent", "chosen", "discarded", "answered")
DEFAULT_EXPIRY_HOURS = 24
QUESTIONS_EXPIRY_HOURS = 24 * 7
REPORT_EXPIRY_HOURS = 72
REPORT_FOLD_HOURS = 24  # a posted report («noted») folds to one line after this many hours
MAX_REPORT_LINKS = 5
MAX_REPORT_NEXT = 3
MAX_NEXT_CHARS = 70
MAX_REPORT_FILES = 10
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
URL_RE = re.compile(r"^https?://\S+$")
MAX_QUESTIONS = 5
MAX_QUESTION_OPTIONS = 4
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MAX_REPLY_CHARS = 2600  # fits one Slack quote block and the edit modal

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    requester TEXT,
    title TEXT NOT NULL,
    context TEXT,
    draft_text TEXT,
    final_text TEXT,
    target_channel TEXT,
    target_thread_ts TEXT,
    options_json TEXT,
    result_json TEXT,
    dm_channel TEXT,
    dm_ts TEXT,
    expires_at TEXT NOT NULL,
    dm_done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,            -- small state of the app: one JSON value per key
    value TEXT NOT NULL
);
"""

TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- task | steer
    status TEXT NOT NULL,            -- task: triage matched confirm routed queued running done failed parked; steer: new sending delivered dropped expired
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    channel TEXT,
    thread_ts TEXT,                  -- task: the thread of the task; steer: where the notice goes
    msg_ts TEXT UNIQUE,              -- the owner's Slack message (stops a double event)
    target_tab INTEGER,
    target_task TEXT,
    requester TEXT,
    tmux TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    queue_notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(kind, status);
CREATE TABLE IF NOT EXISTS picks (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    source_ts TEXT UNIQUE,
    card_ts TEXT,
    items_json TEXT NOT NULL,
    status TEXT NOT NULL,            -- planning | plan_ready | planned | open | started
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_notes (request_id TEXT PRIMARY KEY);
"""

# Columns added after the first release. connect() adds the missing ones to old databases.
NEW_COLUMNS = {
    "requester_label": "TEXT",
    "reply_to_ts": "TEXT",
    "orig_name": "TEXT",
    "orig_time": "TEXT",
    "orig_text": "TEXT",
    "orig_permalink": "TEXT",
    "orig_channel_name": "TEXT",
    "reopened_at": "TEXT",
    "replaces": "TEXT",
    "agent_where": "TEXT",  # set on an «Abro un agente» card: where the new session runs
    "email_cc": "TEXT",
    "email_subject": "TEXT",
    "extras_json": "TEXT",  # decision/questions: {"body": md, "links": [...], "files": [...]}
    "compact": "INTEGER NOT NULL DEFAULT 0",  # the body step (blocks.BODY_LIMITS) that Slack accepted
    "and_continue": "INTEGER NOT NULL DEFAULT 0",  # decision chosen with «Sí, y sigue»: the session goes on with the plan
    "silent": "INTEGER NOT NULL DEFAULT 0",  # `polar report --silent`: a machine notice; Mando keeps it out of Foco
    "followups_json": "TEXT",  # scheduled follow-ups shown on the card: [{"id","phrase","text","by","state"}] (see polar/followups.py)
}


TASK_NEW_COLUMNS = {
    "match_session": "TEXT",
    "match_label": "TEXT",
    "match_reason": "TEXT",
    "match_conf": "TEXT",
    "route_done": "INTEGER NOT NULL DEFAULT 0",
    "card_ts": "TEXT",
    "laptop_tab": "TEXT",
    "host": "TEXT NOT NULL DEFAULT '%s'" % (
        "".join(c for c in (os.environ.get("POLAR_SERVER_NAME") or "server") if c.isalnum() or c in "-_.") or "server"),
    "where_hint": "TEXT NOT NULL DEFAULT 'laptop'",
    "status_ts": "TEXT",    # the one status message of the task (edited in place)
    "status_text": "TEXT",  # what that message says now
    "area": "TEXT NOT NULL DEFAULT 'work'",  # the folder of a new session (tasks.AREAS)
}


PICK_NEW_COLUMNS = {
    "plan_json": "TEXT",
    "old_status": "TEXT",
    "old_card_ts": "TEXT",
}


def migrate(conn: Any) -> None:
    have_p = {r["name"] for r in conn.execute("PRAGMA table_info(picks)").fetchall()}
    for name, typ in PICK_NEW_COLUMNS.items():
        if have_p and name not in have_p:
            conn.execute(f"ALTER TABLE picks ADD COLUMN {name} {typ}")
    have_t = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for name, typ in TASK_NEW_COLUMNS.items():
        if have_t and name not in have_t:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {typ}")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}
    for name, typ in NEW_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {typ}")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_dir() -> Path:
    env = os.environ.get("POLAR_DATA_DIR")
    return Path(env).expanduser() if env else Path.home() / "polar-slack" / "data"


def db_path() -> Path:
    return data_dir() / "polar.db"


class _Result:
    """The rows of one statement, read while the lock was held."""

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._rows = cur.fetchall()
        self.rowcount = cur.rowcount
        self.lastrowid = cur.lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class LockedConnection:
    """One SQLite connection shared by the tick thread and the Slack handler threads.

    Each statement runs and reads all its rows under one lock, so no two threads use the
    connection at the same time."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, args=()) -> _Result:
        with self._lock:
            return _Result(self._conn.execute(sql, args))

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def connect(path: Optional[Path] = None) -> LockedConnection:
    path = Path(path) if path else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    conn = LockedConnection(raw)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.executescript(TASK_SCHEMA)
    migrate(conn)
    return conn


def get_meta(conn: Any, key: str, default: Any = None) -> Any:
    """Read one JSON value of the app state. Return `default` when the key is not set."""
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def set_meta(conn: Any, key: str, value: Any) -> None:
    """Save one JSON value of the app state. None removes the key."""
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
        return
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


def _row(r: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    d = dict(r)
    d["options"] = json.loads(d["options_json"]) if d.get("options_json") else []
    # A report keeps links, files and the session to resume in a dict. `options` stays a list.
    d["report"] = d["options"] if isinstance(d["options"], dict) else {}
    if d["report"]:
        d["options"] = []
    d["extra"] = json.loads(d["extras_json"]) if d.get("extras_json") else {}
    d["result"] = json.loads(d["result_json"]) if d.get("result_json") else {}
    d["followups"] = json.loads(d["followups_json"]) if d.get("followups_json") else []
    return d


def validate_questions(raw: Any) -> List[Dict[str, Any]]:
    """Check a question list. Return a clean copy. Raise ValueError with a short reason."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_QUESTIONS:
        raise ValueError("questions must be a list of 1 to %d items" % MAX_QUESTIONS)
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, q in enumerate(raw, 1):
        if not isinstance(q, dict):
            raise ValueError(f"question {i} must be an object")
        key, text = q.get("key"), q.get("text")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise ValueError(f"question {i} needs a key of letters, digits, - or _")
        if key in seen:
            raise ValueError("question keys must be unique")
        seen.add(key)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"question {i} needs text")
        opts = q.get("options") or []
        if not isinstance(opts, list) or len(opts) > MAX_QUESTION_OPTIONS:
            raise ValueError(f"question {i} takes 0 to {MAX_QUESTION_OPTIONS} options")
        clean = []
        for o in opts:
            if not (isinstance(o, dict) and isinstance(o.get("key"), str) and KEY_RE.match(o["key"])
                    and isinstance(o.get("label"), str) and o["label"].strip()):
                raise ValueError(f"question {i} has a bad option (needs key and label)")
            clean.append({"key": o["key"], "label": o["label"].strip()})
        if len({o["key"] for o in clean}) != len(clean):
            raise ValueError(f"question {i} option keys must be unique")
        item: Dict[str, Any] = {"key": key, "text": text.strip(), "options": clean}
        rec = q.get("recommended")
        if rec is not None:
            if rec not in [o["key"] for o in clean]:
                raise ValueError(f"question {i} recommended must be one of its option keys")
            item["recommended"] = rec
        out.append(item)
    return out


# A session report must say what was checked: a heading such as «Qué revisé», «Qué revisé y qué no», «Qué comprobé»,
# «Pruebas», «Verificación» (or a bold line with those words), with some text under it. The owner reads the evidence
# on the card, and an «it is done» with no check is the failure that costs the most later. POLAR_REPORT_EVIDENCE=off
# turns the rule off.
EVIDENCE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|\*\*)[^\n]*?(revis|comprob|verifi[cq]|prueba|prob[eé]|valid|evidenc|checked|verified|tested)", re.I | re.M)
EVIDENCE_MIN_CHARS = 12
EVIDENCE_HINT = ("a report from a session needs a section that says what you checked and what you did not check: "
                 "a heading such as «## Qué revisé y qué no» with the tests, queries or screenshots that prove the "
                 "result (at least %d characters under it). Add it and send the report again." % EVIDENCE_MIN_CHARS)


def evidence_missing(text: str) -> bool:
    """True when the report text has no «what I checked» section with some text under it."""
    for m in EVIDENCE_HEADING_RE.finditer(text or ""):
        line_end = text.find("\n", m.end())
        rest = text[line_end + 1:] if line_end >= 0 else ""
        tail = text[m.end():line_end if line_end >= 0 else len(text)].split("**", 1)
        inline = tail[1] if len(tail) > 1 else ""  # «**Qué revisé:** pruebas en verde» keeps the text on the line
        bold_start = not text[m.start():m.end()].lstrip().startswith("#")
        # A heading ends the section; a bold-only line ends it only when the section itself began as a bold line.
        stop = r"^\s*(?:#{1,6}\s|\*\*[^\n]*\*\*\s*$)" if bold_start else r"^\s*#{1,6}\s"
        nxt = re.search(stop, rest, re.M)
        body = inline + (rest[:nxt.start()] if nxt else rest)
        if len(re.sub(r"\s+", "", body)) >= EVIDENCE_MIN_CHARS:
            return False
    return True


def check_report_evidence(text: str, requester: Optional[str], silent: bool) -> None:
    """Refuse a session report with no «what I checked» section. Scripts, the release desk and silent notices are
    exempt: they report machine facts, not work that a person must trust."""
    if silent or os.environ.get("POLAR_REPORT_EVIDENCE", "").lower() == "off":
        return
    from polar.tasks import SESSION_ID
    parts = (requester or "").split()
    if parts[:1] == ["prod-op"]:
        parts = parts[2:]
    if not parts or not SESSION_ID.fullmatch(parts[0]):
        return
    if evidence_missing(text):
        raise ValueError(EVIDENCE_HINT)


def validate_report(links: Any, files: Any, resume: Optional[str], nxt: Any = None) -> Dict[str, Any]:
    """Check the extras of a report card. Return the clean dict. Raise ValueError with a short reason."""
    links, files = list(links or []), list(files or [])
    if len(links) > MAX_REPORT_LINKS:
        raise ValueError("report takes at most %d links" % MAX_REPORT_LINKS)
    if len(files) > MAX_REPORT_FILES:
        raise ValueError("report takes at most %d files" % MAX_REPORT_FILES)
    clean = []
    for item in links:
        label, sep, url = str(item).partition("=")
        label, url = label.strip(), url.strip()
        if not sep or not label or not URL_RE.match(url):
            raise ValueError("bad --link %r: use \"Label=https://...\" (http or https only)" % str(item)[:60])
        clean.append({"label": label[:70], "url": url})
    if resume is not None and not SESSION_ID_RE.match(resume):
        raise ValueError("bad --resume: need a session id")
    nxt = list(nxt or [])
    if len(nxt) > MAX_REPORT_NEXT:
        raise ValueError("report takes at most %d --next suggestions" % MAX_REPORT_NEXT)
    steps = []
    for item in nxt:
        t = str(item).strip()
        if not t or len(t) > MAX_NEXT_CHARS:
            raise ValueError("bad --next %r: use 1 to %d characters" % (str(item)[:30], MAX_NEXT_CHARS))
        steps.append(t)
    out: Dict[str, Any] = {"links": clean, "files": [str(f) for f in files]}
    if resume:
        out["resume"] = resume
    if steps:
        out["next"] = steps
    return out


DEFAULT_OWNER_DM = "D0000000000"  # placeholder: set POLAR_OWNER_DM. Polar stores the channel it really uses in meta


def owner_dm_channel(conn: Any = None) -> str:
    """The owner's DM channel with Polar: the one Polar posted in last (meta `owner_dm`), else env, else the default."""
    if conn is not None:
        try:
            known = get_meta(conn, "owner_dm")
        except Exception:
            known = None
        if known:
            return str(known)
    return os.environ.get("POLAR_OWNER_DM") or DEFAULT_OWNER_DM


def create_request(
    conn: sqlite3.Connection,
    kind: str,
    title: str,
    context: str = "",
    requester: str = "",
    draft_text: Optional[str] = None,
    target_channel: Optional[str] = None,
    target_thread_ts: Optional[str] = None,
    options: Optional[List[Dict[str, str]]] = None,
    expires_hours: float = DEFAULT_EXPIRY_HOURS,
    requester_label: Optional[str] = None,
    reply_to_ts: Optional[str] = None,
    replaces: Optional[str] = None,
    agent_where: Optional[str] = None,
    email_cc: Optional[str] = None,
    report: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    silent: bool = False,
    canary: bool = False,
    followups: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Insert a request. `canary` (decision only) marks the hourly end-to-end test card: extras_json gets
    "canary": true; Mando hides it and no DM is sent. `silent` (reports only) stores it as already posted: status «noted», dm_done=1 and no
    dm_ts, so deliver_pending never sends it. Mando and the web API still read it as a posted report."""
    if kind not in KINDS:
        raise ValueError("bad kind")
    if kind == "reply" and target_channel and target_channel == owner_dm_channel(conn):
        # An answer to the owner is never an editable reply card: it is a report in the same thread.
        kind, report = "report", report or {}
    if silent and kind != "report":
        raise ValueError("only a report can be silent")
    if silent and (report or {}).get("files"):
        raise ValueError("a silent report has no thread: --file needs a DM")
    if kind in REPLY_KINDS:
        if kind == "email":
            target_channel = "gmail"
            if not target_thread_ts:
                raise ValueError("email needs a thread id")
        elif not target_channel:
            raise ValueError("reply needs a target channel")
        if not (draft_text or "").strip():
            raise ValueError("reply needs text")
        if len(draft_text) > MAX_REPLY_CHARS:
            raise ValueError("reply text is over %d characters" % MAX_REPLY_CHARS)
    elif kind == "questions":
        options = validate_questions(options)
    elif kind == "report":
        if not (draft_text or "").strip():
            raise ValueError("report needs text")
        rep = report or {}
        options = validate_report(rep.get("links"), rep.get("files"), rep.get("resume"), rep.get("next"))  # type: ignore[assignment]
    else:
        options = options or []
        if not 2 <= len(options) <= 4:
            raise ValueError("decision needs 2 to 4 options")
        keys = [o["key"] for o in options]
        if len(set(keys)) != len(keys):
            raise ValueError("option keys must be unique")
    extras_json = None
    if canary:
        if kind != "decision":
            raise ValueError("only a decision can be a canary")
        extras_json = json.dumps({"body": "canary", "links": [], "files": [], "canary": True})
    elif extra and kind in ("decision", "questions"):
        body = (extra.get("body") or "").strip()
        clean = validate_report(extra.get("links"), extra.get("files"), None)
        if body or clean["links"] or clean["files"]:
            extras_json = json.dumps({"body": body, "links": clean["links"], "files": clean["files"]},
                                     ensure_ascii=False)
    rid = uuid.uuid4().hex[:12]
    created = datetime.now(timezone.utc)
    expires = created + timedelta(hours=expires_hours)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    conn.execute(
        "INSERT INTO requests (id, kind, status, created_at, requester, title, context,"
        " draft_text, target_channel, target_thread_ts, options_json, expires_at,"
        " requester_label, reply_to_ts, replaces, agent_where, email_cc, extras_json, dm_done, silent,"
        " followups_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid, kind, "noted" if silent else "pending", created.strftime(fmt), requester, title, context,
            draft_text, target_channel, target_thread_ts,
            json.dumps(options) if options else None, expires.strftime(fmt),
            requester_label or None, reply_to_ts or None, replaces or None, agent_where or None,
            email_cc or None, extras_json, 1 if silent else 0, 1 if silent else 0,
            json.dumps(followups, ensure_ascii=False) if followups else None,
        ),
    )
    return rid


def get(conn: sqlite3.Connection, rid: str) -> Optional[Dict[str, Any]]:
    return _row(conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())


REVISE_OPEN_HOURS = 24


def revise_open_since() -> str:
    """The oldest `decided_at` of a `revise` card that `list --status open` still shows."""
    t = datetime.now(timezone.utc) - timedelta(hours=REVISE_OPEN_HOURS)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


LINEAGE_TTL = 60.0       # seconds a lineage answer is kept
LINEAGE_FAIL_TTL = 20.0  # seconds a failed lookup is kept (the old rule runs meanwhile)
LINEAGE_TIMEOUT = 5      # seconds for `agent-bus lineage`
_lineage_cache: Dict[str, Any] = {}


LINEAGE_BUDGET = 3.0     # seconds in all for the lineage lookups of one list call


def lineage(sid: str, budget: Optional[Dict[str, Any]] = None) -> List[str]:
    """`agent-bus lineage <sid>`: the session id first, then every successor (handoff or fresh wake), oldest first.
    A failure, a timeout or a bad answer gives [sid] (today's same-session rule). Kept for LINEAGE_TTL seconds."""
    now = time.time()
    hit = _lineage_cache.get(sid)
    if hit and hit[0] > now:
        return list(hit[1])
    result, ttl = [sid], LINEAGE_FAIL_TTL
    timeout: float = LINEAGE_TIMEOUT
    if budget is not None:
        timeout = min(timeout, budget["until"] - time.monotonic())
        if budget.get("failed") or timeout <= 0.2:
            return [sid]  # one failure or the budget spent: today's rule for the rest of this call
    try:
        bus = os.environ.get("AGENT_BUS_BIN") or str(Path.home() / ".local/bin/agent-bus")
        out = subprocess.run([bus, "lineage", sid], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                             timeout=timeout)
        data = json.loads(out.stdout) if out.returncode == 0 else None
        if isinstance(data, list) and data and all(isinstance(x, str) for x in data):
            result, ttl = [sid] + [x for x in data if x != sid], LINEAGE_TTL
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if budget is not None and ttl == LINEAGE_FAIL_TTL:
        budget["failed"] = True
    _lineage_cache[sid] = (now + ttl, result)
    return list(result)


def _first_word(who: Optional[str]) -> str:
    parts = (who or "").split()
    return parts[0] if parts else ""


def answered_by_lineage(conn: Any, req: Dict[str, Any], budget: Optional[Dict[str, Any]] = None) -> bool:
    """True when a `revise` card was answered by a successor of its session (a handoff or a fresh wake has a new
    session id, so the same-session rule of `list_requests` never sees its card). A later card from any session of
    the lineage counts; for a follow-up (`via` = next) a later silent card counts too."""
    sid = _first_word(req.get("requester"))
    if not SESSION_ID_RE.match(sid) or not req.get("decided_at"):
        return False
    chain = lineage(sid, budget)
    follow = (req.get("result") or {}).get("via") == "next"
    for n in conn.execute("SELECT requester, silent FROM requests WHERE created_at>? AND id<>?"
                          " AND COALESCE(requester,'')<>''", (req["decided_at"], req["id"])).fetchall():
        who = _first_word(n["requester"])
        if who in chain[1:] and (follow or not n["silent"]):
            return True
        if who == sid and follow and n["silent"]:
            return True
    return False


def list_requests(
    conn: sqlite3.Connection, status: str = "all", since: Optional[str] = None
) -> List[Dict[str, Any]]:
    sql, args = "SELECT * FROM requests WHERE 1=1", []
    if status == "open":
        # A card in `revise` (the owner pressed «Pedir cambios», or started a follow-up of a report) is open too:
        # its session owes a new card. It stops counting when the same session sends a later card (the new
        # report after a follow-up; a card with --replaces already changes the status; a silent notice is a
        # status line of the work in progress, not the answer), or REVISE_OPEN_HOURS
        # after the owner's answer. Before 2026-11-23 only the hours counted: a session that did its follow-up
        # and sent its report kept an open card for 24 h, so its laptop tab never closed and held a task slot.
        who = "CASE WHEN instr(%s,' ')>0 THEN substr(%s,1,instr(%s,' ')-1) ELSE %s END"
        sql += (" AND (status IN ('pending','shown','sending') OR (status='revise' AND decided_at>=?"
                " AND NOT EXISTS (SELECT 1 FROM requests n WHERE n.created_at>requests.decided_at"
                " AND n.silent=0 AND COALESCE(n.requester,'')<>'' AND " + who % (("n.requester",) * 4) + "="
                + who % (("requests.requester",) * 4) + ")))")
        args.append(revise_open_since())
    elif status != "all":
        sql += " AND status=?"
        args.append(status)
    if since:
        sql += " AND created_at>=?"
        args.append(since)
    sql += " ORDER BY created_at, rowid"
    rows = [_row(r) for r in conn.execute(sql, args).fetchall()]
    if status == "open":
        budget = {"until": time.monotonic() + LINEAGE_BUDGET, "failed": False}
        rows = [r for r in rows if not (r and r["status"] == "revise" and answered_by_lineage(conn, r, budget))]
    return rows  # type: ignore[return-value]


def transition(
    conn: sqlite3.Connection, rid: str, from_status: str, to_status: str, **fields: Any
) -> bool:
    """Move a request in one atomic step. Return False when it is not in from_status."""
    allowed = {
        "final_text", "result_json", "decided_at", "dm_channel", "dm_ts", "dm_done", "reopened_at",
        "and_continue",
    }
    assert set(fields) <= allowed
    sets = ["status=?"] + [f"{k}=?" for k in fields]
    cur = conn.execute(
        f"UPDATE requests SET {', '.join(sets)} WHERE id=? AND status=?",
        [to_status, *fields.values(), rid, from_status],
    )
    return cur.rowcount == 1


def set_compact(conn: Any, rid: str, level: int) -> None:
    """Store the body step that Slack accepted for this card (see blocks.BODY_LIMITS)."""
    conn.execute("UPDATE requests SET compact=? WHERE id=?", (int(level), rid))


def set_original(conn: Any, rid: str, **fields: Optional[str]) -> None:
    """Store the message a reply answers. Only the orig_* columns are allowed."""
    keys = [k for k in fields if k in ("orig_name", "orig_time", "orig_text",
                                       "orig_permalink", "orig_channel_name", "email_subject")]
    if not keys:
        return
    conn.execute(f"UPDATE requests SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                 [fields[k] for k in keys] + [rid])


def set_result(conn: Any, rid: str, result: Dict[str, Any]) -> bool:
    """Save the partial answers of a questions request that is still open."""
    cur = conn.execute(
        "UPDATE requests SET result_json=? WHERE id=? AND status='shown' AND kind='questions'",
        (json.dumps(result, ensure_ascii=False), rid))
    return cur.rowcount == 1


def add_owner_note(conn: Any, rid: str, text: str) -> None:
    """Append a late note of the owner to the result of a card in any status. Read the row again just before the
    write, so a result that changed meanwhile is kept."""
    row = conn.execute("SELECT result_json FROM requests WHERE id=?", (rid,)).fetchone()
    try:
        res = json.loads(row["result_json"]) if row and row["result_json"] else {}
    except ValueError:
        res = {}
    res.setdefault("owner_notes", []).append({"at": now_iso(), "text": text})
    conn.execute("UPDATE requests SET result_json=? WHERE id=?", (json.dumps(res, ensure_ascii=False), rid))


def set_final_text(conn: sqlite3.Connection, rid: str, text: str) -> bool:
    cur = conn.execute(
        "UPDATE requests SET final_text=? WHERE id=? AND status='shown' AND kind IN ('reply','email')",
        (text, rid),
    )
    return cur.rowcount == 1


def expire_due(conn: sqlite3.Connection, now: Optional[str] = None) -> List[str]:
    """Mark open requests past expires_at as expired. Return their ids."""
    now = now or now_iso()
    rows = conn.execute(
        "SELECT id FROM requests WHERE status IN ('pending','shown') AND expires_at<=?",
        (now,),
    ).fetchall()
    ids = []
    for r in rows:
        if transition(conn, r["id"], "pending", "expired", decided_at=now) or transition(
            conn, r["id"], "shown", "expired", decided_at=now
        ):
            ids.append(r["id"])
    return ids


def report_fold_hours() -> float:
    """Hours before a posted report folds to one line (POLAR_REPORT_FOLD_HOURS)."""
    try:
        return float(os.environ.get("POLAR_REPORT_FOLD_HOURS", str(REPORT_FOLD_HOURS)))
    except ValueError:
        return float(REPORT_FOLD_HOURS)


def fold_reports(conn: Any, now: Optional[str] = None) -> List[str]:
    """Fold posted reports (noted) that are old enough to «filed». Return their ids."""
    now = now or now_iso()
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cutoff = (datetime.strptime(now, fmt) - timedelta(hours=report_fold_hours())).strftime(fmt)
    rows = conn.execute(
        "SELECT id FROM requests WHERE status='noted' AND kind='report' AND created_at<=?",
        (cutoff,)).fetchall()
    return [r["id"] for r in rows
            if transition(conn, r["id"], "noted", "filed", decided_at=now, dm_done=0)]


def skip_after_min() -> float:
    """Minutes a waiting card must age before the «Pendientes» list shows it (POLAR_SKIP_AFTER_MIN)."""
    try:
        return float(os.environ.get("POLAR_SKIP_AFTER_MIN", "30"))
    except ValueError:
        return 30.0


def passed_over(conn: Any, now: Optional[str] = None) -> List[Dict[str, Any]]:
    """Shown cards that wait while a newer card was decided. Polar lists them. It never skips them.

    The age counts from created_at, or from reopened_at after a reopen."""
    now = now or now_iso()
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    cutoff = (datetime.strptime(now, fmt) - timedelta(minutes=skip_after_min())).strftime(fmt)
    rows = conn.execute(
        "SELECT * FROM requests r WHERE r.status='shown'"
        " AND r.kind IN ('reply','decision','email','questions')"
        " AND COALESCE(r.reopened_at, r.created_at)<=?"
        " AND EXISTS (SELECT 1 FROM requests n WHERE n.created_at>r.created_at"
        " AND n.status IN ('sent','chosen','discarded','answered'))"
        " ORDER BY r.created_at, r.rowid",
        (cutoff,),
    ).fetchall()
    return [_row(r) for r in rows]  # type: ignore[misc]


def reopen(conn: Any, rid: str, now: Optional[str] = None) -> bool:
    """Put a skipped request back to shown. Return False when it is not skipped."""
    return transition(conn, rid, "skipped", "shown", decided_at=None, result_json=None,
                      dm_done=0, reopened_at=now or now_iso())


REPLACEABLE = ("shown", "pending", "revise", "noted")


def mark_replaced(conn: Any, rid: str) -> bool:
    """Mark an old request as replaced by a new card. Return False when it is not replaceable."""
    for st in REPLACEABLE:
        if transition(conn, rid, st, "replaced", decided_at=now_iso(), dm_done=0):
            return True
    return False


def withdraw(conn: Any, rid: str) -> bool:
    """The requester takes its own card back. Return False when it is not shown, pending or revise."""
    for st in REPLACEABLE:
        if transition(conn, rid, st, "discarded", decided_at=now_iso(), dm_done=0,
                      result_json=json.dumps({"by": "requester"})):
            return True
    return False
