"""`polar demo`: the web UI on invented sample cards. No Slack token, no config, and nothing leaves this machine.

Each run makes a fresh temporary data dir (POLAR_DATA_DIR is ignored) and seeds it through the real `db` functions.
The actions run through the real `Polar` core with a fake Slack client, so Send only changes the temporary database."""
from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polar import db
from polar.core import Polar
from polar.web.strings import set_lang

OWNER = "demo-owner"
DAY = 24.0


class DemoSlack:
    """A Slack client that answers every call and sends nothing."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._n = 0

    def conversations_open(self, **kw: Any) -> Dict[str, Any]:
        return {"channel": {"id": "demo-dm"}}

    def chat_postMessage(self, **kw: Any) -> Dict[str, Any]:
        self._n += 1
        self.calls.append(("chat_postMessage", kw))
        return {"ok": True, "channel": kw.get("channel") or "demo-dm", "ts": "1700000000.%06d" % self._n}

    def chat_getPermalink(self, **kw: Any) -> Dict[str, Any]:
        return {"permalink": "https://example.com/demo/message"}

    def __getattr__(self, name: str) -> Any:
        def call(**kw: Any) -> Dict[str, Any]:
            self.calls.append((name, kw))
            return {"ok": True}
        return call


class DemoPolar(Polar):
    """The real core. Only the Gmail send is replaced: the email card moves to «sent» in the database."""

    def _send_email(self, req: Dict[str, Any], text: str, src: Optional[str] = None) -> None:
        db.transition(self.conn, req["id"], "sending", "sent", final_text=text,
                      result_json=json.dumps({"demo": True, "src": src}), decided_at=db.now_iso())


def chart_png(width: int = 480, height: int = 220) -> bytes:
    """A small bar chart (before and after) drawn in code, as PNG bytes."""
    bg, axis, old, new = (20, 26, 29), (47, 57, 62), (141, 152, 158), (127, 209, 185)
    bars = [(60, 150, old), (150, 150, old), (240, 50, new), (330, 48, new)]  # (x, bar height, colour)
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            px = bg
            if y == height - 30 or (x == 40 and 20 <= y <= height - 30):
                px = axis
            for bx, bh, col in bars:
                if bx <= x < bx + 60 and height - 30 - bh <= y < height - 30:
                    px = col
            row += bytes(px)
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


def _sid(n: int) -> str:
    """An invented session id (a UUID): the card has a reader, so «Request changes» starts no task."""
    return "%08x-0000-4000-8000-%012x" % (0xDE30 + n, n)


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(conn: Any, rid: str, hours_ago: float, decided_hours_ago: Optional[float] = None) -> None:
    """Move a sample card back in time. The created time and the decided time only; the status stays."""
    conn.execute("UPDATE requests SET created_at=? WHERE id=?", (_iso(hours_ago), rid))
    if decided_hours_ago is not None:
        conn.execute("UPDATE requests SET decided_at=? WHERE id=?", (_iso(decided_hours_ago), rid))


def _show(conn: Any, rid: str, report: bool = False) -> None:
    db.transition(conn, rid, "pending", "noted" if report else "shown")


def _close(conn: Any, rid: str, to: str, result: Dict[str, Any], final_text: Optional[str] = None,
           report: bool = False) -> None:
    fields: Dict[str, Any] = {"result_json": json.dumps(result), "decided_at": db.now_iso()}
    if final_text:
        fields["final_text"] = final_text
    db.transition(conn, rid, "noted" if report else "shown", to, **fields)


DECISION_BODY = """The release branch passed all checks. Two items are still open:

- The mobile banner fix is merged but not yet seen on a phone.
- The **migration** adds one column and runs in under a second on a copy of the data.

| Check | Result |
|---|---|
| Unit tests | 1,204 passed |
| Load test | p95 180 ms |
"""

REPORT_BODY = """## What changed

Search now reads from a prepared index instead of scanning the table. The median query fell from **420 ms** to
**130 ms** on the staging data.

| Query | Before | After |
|---|---|---|
| Single word | 380 ms | 110 ms |
| Two words | 450 ms | 140 ms |
| With filters | 610 ms | 190 ms |

## What I checked

1. The results are the same for the 200 most common queries.
2. The index rebuilds in 40 seconds after a deploy.
3. Memory use grew by 60 MB.

The chart below compares the two versions.
"""


def seed(conn: Any, data_dir: Path) -> Dict[str, str]:
    """Write the sample cards. Return {name: card id}. Only invented names and generic software-team content."""
    files = data_dir / "files"
    files.mkdir(parents=True, exist_ok=True)
    chart, notes = files / "search-chart.png", files / "db-notes.txt"
    chart.write_bytes(chart_png())
    notes.write_text("Postgres: the team knows it; analytics queries need tuning.\n"
                     "ClickHouse: fast for reports; one more system to run.\n", encoding="utf-8")
    ids: Dict[str, str] = {}
    keep = 30 * DAY

    def make(name: str, kind: str, title: str, who: int, label: str, **kw: Any) -> str:
        rid = db.create_request(conn, kind, title, requester=_sid(who) + " " + label.lower().replace(" ", "-"),
                                requester_label=label, expires_hours=keep, **kw)
        ids[name] = rid
        return rid

    # -- waiting -------------------------------------------------------------------------------------------------
    rid = make("release", "decision", "Ship release 2.4 to production today?", 1, "Release agent",
               context="All checks are green. The last deploy window today closes at 17:00.",
               options=[{"key": "ship", "label": "Ship now (recommended)"}, {"key": "monday", "label": "Wait for Monday"},
                        {"key": "api", "label": "Ship the API only"}],
               extra={"body": DECISION_BODY, "links": ["Release notes=https://example.com/releases/2.4",
                                                       "Checks=https://example.com/ci/run/812"]})
    _show(conn, rid)
    _age(conn, rid, 9)
    rid = make("database", "decision", "Which database for the analytics service?", 2, "Data agent",
               context="The analytics service needs a store for 50 million events a month.",
               options=[{"key": "pg", "label": "Postgres, the one we run today"},
                        {"key": "ch", "label": "ClickHouse, built for reports"}],
               extra={"body": "Both handle the load in the test. The notes file lists the trade-offs.",
                      "files": [str(notes)]})
    _show(conn, rid)
    _age(conn, rid, 6)
    rid = make("questions", "questions", "Three questions before I write the onboarding guide", 3, "Docs agent",
               context="I will write the guide as soon as I have these answers.",
               options=[{"key": "audience", "text": "Who is the guide for?", "recommended": "new",
                         "options": [{"key": "new", "label": "New engineers"},
                                     {"key": "all", "label": "Everyone on the team"}]},
                        {"key": "format", "text": "Which format?",
                         "options": [{"key": "page", "label": "One long page"},
                                     {"key": "steps", "label": "Short step pages"}]},
                        {"key": "deadline", "text": "When do you need it?", "options": []}])
    _show(conn, rid)
    _age(conn, rid, 5)
    rid = make("reply", "reply", "Reply to Dana about the login bug", 4, "Support agent",
               context="Dana reported that login fails on Safari. The fix is in review.",
               draft_text="Thanks, Dana. We found it: Safari drops the session cookie after the redirect. The fix is "
                          "in review and should ship tomorrow morning. I will post here when it is live.",
               target_channel="demo-team-web", target_thread_ts="1700000000.000100")
    db.set_original(conn, rid, orig_name="Dana", orig_time="09:12", orig_channel_name="team-web",
                    orig_text="Login fails on Safari since this morning. Chrome works. Is anyone on it?",
                    orig_permalink="https://example.com/demo/message")
    _show(conn, rid)
    _age(conn, rid, 1.5)
    rid = make("email", "email", "Answer Omar Ruiz about invoice 1043", 5, "Billing agent",
               context="Omar asks why invoice 1043 shows two lines for the same month.",
               draft_text="Hello Omar,\n\nThe second line is the extra seat you added on the 12th. It is billed "
                          "from that day to the end of the month. I attach nothing new; the invoice is correct.\n\n"
                          "Best regards",
               target_thread_ts="demo-thread-1043", email_cc="billing@example.com")
    db.set_original(conn, rid, orig_name="Omar Ruiz", orig_time="Yesterday 16:40",
                    email_subject="Question about invoice 1043",
                    orig_text="Hi, invoice 1043 has two lines for March. Can you tell me why?")
    _show(conn, rid)
    _age(conn, rid, 0.7)
    rid = make("search", "report", "Search is 3x faster", 6, "Search agent", draft_text=REPORT_BODY,
               context="The new search index is live on staging.",
               report={"links": ["Pull request=https://example.com/pr/311"], "files": [str(chart)],
                       "next": ["Turn the index on in production", "Add the index to the nightly backup",
                                "Write a short note for the team"]})
    _show(conn, rid, report=True)
    _age(conn, rid, 2)
    rid = make("backup", "report", "Nightly backup check passed", 7, "Ops agent",
               draft_text="All 14 backups of the week restored without errors. The largest took 6 minutes.",
               context="The weekly restore test ran at 03:00.")
    _show(conn, rid, report=True)
    _age(conn, rid, 7)

    # -- decided (history) ---------------------------------------------------------------------------------------
    rid = make("flag", "decision", "Rename the --out flag to --output?", 8, "CLI agent",
               context="Two scripts in the repo use --out.",
               options=[{"key": "yes", "label": "Rename, keep --out as an alias"}, {"key": "no", "label": "Keep --out"}])
    _show(conn, rid)
    _close(conn, rid, "chosen", {"option": "yes"})
    _age(conn, rid, 28, 27)
    rid = make("ratelimit", "reply", "Reply to Lee about the API rate limit", 4, "Support agent",
               draft_text="Hi Lee, the limit is 600 requests a minute per key. I raised yours to 1,200 for the "
                          "import this week.", target_channel="demo-team-api", target_thread_ts="1700000000.000200")
    db.set_original(conn, rid, orig_name="Lee", orig_time="11:05", orig_channel_name="team-api",
                    orig_text="We hit 429 errors during the import. What is the limit?")
    _show(conn, rid)
    _close(conn, rid, "sent", {"channel": "demo-team-api"},
           final_text="Hi Lee, the limit is 600 requests a minute per key. I raised yours to 1,200 for the import "
                      "this week.")
    _age(conn, rid, 30, 29)
    rid = make("testplan", "questions", "Questions about the test plan", 3, "QA agent",
               options=[{"key": "browsers", "text": "Which browsers?",
                         "options": [{"key": "two", "label": "Chrome and Safari"},
                                     {"key": "all", "label": "All four"}]},
                        {"key": "devices", "text": "Test on real phones?", "options": []}])
    _show(conn, rid)
    _close(conn, rid, "answered", {"answers": {"browsers": {"option": "two", "text": "Chrome and Safari"},
                                               "devices": {"option": None, "text": "Yes, one iPhone and one Android"}},
                                   "note": "Skip the tablets for now."})
    _age(conn, rid, 50, 49)
    rid = make("sam", "reply", "Reply to Sam about the holiday schedule", 4, "Support agent",
               draft_text="Hi Sam, the office is closed on the 24th and the 31st.",
               target_channel="demo-team-general", target_thread_ts="1700000000.000300")
    _show(conn, rid)
    _close(conn, rid, "discarded", {})
    _age(conn, rid, 52, 51)
    rid = make("cache", "decision", "Move the cache to a managed service?", 2, "Infra agent",
               options=[{"key": "move", "label": "Move it"}, {"key": "stay", "label": "Keep our own"}])
    _show(conn, rid)
    _close(conn, rid, "revise", {"instructions": "Compare the cost first."})
    _age(conn, rid, 75, 74)
    rid = make("beta", "decision", "Pause the beta signups for the weekend?", 1, "Growth agent",
               options=[{"key": "pause", "label": "Pause"}, {"key": "open", "label": "Keep open"}])
    _show(conn, rid)
    db.transition(conn, rid, "shown", "expired", decided_at=db.now_iso())
    _age(conn, rid, 100, 76)
    rid = make("deps", "report", "Dependency update done", 8, "Maintenance agent",
               draft_text="Updated 23 packages. No breaking changes. All tests pass.")
    _show(conn, rid, report=True)
    _close(conn, rid, "chosen", {"option": "ok"}, report=True)
    _age(conn, rid, 98, 97)
    rid = make("budget", "report", "Error budget for November", 7, "Ops agent",
               draft_text="We used 40% of the error budget. Most of it came from one slow deploy on the 9th.",
               report={"next": ["Add a check that stops a slow deploy"]})
    _show(conn, rid, report=True)
    _close(conn, rid, "revise", {"instructions": "Add a check that stops a slow deploy", "via": "next",
                                 "indexes": [0], "steps": ["Add a check that stops a slow deploy"]}, report=True)
    _age(conn, rid, 120, 118)
    return ids


DEMO_ENV = {"POLAR_DELIVERY": "web", "POLAR_TASK_NOTICES": "off", "POLAR_WEB_ALLOW": "send",
            "POLAR_SLACK_REMINDERS": "off"}


def prepare(data_dir: Path) -> Tuple[DemoPolar, Dict[str, str]]:
    """Open a database in `data_dir`, seed it, and return the core and the card ids. It reads no environment."""
    conn = db.connect(Path(data_dir) / "polar.db")
    ids = seed(conn, Path(data_dir))
    slack = DemoSlack()
    return DemoPolar(conn, slack, slack, OWNER), ids


def run(port: int = 8792, lang: Optional[str] = None) -> int:
    """Start the demo UI and block until Ctrl-C. The temporary data dir is removed at the end."""
    from polar.web import server
    tmp = Path(tempfile.mkdtemp(prefix="polar-demo-"))
    os.environ["POLAR_DATA_DIR"] = str(tmp)  # anything that asks for the data dir gets the temporary one
    for k, v in DEMO_ENV.items():
        os.environ[k] = v
    if lang:
        set_lang(lang)
    try:
        polar, _ = prepare(tmp)
        try:
            ui = server.UIServer(port, polar, demo=True, data_dir=tmp)
        except OSError as exc:
            print("polar demo: port %d is not free (%s)" % (port, exc.strerror or type(exc).__name__))
            return 1
        print("Polar demo: %s  (sample data in a temporary folder; Ctrl-C stops it)" % ui.url, flush=True)
        try:
            ui.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            ui.server_close()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
