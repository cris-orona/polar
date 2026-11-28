"""The `polar` command. It only reads and writes SQLite, except `serve` and `doctor`."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import re
import sys
from typing import Any, Dict, List, Optional

from polar import db


def _out(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _parse_option(raw: str) -> Dict[str, str]:
    key, sep, label = raw.partition(":")
    if not sep or not key.strip() or not label.strip():
        raise ValueError(f"bad --option {raw!r}: use key:Label")
    return {"key": key.strip(), "label": label.strip()}


def _public(req: Dict[str, Any]) -> Dict[str, Any]:
    res = req.get("result") or {}
    return {
        "id": req["id"], "kind": req["kind"], "status": req["status"],
        "title": req["title"], "requester": req["requester"],
        "created_at": req["created_at"], "decided_at": req["decided_at"],
        "expires_at": req["expires_at"],
        "chosen_option": res.get("option"),
        "and_continue": int(req.get("and_continue") or 0),
        **({"instructions": res["instructions"]} if res.get("instructions") else {}),
        **({"via": res["via"]} if res.get("via") else {}),
        **({"respond_summary": res.get("respond_summary"), "respond_note": res["respond_note"]}
           if res.get("respond_note") else {}),
        **({"indexes": res["indexes"], "steps": res["steps"]} if res.get("steps") else {}),
        **({"replaces": req["replaces"]} if req.get("replaces") else {}),
        **({"silent": True} if req.get("silent") else {}),
        **({"message_id": res["message_id"], "thread_id": res.get("thread_id")} if res.get("message_id") else {}),
        **({"questions": [{"key": q["key"], "text": q["text"]} for q in req["options"]],
            "answers": res.get("answers", {}), "note": res.get("note", "")}
           if req["kind"] == "questions" else {}),
        "final_text": req["final_text"],
        "permalink": res.get("permalink"),
        "error": res.get("error"),
    }


SNAKE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+){1,}\b")
URL_OR_CHANNEL = re.compile(r"https?://\S+|<#[A-Z0-9]+(?:\|[^>]*)?>|#[\w-]+")


def check_context(context: str) -> Optional[str]:
    """Return a bilingual error text, or None when the context is fine."""
    ctx = (context or "").strip()
    if len(ctx) > 280:
        why = "es más larga de 280 caracteres / is longer than 280 characters"
    elif len(ctx.splitlines()) > 2:
        why = "tiene más de 2 líneas / has more than 2 lines"
    elif SNAKE.search(URL_OR_CHANNEL.sub(" ", ctx)):
        why = "tiene un código tipo snake_case / has a snake_case code"
    else:
        return None
    return (f"--context rechazado: {why}. Escribe en español, una línea simple, sin códigos. "
            "/ Write Spanish, one plain line, no codes. Use --force-context only in rare cases.")


CONTEXT_HELP = "Por qué lo pides: español, una línea simple, sin códigos del sistema (max 280 caracteres)."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polar")
    sub = p.add_subparsers(dest="cmd", required=True)

    ask = sub.add_parser("ask", help="queue a request for the owner")
    ask_sub = ask.add_subparsers(dest="kind", required=True)
    for kind in ("reply", "decision", "questions", "agent", "email"):
        a = ask_sub.add_parser(kind)
        a.add_argument("--title", required=True)
        a.add_argument("--context", default="", help=CONTEXT_HELP)
        a.add_argument("--force-context", action="store_true",
                       help="skip the context checks (rare cases)")
        a.add_argument("--replaces", metavar="ID",
                       help="id of your earlier card that this card replaces (after «Pedir cambios»); "
                            "the old card closes as «Reemplazada»")
        a.add_argument("--requester", default="")
        a.add_argument("--requester-label", default="",
                       help="session topic shown as «Sesión: …» on the card")
        a.add_argument("--expires-hours", type=float, default=None,
                       help="hours until it expires (24; questions 168)")
        if kind == "reply":
            a.add_argument("--channel", required=True)
            a.add_argument("--thread")
            a.add_argument("--reply-to", help="ts of the exact message you answer (not a thread root)")
            g = a.add_mutually_exclusive_group(required=True)
            g.add_argument("--text")
            g.add_argument("--text-file")
        elif kind == "email":
            a.add_argument("--thread", required=True, help="Gmail thread id")
            a.add_argument("--cc", default="", help="Cc addresses, comma separated")
            g = a.add_mutually_exclusive_group(required=True)
            g.add_argument("--text")
            g.add_argument("--text-file")
        elif kind == "questions":
            a.add_argument("--question-file", required=True,
                           help="JSON list of 1-5 questions, or - for stdin")
        elif kind == "agent":
            a.add_argument("--brief-file", required=True,
                           help="the task for the new session: what to do, Slack permalink, sender")
            a.add_argument("--channel", help="channel of the source message")
            a.add_argument("--reply-to", help="ts of the source message (the card quotes it)")
            a.add_argument("--where", default="server", choices=["server", "laptop"])
        else:
            a.add_argument("--option", action="append", default=[], required=True)
            if kind == "decision":
                a.add_argument("--canary", action="store_true",
                               help="hourly test card of harness-canary: hidden from Mando, no DM")

        if kind in ("decision", "questions"):
            a.add_argument("--text-file", help="Markdown body shown as «📄 Qué es» (what the proposal is); - = stdin")
            a.add_argument("--link", action="append", default=[], metavar="Label=https://...",
                           help="URL shown under the body; repeat, max 5, http/https only")
            a.add_argument("--file", action="append", default=[],
                           help="file uploaded in the card thread; repeat")

    rp = sub.add_parser("report", help="send an FYI card: what a session did")
    rp.add_argument("--title", required=True, help="short, Spanish")
    rp.add_argument("--text-file", required=True, help="Markdown file, or - for stdin")
    rp.add_argument("--channel", help="channel of the thread the report answers (with --thread)")
    rp.add_argument("--thread", help="ts of the thread root: the report is posted inside that thread")
    rp.add_argument("--link", action="append", default=[], metavar="Label=https://...",
                    help="URL button (max 5, http/https)")
    rp.add_argument("--file", action="append", default=[], help="file to attach in the card thread")
    rp.add_argument("--next", action="append", default=[], metavar="TEXT",
                    help="a follow-up you suggest; the owner starts it with one tap, or checks several (max 3, 70 chars each)")
    rp.add_argument("--revisar", action="append", default=[], metavar="SPEC|what",
                    help='schedule a follow-up: the bus hands "what" back to this session when SPEC is met, and the '
                         'card shows «🗓 Revisaré …» with «Cancelar». Repeat, max 3. SPEC: "30-nov 10:00", '
                         '"mañana 9:00", "+3d 9:00", "lunes 9:00", "+2h", "prod:<repo>#<n>", "ci:<repo>#<n>" '
                         '(in the time zone of the bus). Example: --revisar "30-nov 10:00|Mide Polar contra la línea base". '
                         'Needs the session id in --requester. A bad SPEC refuses the whole command')
    rp.add_argument("--requester", default="")
    rp.add_argument("--requester-label", default="", help="session topic shown as «Sesión: …»")
    rp.add_argument("--resume", metavar="SESSION_ID", help="adds «Reanudar»: claude --resume <id> on the task host")
    rp.add_argument("--expires-hours", type=float, default=None, help="hours until it expires (72)")
    rp.add_argument("--silent", action="store_true",
                    help="store the report with no Slack DM: for a notice that needs no action from the owner. "
                         "It shows as a posted report in Mando and on the web page. Not with --file (no thread)")

    rp.add_argument("--card", action="store_true",
                    help="keep the report as a card even when it has no --next, --revisar or --resume")
    rd_ = sub.add_parser("revise-digest", help="the owner's corrections of the last days; --start hands them to a task")
    rd_.add_argument("--days", type=int, default=7)
    rd_.add_argument("--start", action="store_true", help="start one owner task that proposes rules from them")
    st = sub.add_parser("status")
    st.add_argument("id")

    ls = sub.add_parser("list")
    ls.add_argument("--status", default="all", choices=["pending", "open", "all", "shown",
                    "sent", "chosen", "discarded", "expired", "failed", "answered", "skipped",
                    "revise", "replaced", "noted", "filed"])
    ls.add_argument("--since")

    dc = sub.add_parser("discard", help="withdraw your own card (shown, pending or revise)")
    dc.add_argument("id")

    th = sub.add_parser("thread", help="messages of one Slack thread (JSON lines, same fields as inbox)")
    th.add_argument("channel")
    th.add_argument("thread_ts")

    fl = sub.add_parser("files", help="paths of the files of one owner message (JSON list); a file id downloads it")
    fl.add_argument("key", help="the message ts that the task text names, or a Slack file id (F…)")

    rp_ = sub.add_parser("replay", help="read one owner DM again (a message that Polar missed); needs the bot token")
    rp_.add_argument("ts", help="the ts of the message in the owner's DM with Polar")

    ml = sub.add_parser("mail", help="Gmail: auth, search, thread, draft, inbox")
    ml_sub = ml.add_subparsers(dest="mail_cmd", required=True)
    ml_sub.add_parser("auth", help="connect Gmail (OAuth, no browser on this host)")
    ms = ml_sub.add_parser("search")
    ms.add_argument("query")
    ms.add_argument("--max", type=int, default=20)
    mt = ml_sub.add_parser("thread")
    mt.add_argument("thread_id")
    mi = ml_sub.add_parser("inbox", help="new mail a person wrote to the owner (JSON lines, same fields as inbox)")
    mi.add_argument("--since", type=float, required=True, help="epoch seconds")
    md = ml_sub.add_parser("draft", help="create a Gmail draft reply; prints the draft id")
    md.add_argument("--thread", required=True)
    md.add_argument("--cc", default="")
    g = md.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--text-file")

    ib = sub.add_parser("inbox", help="new DMs and mentions for the owner (JSON lines)")
    ib.add_argument("--since-ts", type=float, default=0.0)

    tk = sub.add_parser("tasks", help="owner tasks started from Slack DMs")
    tk.add_argument("--json", action="store_true", help="print one JSON list")
    tk.add_argument("--status", default="all")

    sr = sub.add_parser("steers", help="steering messages for the laptop (undelivered)")
    sr.add_argument("--json", action="store_true")

    sd = sub.add_parser("steer-done", help="the laptop delivered steering message <id>")
    sd.add_argument("id")

    tg = sub.add_parser("triage", help="tasks that wait for a session match (JSON list)")
    tg.add_argument("--json", action="store_true")

    ts_ = sub.add_parser("triage-set", help="answer the triage of task <id>")
    ts_.add_argument("id")
    g = ts_.add_mutually_exclusive_group(required=True)
    g.add_argument("--match", metavar="SESSION_ID")
    g.add_argument("--none", action="store_true", help="no session fits: open a new one")
    ts_.add_argument("--label", default="")
    ts_.add_argument("--confidence", choices=["high", "medium"],
                     help="high or medium: Polar routes at once, without asking the owner (medium says «coincidencia probable»)")
    ts_.add_argument("--reason", default="")
    ts_.add_argument("--where", choices=["laptop", "server"], default="server",
                     help="where a new session runs: server (default, at once) or laptop (with grace; browser or local Supabase)")
    ts_.add_argument("--area", choices=["work", "personal"], default=None,
                     help="the folder of a new session: work (~/work) or personal (~/personal: not team "
                          "work, so the team memory does not read it). Not given: work for --none, the area "
                          "of the matched session for --match")

    rt = sub.add_parser("routes", help="routed tasks that the laptop must deliver (JSON list)")
    rt.add_argument("--json", action="store_true")

    rd = sub.add_parser("route-done", help="the laptop delivered routed task <id>")
    rd.add_argument("id")

    rf = sub.add_parser("route-failed", help="the laptop cannot deliver routed task <id>: new session")
    rf.add_argument("id")

    qu = sub.add_parser("queue", help="queued tasks that wait for a laptop claim (JSON list)")
    qu.add_argument("--json", action="store_true")

    cl = sub.add_parser("claim", help="the laptop takes queued task <id>; exit 1 when it is not queued")
    cl.add_argument("id")
    cl.add_argument("--host", default="laptop", choices=["laptop"])
    cl.add_argument("--tab", required=True)
    cl.add_argument("--session", required=True)
    cl.add_argument("--pane", default="")

    ho = sub.add_parser("handoff", help="hand one step to a new laptop session (browser, laptop MCP)")
    ho.add_argument("--title", required=True)
    ho.add_argument("--text-file", required=True, help="the step; - reads stdin")
    ho.add_argument("--requester", required=True, help='"<session id> <tmux name>" of the asking session')

    td = sub.add_parser("task-done", help="the laptop tab of task <id> closed")
    td.add_argument("id")

    pk = sub.add_parser("picks", help="owner lists that wait for a plan (JSON list)")
    pk.add_argument("--json", action="store_true")

    ps = sub.add_parser("plan-set", help="store the plan of pick <id> (JSON from --file or stdin)")
    ps.add_argument("id")
    ps.add_argument("--file", default="-")

    fb = sub.add_parser("follow-bus", help="make task statuses follow the agent-bus state (one pass, no 60 s gate)")
    fb.add_argument("--dry-run", action="store_true", help="print the planned changes as JSON; change nothing")
    sub.add_parser("expire")
    sub.add_parser("serve")
    demo = sub.add_parser("demo", help="the web UI on sample data: no Slack, nothing is sent")
    demo.add_argument("--port", type=int, default=8792)
    demo.add_argument("--lang", choices=["en", "es"], default=None)
    sub.add_parser("ui-url", help="print the login link of the web UI (POLAR_UI_PORT); it is a credential")
    sub.add_parser("doctor")
    return p


def _replace_old(args: argparse.Namespace, new_id: str) -> None:
    """Close the old card named by --replaces. Do nothing when it is not replaceable."""
    if args.replaces:
        db.mark_replaced(db.connect(), args.replaces)


def _read_text(args: argparse.Namespace) -> str:
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as fh:
            return fh.read().strip()
    return args.text


def _wait_for_redirect() -> str:
    """The redirect URL: from a listener on 127.0.0.1:8765 (reach it with ssh -L 8765:127.0.0.1:8765),
    or from a line pasted on stdin, whichever comes first."""
    import http.server, select, threading
    got: Dict[str, str] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if "code=" in self.path:
                got["url"] = "http://localhost:8765" + self.path
            body = "Listo. Polar ya tiene acceso. Puedes cerrar esta pestaña.".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = None
    try:
        srv = http.server.HTTPServer(("127.0.0.1", 8765), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError:
        srv = None
    try:
        while "url" not in got:
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if r:
                line = sys.stdin.readline()
                if line.strip():
                    return line
        return got["url"]
    finally:
        if srv:
            srv.shutdown()


def cmd_mail(args: argparse.Namespace) -> int:
    from polar import gmail
    if args.mail_cmd == "auth":
        flow, url = gmail.auth_url()
        print("1. Abre esta URL en el navegador de tu laptop y da permiso:\n\n%s\n" % url, file=sys.stderr)
        print("2. Si abriste esto con ssh -L 8765:127.0.0.1:8765, el navegador dice «Listo» y termina solo.\n"
              "   Si no, copia la URL completa de la barra de direcciones (empieza con %s?code=…) y pégala aquí."
              % gmail.AUTH_REDIRECT.rstrip("/"), file=sys.stderr)
        sys.stderr.write("URL o código: ")
        sys.stderr.flush()
        try:
            gmail.finish_auth(flow, _wait_for_redirect())
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            print("Google rechazó el código (%s). Corre polar mail auth otra vez." % gmail.error_code(exc),
                  file=sys.stderr)
            return 1
        _out({"connected": True})
        return 0
    try:
        svc = gmail.service()
        if args.mail_cmd == "search":
            for row in gmail.search(svc, args.query, args.max):
                _out(row)
        elif args.mail_cmd == "thread":
            _out(gmail.thread(svc, args.thread_id))
        elif args.mail_cmd == "inbox":
            rows, more = gmail.inbox(svc, args.since)
            for row in rows:
                _out(row)
            if more is not None:  # not every new message was opened: the caller asks again from this time
                _out({"source": "mail-more", "ts": "%.3f" % more})
        elif args.mail_cmd == "draft":
            _out({"draft_id": gmail.create_draft(svc, args.thread, _read_text(args), args.cc or None)})
    except gmail.NotConnected:
        print(gmail.NOT_CONNECTED, file=sys.stderr)
        return 2
    except ValueError as exc:
        _out({"error": str(exc)})
        return 2
    except Exception as exc:  # print the code only, never request data
        _out({"error": gmail.error_code(exc)})
        return 1
    return 0


def _extra(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """Body, links and files of a decision or questions card. Fail before the database opens."""
    import os
    body = ""
    tf = getattr(args, "text_file", None)
    if tf and args.kind in ("decision", "questions"):
        if tf == "-":
            body = sys.stdin.read().strip()
        else:
            with open(tf, encoding="utf-8") as fh:
                body = fh.read().strip()
    files = []
    for f in getattr(args, "file", None) or []:
        path = os.path.abspath(os.path.expanduser(f))
        if not os.path.isfile(path):
            raise ValueError("file not found: %s" % f)
        files.append(keep_file(path))
    links = getattr(args, "link", None) or []
    db.validate_report(links, files, None)
    if not (body or links or files):
        return None
    return {"body": body, "links": links, "files": files}


UPLOAD_KEEP_DAYS = 30


def prune_uploads(base: str, now: Optional[float] = None) -> int:
    """Remove the copies that keep_file made more than UPLOAD_KEEP_DAYS ago (folders named `<epoch>-<random>`), so the
    folder does not grow without end. A card that old has expired. Anything else in the folder stays."""
    limit = (time.time() if now is None else now) - UPLOAD_KEEP_DAYS * 86400
    gone = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        m = re.match(r"(\d{9,11})-", name)
        full = os.path.join(base, name)
        if m and int(m.group(1)) < limit and os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
            gone += 1
    return gone


def keep_file(path: str) -> str:
    """Copy an attachment into the data folder and return the copy. The Polar service runs under launchd, and macOS
    does not let it read ~/Downloads, ~/Desktop or ~/Documents. This command runs in the session, which can."""
    base = os.path.realpath(str(db.data_dir() / "uploads"))
    if os.path.realpath(path).startswith(base + os.sep):
        return path
    os.makedirs(base, exist_ok=True)
    prune_uploads(base)
    folder = tempfile.mkdtemp(prefix="%d-" % time.time(), dir=base)
    os.chmod(folder, 0o755)
    return shutil.copy(path, os.path.join(folder, os.path.basename(path)))


NO_SESSION = ('polar: this card names no session, so the owner\'s answer cannot come back. '
              'Pass --requester "<claude session id> <herdr pane or tmux name>", '
              'or a system name (for example --requester watchdog).')


def _parent_pid(pid: int) -> Optional[int]:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=3).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_session_id() -> str:
    """The Claude session id: env first, then the session file of a parent process."""
    sid = os.environ.get("POLAR_SESSION_ID", "").strip()
    if sid:
        return sid
    pid: Optional[int] = os.getppid()
    for _ in range(8):
        if not pid or pid <= 1:
            return ""
        path = os.path.expanduser("~/.claude/sessions/%d.json" % pid)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return str(json.load(fh).get("sessionId") or "").strip()
            except (OSError, ValueError, AttributeError):
                return ""
        pid = _parent_pid(pid)
    return ""


def find_place() -> str:
    """The Herdr pane id, else the tmux session name, else empty."""
    pane = os.environ.get("HERDR_PANE_ID", "").strip()
    if pane:
        return pane
    if not os.environ.get("TMUX"):
        return ""
    exe = shutil.which("tmux") or ("/opt/homebrew/bin/tmux" if os.path.exists("/opt/homebrew/bin/tmux") else "")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "display-message", "-p", "#S"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def resolve_requester(given: str) -> str:
    """Keep a named requester. Else fill "<session id> <place>"; empty when no session is found."""
    if given.strip() and not given.strip().startswith("unknown-session"):
        return given
    sid = find_session_id()
    if not sid:
        return ""
    return ("%s %s" % (sid, find_place())).strip()


def cmd_ask(args: argparse.Namespace) -> int:
    # An «Abrir agente» card keeps only a requester that the caller names: the router run that sends it ends at
    # once, so its own session id would name a session that nobody can wake.
    if args.kind != "agent":
        args.requester = resolve_requester(args.requester)
    if not args.requester and args.kind != "agent":
        print(NO_SESSION, file=sys.stderr)
        return 2
    if not args.force_context:
        err = check_context(args.context)
        if err:
            print(err, file=sys.stderr)
            return 2
    hours = args.expires_hours
    if hours is None:
        hours = db.QUESTIONS_EXPIRY_HOURS if args.kind == "questions" else db.DEFAULT_EXPIRY_HOURS
    args.expires_hours = hours
    extra = _extra(args)
    if args.kind == "questions":
        if args.question_file == "-":
            raw = sys.stdin.read()
        else:
            with open(args.question_file, encoding="utf-8") as fh:
                raw = fh.read()
        try:
            questions = json.loads(raw)
        except ValueError as exc:
            raise ValueError("bad question JSON: " + str(exc)[:100])
        db.validate_questions(questions)  # fail before the database opens
        rid = db.create_request(
            db.connect(), "questions", args.title, args.context, args.requester,
            options=questions, expires_hours=hours, requester_label=args.requester_label,
            replaces=args.replaces, extra=extra)
        _replace_old(args, rid)
        _out({"id": rid})
        return 0
    conn = db.connect()
    if args.kind == "email":
        from polar import gmail
        if not gmail.connected():
            print(gmail.NOT_CONNECTED, file=sys.stderr)
            return 2
        text = _read_text(args)
        rid = db.create_request(
            conn, "email", args.title, args.context, args.requester, draft_text=text,
            target_thread_ts=args.thread, expires_hours=args.expires_hours,
            requester_label=args.requester_label, replaces=args.replaces, email_cc=args.cc)
    elif args.kind == "reply":
        if args.text_file:
            with open(args.text_file, encoding="utf-8") as fh:
                text = fh.read().strip()
        else:
            text = args.text
        rid = db.create_request(
            conn, "reply", args.title, args.context, args.requester, draft_text=text,
            target_channel=args.channel, target_thread_ts=args.thread,
            expires_hours=args.expires_hours, requester_label=args.requester_label,
            reply_to_ts=args.reply_to, replaces=args.replaces)
        if (db.get(conn, rid) or {}).get("kind") == "report":
            print("Polar: an answer to the owner is a report, not a reply; sent as report %s" % rid,
                  file=sys.stderr)
    elif args.kind == "agent":
        with open(args.brief_file, encoding="utf-8") as fh:
            brief = fh.read().strip()
        if not brief:
            raise ValueError("agent needs a brief")
        rid = db.create_request(
            conn, "decision", args.title, args.context, args.requester, draft_text=brief,
            target_channel=args.channel, options=[{"key": "agent", "label": "Abrir agente"},
                                                  {"key": "no", "label": "No"}],
            expires_hours=args.expires_hours, requester_label=args.requester_label,
            reply_to_ts=args.reply_to, replaces=args.replaces, agent_where=args.where)
    else:
        rid = db.create_request(
            conn, "decision", args.title, args.context, args.requester,
            options=[_parse_option(o) for o in args.option], expires_hours=args.expires_hours,
            requester_label=args.requester_label, replaces=args.replaces, extra=extra,
            canary=bool(getattr(args, "canary", False)))
    _replace_old(args, rid)
    _out({"id": rid})
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    import os
    args.requester = resolve_requester(args.requester)
    if args.text_file == "-":
        text = sys.stdin.read().strip()
    else:
        with open(args.text_file, encoding="utf-8") as fh:
            text = fh.read().strip()
    if args.silent and args.file:
        raise ValueError("--silent sends no DM, so there is no thread for --file; drop one of them")
    db.check_report_evidence(text, args.requester, args.silent)  # the evidence rule uses the flag the caller gave
    if not args.silent and not args.card and db.asks_nothing(args.next, args.revisar, args.resume, args.file):
        args.silent = True  # nothing for the owner to do: stored for the web UI reports list, no card, no DM, no push
    files = []
    for f in args.file:
        path = os.path.abspath(os.path.expanduser(f))
        if not os.path.isfile(path):
            raise ValueError("file not found: %s" % f)
        files.append(keep_file(path))
    report = {"links": args.link, "files": files, "resume": args.resume, "next": args.next}
    if not args.revisar:
        rid = db.create_request(
            db.connect(), "report", args.title, "", args.requester, draft_text=text,
            expires_hours=args.expires_hours or db.REPORT_EXPIRY_HOURS,
            requester_label=args.requester_label, report=report,
            silent=args.silent, target_channel=args.channel, target_thread_ts=args.thread)
        _out({"id": rid})
        return 0
    return _report_with_followups(args, text, report)


def _report_with_followups(args: argparse.Namespace, text: str, report: Dict[str, Any]) -> int:
    """`--revisar`: check every follow-up with the bus first, add them, create the card, then link them to it."""
    from polar import followups as fu
    if len(args.revisar) > fu.MAX_FOLLOWUPS:
        raise ValueError("report takes at most %d --revisar follow-ups" % fu.MAX_FOLLOWUPS)
    items = [fu.parse_revisar(r) for r in args.revisar]
    db.validate_report(report["links"], report["files"], report["resume"], report["next"])  # refuse before the bus
    sid = fu.session_id(args.requester)
    if not sid:
        raise ValueError(fu.NO_SESSION)
    for spec, what in items:  # nothing is stored until every item passes
        check = fu.add(spec, sid, "session", args.title, text=what, dry_run=True)
        if check.get("error"):
            raise ValueError(str(check["error"]))
    added: List[Dict[str, Any]] = []
    created: List[int] = []  # new rows only: a duplicate is an older follow-up that another card may show

    def undo() -> None:
        for fid in created:
            fu.cancel(fid, by="session")

    for spec, what in items:
        data = fu.add(spec, sid, "session", args.title, text=what)
        if data.get("error") or data.get("id") is None:
            undo()
            raise ValueError(str(data.get("error") or fu.BUS_DOWN))
        if not data.get("duplicate"):
            created.append(data["id"])
        added.append(fu.entry(dict(data, text=data.get("text") or what)))
    try:
        rid = db.create_request(
            db.connect(), "report", args.title, "", args.requester, draft_text=text,
            expires_hours=args.expires_hours or db.REPORT_EXPIRY_HOURS,
            requester_label=args.requester_label, report=report, followups=added,
            silent=args.silent, target_channel=args.channel, target_thread_ts=args.thread)
    except Exception:
        undo()
        raise
    for e in added:
        linked = fu.set_card(e["id"], rid)
        if linked.get("error"):
            print("polar: follow-up %s was not linked to the card: %s" % (e["id"], linked["error"]), file=sys.stderr)
    _out({"id": rid, "followups": [e["id"] for e in added]})
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "ask":
            return cmd_ask(args)
        if args.cmd == "report":
            return cmd_report(args)
        if args.cmd == "revise-digest":
            from polar import revise_digest
            out = revise_digest.run(args.days, send=args.start)
            if args.start:
                _out(out)
            else:
                print(out["markdown"], end="")
            return 0 if not args.start or out.get("started") or out["count"] < 2 else 1
        if args.cmd == "status":
            req = db.get(db.connect(), args.id)
            if not req:
                _out({"error": "not found"})
                return 1
            _out(_public(req))
            return 0
        if args.cmd == "discard":
            ok = db.withdraw(db.connect(), args.id)
            _out({"id": args.id, "discarded": ok})
            return 0 if ok else 1
        if args.cmd == "list":
            for req in db.list_requests(db.connect(), args.status, args.since):
                _out(_public(req))
            return 0
        if args.cmd == "inbox":
            from polar.inbox import run
            try:
                for msg in run(args.since_ts):
                    _out(msg)
            except Exception as exc:  # print the Slack error code only, never request data
                resp = getattr(exc, "response", None)
                code = resp.get("error") if resp is not None and hasattr(resp, "get") else str(exc)[:120]
                _out({"error": code})
                return 1
            return 0
        if args.cmd == "mail":
            return cmd_mail(args)
        if args.cmd == "thread":
            from polar.inbox import run_thread
            try:
                for msg in run_thread(args.channel, args.thread_ts):
                    _out(msg)
            except Exception as exc:
                resp = getattr(exc, "response", None)
                code = resp.get("error") if resp is not None and hasattr(resp, "get") else str(exc)[:120]
                _out({"error": code})
                return 1
            return 0
        if args.cmd == "files":
            from polar import files
            rows = files.listing(args.key)
            if rows is None:
                rows = files.fetch_by_id(args.key)
            if rows is None:
                _out({"error": "not found"})
                return 1
            _out(rows)
            return 0
        if args.cmd == "replay":
            from slack_sdk import WebClient
            from polar.app import owner_id
            from polar.tasks import Tasks
            from polar.tokens import get_token
            token = get_token("bot")
            if not token:
                _out({"error": "bot token missing"})
                return 1
            try:
                ok = Tasks(db.connect(), WebClient(token=token), owner_id() or "").replay(args.ts)
            except Exception as exc:  # the Slack error code only
                resp = getattr(exc, "response", None)
                _out({"error": resp.get("error") if resp is not None and hasattr(resp, "get") else type(exc).__name__})
                return 1
            _out({"ts": args.ts, "taken": ok})
            return 0 if ok else 1
        if args.cmd == "tasks":
            from polar.tasks import public_task
            conn = db.connect()
            sql = "SELECT * FROM tasks WHERE kind='task'" + ("" if args.status == "all" else " AND status=?")
            rows = [public_task(dict(r)) for r in conn.execute(
                sql + " ORDER BY created_at, rowid", [] if args.status == "all" else [args.status]).fetchall()]
            if args.json:
                _out(rows)
            else:
                for r in rows:
                    print("%s  %-8s %s  %s" % (r["id"], r["status"], r["tmux"] or "-", r["title"]))
            return 0
        if args.cmd == "follow-bus":
            from polar.tasks import Tasks
            _out(Tasks(db.connect(), None, "").follow_bus(force=True, dry_run=args.dry_run))
            return 0
        if args.cmd == "steers":
            from polar.tasks import Tasks
            _out(Tasks(db.connect(), None, "").laptop_steers())
            return 0
        if args.cmd == "queue":
            from polar.tasks import Tasks
            _out(Tasks(db.connect(), None, "").queue_list())
            return 0
        if args.cmd == "claim":
            from polar.tasks import Tasks
            ok = Tasks(db.connect(), None, "").claim(
                args.id, args.host, args.tab, args.session, args.pane)
            _out({"id": args.id, "claimed": ok})
            return 0 if ok else 1
        if args.cmd == "handoff":
            from polar.tasks import Tasks
            text = sys.stdin.read() if args.text_file == "-" else open(args.text_file).read()
            if not text.strip():
                print("polar: empty handoff text", file=sys.stderr)
                return 2
            tid = Tasks(db.connect(), None, "").create_handoff(args.title, text, args.requester)
            _out({"id": tid, "where": "laptop", "status": "queued"})
            return 0 if tid else 1
        if args.cmd == "task-done":
            from polar.tasks import Tasks
            ok = Tasks(db.connect(), None, "").task_done(args.id)
            _out({"id": args.id, "done": ok})
            return 0 if ok else 1
        if args.cmd == "steer-done":
            from polar.tasks import Tasks
            ok = Tasks(db.connect(), None, "").mark_steer_done(args.id)
            _out({"id": args.id, "delivered": ok})
            return 0 if ok else 1
        if args.cmd == "triage":
            from polar.tasks import public_triage
            _out([public_triage(dict(r)) for r in db.connect().execute(
                "SELECT * FROM tasks WHERE kind='task' AND status='triage' ORDER BY created_at, rowid").fetchall()])
            return 0
        if args.cmd == "triage-set":
            from polar.tasks import Tasks
            if args.match and not (args.confidence and args.label):
                raise ValueError("--match needs --label and --confidence")
            ok = Tasks(db.connect(), None, "").triage_set(
                args.id, None if args.none else args.match, args.label, args.confidence or "", args.reason, args.where,
                args.area)
            _out({"id": args.id, "set": ok})
            return 0 if ok else 1
        if args.cmd == "routes":
            from polar.tasks import public_route
            _out([public_route(dict(r)) for r in db.connect().execute(
                "SELECT * FROM tasks WHERE kind='task' AND status='routed' AND route_done=0"
                " ORDER BY created_at, rowid").fetchall()])
            return 0
        if args.cmd == "route-done":
            from polar.tasks import Tasks
            ok = Tasks(db.connect(), None, "").mark_route_done(args.id)
            _out({"id": args.id, "delivered": ok})
            return 0 if ok else 1
        if args.cmd == "route-failed":
            from polar.tasks import Tasks
            ok = Tasks(db.connect(), None, "").route_failed(args.id)
            _out({"id": args.id, "failed": ok})
            return 0 if ok else 1
        if args.cmd == "picks":
            from polar.tasks import Tasks
            _out(Tasks(db.connect(), None, "").planning_picks())
            return 0
        if args.cmd == "plan-set":
            from polar.tasks import Tasks
            raw = sys.stdin.read() if args.file == "-" else open(args.file).read()
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise ValueError("plan is not valid JSON: %s" % exc)
            Tasks(db.connect(), None, "").plan_set(args.id, data)
            _out({"id": args.id, "planned": True})
            return 0
        if args.cmd == "expire":
            ids = db.expire_due(db.connect())
            _out({"expired": ids})
            return 0
        if args.cmd == "demo":
            from polar.web import demo as web_demo
            return web_demo.run(args.port, args.lang)
        if args.cmd == "ui-url":
            from polar.web import server as web_ui
            port = web_ui.ui_port()
            if not port:
                _out({"error": "POLAR_UI_PORT is not set: the web UI is off"})
                return 2
            print(web_ui.login_url(port))
            return 0
        if args.cmd == "serve":
            from polar.app import serve
            return serve()
        if args.cmd == "doctor":
            from polar.app import doctor
            return doctor()
    except (ValueError, OSError) as exc:
        _out({"error": str(exc)})
        return 2
    return 2
