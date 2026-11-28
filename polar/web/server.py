"""The web UI server: pages, files and one action endpoint. Standard library only, bound to 127.0.0.1.

Each action runs in this process through `webapi.perform`, the same function as the local action API, so the
Slack card changes too and the same gates (POLAR_WEB_ALLOW) apply. See SECURITY.md for the checks on each request."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from polar import db, webapi
from polar.web import views
from polar.web.strings import has, t

log = logging.getLogger("polar")
MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
FILE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox"
INLINE_OK = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}
STATIC_FILES = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
ACTIONS = ("choose", "answer", "send", "discard", "revise", "next", "seen")
SECRET_FILE = "web-ui-secret"


def ui_port() -> Optional[int]:
    """POLAR_UI_PORT as a port number. Unset, empty, 0 or not a number: None (the UI is off)."""
    raw = (os.environ.get("POLAR_UI_PORT") or "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        log.warning("web UI off: POLAR_UI_PORT is not a number")
        return None
    return port if 0 < port < 65536 else None


def build_payload(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Check what the page sent. Return (payload for webapi.perform, None) or (None, error text)."""
    if not isinstance(data, dict):
        return None, t("err_bad")
    rid, action = data.get("id"), data.get("action")
    if not isinstance(rid, str) or not rid or len(rid) > 40:
        return None, t("v_id")
    if action not in ACTIONS:
        return None, t("v_action")
    if action == "seen":  # «Seen» on a report is the «ok» option: the report closes as chosen
        return {"id": rid, "action": "choose", "option": "ok"}, None
    p: Dict[str, Any] = {"id": rid, "action": action}
    if action == "choose":
        if not isinstance(data.get("option"), str) or not data["option"]:
            return None, t("v_option")
        p["option"] = data["option"]
    elif action == "answer":
        ans = data.get("answers")
        if (not isinstance(ans, dict) or len(ans) > 20
                or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in ans.items())):
            return None, t("v_answers")
        p["answers"] = dict(ans)
        note = data.get("note")
        if note is not None:
            if not isinstance(note, str) or len(note) > db.MAX_REPLY_CHARS:
                return None, t("v_note")
            if note.strip():
                p["note"] = note.strip()
    elif action == "send":
        if data.get("text") is not None:
            if not isinstance(data["text"], str) or not data["text"].strip():
                return None, t("v_text")
            p["text"] = data["text"]
    elif action == "revise":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, t("v_revise")
        p["text"] = text.strip()
    elif action == "next":
        ix = data.get("indexes")
        if (not isinstance(ix, list) or not 1 <= len(ix) <= db.MAX_REPORT_NEXT
                or any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in ix) or len(set(ix)) != len(ix)):
            return None, t("v_next")
        p["indexes"] = sorted(ix)
    return p, None


def ui_error(code: int, out: Dict[str, Any]) -> str:
    """The page's text for a refusal of webapi.perform (its own texts are Spanish and meant for Slack users)."""
    if code == 403:
        return t("err_gate")
    if code == 409:
        st = str(out.get("status") or "")
        return t("err_changed", status=t("st_" + st) if has("st_" + st) else st or "?")
    if code == 502:
        return t("err_send")
    if code == 400:
        return t("err_bad")
    return t("err_internal")


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, polar: Any, demo: bool = False, data_dir: Optional[Path] = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.polar, self.demo = polar, demo
        self.data_dir = Path(data_dir) if data_dir else db.data_dir()
        self.port = self.server_address[1]
        self.secret = webapi.load_token(self.data_dir / SECRET_FILE)
        extra = [h.strip().lower() for h in (os.environ.get("POLAR_UI_HOSTS") or "").split(",") if h.strip()]
        self.hosts = ["127.0.0.1:%d" % self.port, "localhost:%d" % self.port] + extra
        self.origins = {s + h for h in self.hosts for s in ("http://", "https://")}
        self.owner = (os.environ.get("POLAR_UI_OWNER") or "").strip()
        self.owner_header = (os.environ.get("POLAR_UI_OWNER_HEADER") or "Tailscale-User-Login").strip()
        self.roots = [os.path.realpath(str(self.data_dir / "files")), os.path.realpath(str(self.data_dir / "uploads"))]

    def csrf(self) -> str:
        return hmac.new(self.secret.encode(), b"polar-ui-csrf:" + self.owner.encode(), hashlib.sha256).hexdigest()

    def allow_send(self) -> bool:
        return "send" in webapi.allowed()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.port


class Handler(BaseHTTPRequestHandler):
    server_version = "polar-ui"
    protocol_version = "HTTP/1.1"
    server: UIServer

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # -- replies -------------------------------------------------------------------------------------------------------
    def _send(self, code: int, body: Any, ctype: str = "text/html; charset=utf-8",
              extra: Optional[Dict[str, str]] = None, csp: str = CSP) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code: int, obj: Dict[str, Any]) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _text(self, code: int, text: str) -> None:
        self._send(code, text, "text/plain; charset=utf-8")

    def _page(self, title: str, active: str, body: str, code: int = 200) -> None:
        conn = self.server.polar.conn
        st = views.state(conn)
        self._send(code, views.layout(title, active, body, self.server.csrf(), st["waiting"], self.server.demo, st["v"]))

    # -- checks --------------------------------------------------------------------------------------------------------
    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.hosts:
            self._text(403, t("err_host"))
            return False
        if self.server.owner:
            got = self.headers.get(self.server.owner_header) or ""
            if not hmac.compare_digest(got.encode("utf-8"), self.server.owner.encode("utf-8")):
                self._text(403, t("err_owner"))
                return False
        return True

    # -- verbs ---------------------------------------------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._guard():
            return
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # never a stack trace on a page
            log.error("web UI page failed: %s", type(exc).__name__)
            self._text(500, t("err_internal"))

    def _route_get(self) -> None:
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        conn = self.server.polar.conn

        def arg(name: str) -> str:
            return (qs.get(name) or [""])[0]

        def page_no() -> int:
            try:
                return max(0, min(int(arg("page") or 0), 10000))
            except ValueError:
                return 0
        if path == "/":
            cid = arg("c")
            focus = db.get(conn, cid) if cid and len(cid) <= 40 else None
            body = views.pending_page(views.pending_cards(conn), self.server.allow_send(), focus)
            return self._page(t("nav_pending"), "/", body)
        if path == "/history":
            rows, more = views.history(conn, page_no())
            return self._page(t("nav_history"), "/history", views.history_page(rows, page_no(), more))
        if path in ("/reports", "/informes"):
            q = arg("q").strip()[:200]
            rows, more = views.reports(conn, q, page_no())
            return self._page(t("nav_reports"), "/reports", views.reports_page(rows, q, page_no(), more))
        for prefix in ("/reports/", "/informes/"):
            if path.startswith(prefix):
                card = db.get(conn, unquote(path[len(prefix):])[:40])
                if not card or card["kind"] != "report":
                    return self._page(t("not_found"), "/reports", views.message_page(t("not_found")), 404)
                return self._page(card["title"], "/reports", views.report_page(card, self.server.allow_send()))
        if path.startswith("/files/"):
            return self._file(path)
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if name not in STATIC_FILES:
                return self._text(404, t("not_found"))
            data = (views.STATIC / name).read_bytes()
            return self._send(200, data, STATIC_FILES[name], {"Cache-Control": "public, max-age=3600"})
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path == "/api/state":
            return self._json(200, dict(views.state(conn), ok=True))
        self._page(t("not_found"), "", views.message_page(t("not_found")), 404)

    def resolve_file(self, card: Dict[str, Any], idx: int) -> Optional[str]:
        """The real path of file `idx` of a card, only when it is a file inside one of the data dir's file roots."""
        files: List[Any] = (card.get("report") or {}).get("files") or (card.get("extra") or {}).get("files") or []
        if idx < 0 or idx >= len(files):
            return None
        try:
            real = os.path.realpath(str(files[idx]))
        except (ValueError, OSError):
            return None
        for root in self.server.roots:
            if real.startswith(root + os.sep) and os.path.isfile(real):
                return real
        return None

    def _file(self, path: str) -> None:
        parts = path.split("/")  # "", "files", id, n
        if len(parts) != 4 or not parts[3].isdigit() or len(parts[2]) > 40:
            return self._text(404, t("not_found"))
        card = db.get(self.server.polar.conn, unquote(parts[2]))
        real = self.resolve_file(card, int(parts[3])) if card else None
        if not real:
            return self._text(404, t("not_found"))
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        name = os.path.basename(real).replace('"', "")
        disp = "inline" if ctype in INLINE_OK else "attachment"
        with open(real, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype + ("; charset=utf-8" if ctype == "text/plain" else ""),
                   {"Content-Disposition": '%s; filename="%s"' % (disp, name.encode("ascii", "replace").decode())},
                   csp=FILE_CSP)

    def do_POST(self) -> None:
        if not self._guard():
            return
        if urlparse(self.path).path != "/api/act":
            return self._json(404, {"ok": False, "error": t("not_found")})
        if (self.headers.get("Origin") or "") not in self.server.origins:
            return self._json(403, {"ok": False, "error": t("err_origin")})
        got = self.headers.get("X-CSRF-Token") or ""
        if not got or not hmac.compare_digest(got.encode("utf-8"), self.server.csrf().encode("utf-8")):
            return self._json(403, {"ok": False, "error": t("err_csrf")})
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return self._json(411, {"ok": False, "error": t("err_bad")})
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self._json(413, {"ok": False, "error": t("err_too_big")})
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return self._json(400, {"ok": False, "error": t("err_json")})
        payload, err = build_payload(data)
        if err or payload is None:
            return self._json(400, {"ok": False, "error": err})
        polar = self.server.polar
        try:
            code, out = webapi.perform(polar, payload)
        except Exception as exc:  # never the text: it could hold a token
            log.error("web UI action failed: %s", type(exc).__name__)
            code, out = 500, {"ok": False}
        st = views.state(polar.conn)
        if code == 200 and out.get("ok"):
            return self._json(200, {"ok": True, "status": out.get("status"), "v": st["v"], "waiting": st["waiting"]})
        self._json(code, {"ok": False, "error": ui_error(code, out), "status": out.get("status"), "v": st["v"]})


def start(polar: Any, port: Optional[int] = None) -> Optional[UIServer]:
    """Start the UI in a daemon thread when POLAR_UI_PORT (or `port`) is set. Return None when it is off."""
    port = port if port is not None else ui_port()
    if not port:
        log.info("web UI off: POLAR_UI_PORT is not set")
        return None
    try:
        server = UIServer(port, polar)
    except Exception as exc:  # a busy port or a bad data dir only turns the UI off
        log.warning("web UI off: port %s (%s)", port, type(exc).__name__)
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="polar-ui").start()
    log.info("web UI on %s (POLAR_WEB_URL: %s)", server.url, os.environ.get("POLAR_WEB_URL") or "not set")
    return server
