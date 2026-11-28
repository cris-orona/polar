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
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from polar import db, webapi
from polar.web import views
from polar.web.strings import has, t

log = logging.getLogger("polar")
MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
FILE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; sandbox"
INLINE_OK = {"image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}  # a PDF is a download
CHUNK = 64 * 1024
STATIC_FILES = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
ACTIONS = ("choose", "answer", "send", "discard", "revise", "next", "seen")
COOKIE = "polar_ui"
COOKIE_MAX_AGE = 180 * 24 * 3600  # seconds: about six months
PUBLIC = ("/healthz", "/login")  # with /static/: the only paths that need no session cookie
LOGIN_CMD = "polar ui-url"


def _mac(secret: str, label: str) -> str:
    return hmac.new(secret.encode("utf-8"), label.encode("utf-8"), hashlib.sha256).hexdigest()


def login_token(secret: str) -> str:
    """The token of the login link. It comes from the web API secret (data/web-token, mode 0600)."""
    return _mac(secret, "polar-ui-login")


def session_value(secret: str) -> str:
    """The value of the session cookie that /login sets."""
    return _mac(secret, "polar-ui-session")


def secret_path(data_dir: Optional[Path] = None) -> Path:
    """The same secret file as the local web API: one secret for both."""
    return Path(data_dir) / "web-token" if data_dir else webapi.token_path()


def login_url(port: int, data_dir: Optional[Path] = None) -> str:
    """The login link: open it once in the browser, it sets the session cookie. It is a credential: never log it."""
    return "http://127.0.0.1:%d/login?token=%s" % (port, login_token(webapi.load_token(secret_path(data_dir))))


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


def _too_long(value: str) -> bool:
    return len(value.strip()) > db.MAX_REPLY_CHARS  # the core counts the text after strip


def disposition(kind: str, name: str) -> str:
    """A Content-Disposition value that cannot break the header: no control characters, quotes or backslashes,
    an ASCII `filename` for old clients and the full name as RFC 5987 `filename*`."""
    clean = "".join(c for c in name if c >= " " and c not in '"\\\x7f' and not 0x80 <= ord(c) < 0xa0).strip() or "file"
    ascii_name = "".join(c if " " <= c < "\x7f" else "_" for c in clean)
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (kind, ascii_name, quote(clean, safe=""))


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
        if any(_too_long(v) for v in ans.values()):
            return None, t("v_too_long", n=db.MAX_REPLY_CHARS)
        p["answers"] = dict(ans)
        note = data.get("note")
        if note is not None:
            if not isinstance(note, str):
                return None, t("v_answers")
            if _too_long(note):
                return None, t("v_too_long", n=db.MAX_REPLY_CHARS)
            if note.strip():
                p["note"] = note.strip()
    elif action == "send":
        if data.get("text") is not None:
            if not isinstance(data["text"], str) or not data["text"].strip():
                return None, t("v_text")
            if _too_long(data["text"]):
                return None, t("v_too_long", n=db.MAX_REPLY_CHARS)
            p["text"] = data["text"]
    elif action == "revise":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, t("v_revise")
        if _too_long(text):
            return None, t("v_too_long", n=db.MAX_REPLY_CHARS)
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
    if code == 400:  # a specific UI text when the core said why (its own texts are Spanish)
        said = str(out.get("error") or "")
        for start, key in (("Máximo", "v_too_long"), ("Faltan respuestas", "v_answers"), ("Faltan las respuestas", "v_answers"),
                           ("Respuesta vacía", "v_answers"), ("Opción no válida", "v_option"),
                           ("Seguimiento no válido", "v_next"), ("Falta el texto", "v_text")):
            if said.startswith(start):
                return t(key, n=db.MAX_REPLY_CHARS) if key == "v_too_long" else t(key)
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
        self.secret = webapi.load_token(secret_path(self.data_dir))
        self.login, self.session = login_token(self.secret), session_value(self.secret)
        extra = [h.strip().lower() for h in (os.environ.get("POLAR_UI_HOSTS") or "").split(",") if h.strip()]
        self.hosts = ["127.0.0.1:%d" % self.port, "localhost:%d" % self.port] + extra
        self.origins = {s + h for h in self.hosts for s in ("http://", "https://")}
        self.owner = (os.environ.get("POLAR_UI_OWNER") or "").strip()
        self.owner_header = (os.environ.get("POLAR_UI_OWNER_HEADER") or "Tailscale-User-Login").strip()
        self.roots = [os.path.realpath(str(self.data_dir / "files")), os.path.realpath(str(self.data_dir / "uploads"))]

    def csrf(self) -> str:
        """The CSRF token, bound to the session value: another secret or another session gives another token."""
        return _mac(self.secret, "polar-ui-csrf:%s:%s" % (self.session, self.owner))

    def allow_send(self) -> bool:
        return "send" in webapi.allowed()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.port

    @property
    def login_url(self) -> str:
        return "http://127.0.0.1:%d/login?token=%s" % (self.port, self.login)


class Handler(webapi.CloseOnError):
    """Every answer goes through `send_response`: an error answer (>= 400) closes the connection (see CloseOnError)."""
    server_version = "polar-ui"
    sys_version = ""  # no Python version in the Server header
    protocol_version = "HTTP/1.1"
    timeout = 30  # seconds: a slow or stalled client cannot hold a thread for ever
    server: UIServer

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # also silences «Request timed out» of an idle keep-alive connection

    def version_string(self) -> str:
        return self.server_version

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Errors of the HTTP layer (unknown method, bad request line, long header): a short plain answer with the
        same security headers as a page. `_send` passes `send_response`, so the connection closes."""
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "Error"
        if not str(getattr(self, "request_version", "")).startswith("HTTP/1."):
            self.request_version = "HTTP/1.0"  # a bad request line: still send a status line and the headers
        self._send(code, "%d %s\n" % (code, phrase), "text/plain; charset=utf-8")

    # -- replies -------------------------------------------------------------------------------------------------------
    def _send(self, code: int, body: Any, ctype: str = "text/html; charset=utf-8",
              extra: Optional[Dict[str, str]] = None, csp: str = CSP) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self._head(code, ctype, len(data), extra, csp)
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(data)

    def _head(self, code: int, ctype: str, length: int, extra: Optional[Dict[str, str]], csp: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

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

    def _has_session(self) -> bool:
        """True when the request carries the session cookie that /login sets."""
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return False
        got = jar[COOKIE].value if COOKIE in jar else ""
        return bool(got) and hmac.compare_digest(got.encode("utf-8"), self.server.session.encode("utf-8"))

    def _need_login(self, path: str) -> None:
        """401 without a session: JSON for the API, else a short page that says how to log in (no card data)."""
        if path.startswith("/api/") or self.command == "POST":
            return self._json(401, {"ok": False, "error": t("err_login")})
        self._send(401, views.login_page(t("login_need"), LOGIN_CMD))

    def _login(self, token: str) -> None:
        if not token or not hmac.compare_digest(token.encode("utf-8"), self.server.login.encode("utf-8")):
            return self._send(403, views.login_page(t("login_bad"), LOGIN_CMD))
        cookie = "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d" % (COOKIE, self.server.session, COOKIE_MAX_AGE)
        self._send(303, "", "text/plain; charset=utf-8", {"Location": "/", "Set-Cookie": cookie})

    # -- verbs ---------------------------------------------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:  # framing (no body on GET, one plain length) is checked first, in parse_request
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path not in PUBLIC and not path.startswith("/static/") and not self._has_session():
            return self._need_login(path)
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # never a stack trace on a page
            log.error("web UI page failed: %s", type(exc).__name__)
            if self.response_started:  # the headers went out: a 500 now would land inside the body
                self.close_connection = True
                return
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
        if path == "/login":
            return self._login(arg("token"))
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
        if len(parts) != 4 or not (parts[3].isascii() and parts[3].isdigit()) or len(parts[2]) > 40:
            return self._text(404, t("not_found"))
        card = db.get(self.server.polar.conn, unquote(parts[2]))
        real = self.resolve_file(card, int(parts[3])) if card else None
        if not real:
            return self._text(404, t("not_found"))
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        disp = "inline" if ctype in INLINE_OK else "attachment"
        with open(real, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            self._head(200, ctype + ("; charset=utf-8" if ctype == "text/plain" else ""), size,
                       {"Content-Disposition": disposition(disp, os.path.basename(real))}, FILE_CSP)
            if self.command == "HEAD":
                return
            left = size  # stream: a large file never sits in memory, and never more bytes than Content-Length
            while left > 0:
                chunk = fh.read(min(CHUNK, left))
                if not chunk:
                    self.close_connection = True  # the file shrank: the client must not wait for the rest
                    return
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self) -> None:
        if not self._guard():
            return
        if not self._has_session():
            return self._need_login(urlparse(self.path).path)
        if urlparse(self.path).path != "/api/act":
            return self._json(404, {"ok": False, "error": t("not_found")})
        if (self.headers.get("Origin") or "") not in self.server.origins:
            return self._json(403, {"ok": False, "error": t("err_origin")})
        got = self.headers.get("X-CSRF-Token") or ""
        if not got or not hmac.compare_digest(got.encode("utf-8"), self.server.csrf().encode("utf-8")):
            return self._json(403, {"ok": False, "error": t("err_csrf")})
        length = self.body_length()  # parse_request already refused Transfer-Encoding and bad lengths
        if length is None:
            return self._json(411, {"ok": False, "error": t("err_bad")})
        if length > MAX_BODY:
            return self._json(413, {"ok": False, "error": t("err_too_big")})
        try:
            data = json.loads(self.read_body(length).decode("utf-8"))
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
    log.info("web UI on %s; run `%s` to print the login link (POLAR_WEB_URL: %s)", server.url, LOGIN_CMD,
             os.environ.get("POLAR_WEB_URL") or "not set")
    return server
