"""A local HTTP action API: a second front end (a web page on this server) records the owner's decisions.

Each action calls the same `Polar.on_*` method as the matching Slack button, as the owner, with src="web".
It binds 127.0.0.1 only and needs `Authorization: Bearer <token>` (the token is in data/web-token)."""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from polar import alarm, blocks, db
from polar.core import Polar

log = logging.getLogger("polar")
DEFAULT_PORT = 8791
MAX_BODY = 64 * 1024
SRC = "web"


def token_path() -> Path:
    return db.data_dir() / "web-token"


def load_token(path: Optional[Path] = None) -> str:
    """Read the token. Create it (random, mode 0600) when the file is missing. Never log it."""
    path = Path(path) if path else token_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = None  # another process made it first
        if fd is not None:
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


class ActionError(Exception):
    def __init__(self, error: str, code: int = 409):
        super().__init__(error)
        self.error, self.code = error, code


def _status(polar: Polar, rid: str) -> str:
    req = db.get(polar.conn, rid)
    return req["status"] if req else ""


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionError("Falta el texto: %s." % what, 400)
    text = value.strip()
    if len(text) > db.MAX_REPLY_CHARS:
        raise ActionError("Máximo %d caracteres." % db.MAX_REPLY_CHARS, 400)
    return text


def new_task(polar: Polar, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """The owner writes a task in the web composer: the same path as a DM to Polar. A notice in the owner DM is the
    thread of the task (triage finds the session that owns the subject, or a new session starts)."""
    try:
        text = _text(body.get("text"), "la tarea")
    except ActionError as exc:
        return exc.code, {"ok": False, "error": exc.error}
    from polar.tasks import OWNER_DM, clip_title
    head = polar.tasks.say(OWNER_DM, None, "🖥️ Desde Mando: %s" % clip_title(text))
    if not head:
        return 502, {"ok": False, "error": "Slack no tomó el aviso; la tarea no se creó."}
    context = body.get("context")
    context = context[:6000] if isinstance(context, str) else ""
    tid = polar.tasks.create_task(OWNER_DM, head, "web:" + head, text, intake=True, context=context)
    if not tid:
        return 409, {"ok": False, "error": "Esa tarea ya existe."}
    polar.tasks.say(OWNER_DM, head, polar.tasks.received("🧭 Recibido desde Mando."))
    return 200, {"ok": True, "status": "queued", "task": tid}


def calendar_action(body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Mando's calendar: today's events, and the web sign-in (link, then the pasted address)."""
    from polar import gcal
    action = body.get("action")
    try:
        if action == "cal_today":
            return 200, dict(gcal.today(), ok=True)
        if action == "cal_auth_url":
            return 200, {"ok": True, "url": gcal.auth_url()}
        if action == "cal_auth_finish":
            gcal.finish(str(body.get("pasted") or "")[:4000])
            return 200, {"ok": True}
    except gcal.NotConnected:
        return 200, {"ok": True, "connected": False, "events": [], "expired": True}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:  # never the text: it could hold a token
        log.error("calendar failed: %s", type(exc).__name__)
        name = type(exc).__name__
        msg = "Google no dejó leer el calendario (%s)." % name
        if name == "HttpError" and "accessNotConfigured" in str(getattr(exc, "content", b""))[:2000]:
            msg = "Falta encender la API de Calendar en el proyecto de Google."
        return 502, {"ok": False, "error": msg}
    return 400, {"ok": False, "error": "Acción de calendario desconocida."}


def perform(polar: Polar, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """Run one action. Return (HTTP code, JSON body)."""
    if body.get("action") == "task":
        return new_task(polar, body)
    if str(body.get("action") or "").startswith("cal_"):
        return calendar_action(body)
    rid, action = body.get("id"), body.get("action")
    if not isinstance(rid, str) or not isinstance(action, str):
        return 400, {"ok": False, "error": "Faltan id y action."}
    req = db.get(polar.conn, rid)
    if not req:
        return 409, {"ok": False, "error": "No existe esa tarjeta."}
    try:
        _run(polar, req, action, body)
    except ActionError as exc:
        out: Dict[str, Any] = {"ok": False, "error": exc.error}
        if exc.code in (403, 409):
            out["status"] = _status(polar, rid)
        return exc.code, out
    after = _status(polar, rid)
    if action == "send" and after == "failed":
        # The post was tried and Slack or Gmail refused it. The card shows «Falló», as in Slack.
        return 502, {"ok": False, "status": after, "error": "No se pudo enviar. Revisa la tarjeta."}
    return 200, {"ok": True, "status": after}


GATED = ("send", "prod")  # actions with an effect outside this machine


def allowed() -> Tuple[str, ...]:
    """The gated actions that POLAR_WEB_ALLOW turns on (a comma list of «send» and «prod»). Default: none."""
    asked = {x.strip() for x in (os.environ.get("POLAR_WEB_ALLOW") or "").split(",")}
    return tuple(g for g in GATED if g in asked)


def is_prod_op(req: Dict[str, Any]) -> bool:
    """A production step of prod-op: the Go approves a production write."""
    return req["kind"] == "decision" and (req.get("requester") or "").startswith("prod-op ")


def _gate(name: str, what: str) -> None:
    """A message to another person and a production Go stay in Slack until the owner allows them here."""
    if name not in allowed():
        raise ActionError("%s se hace desde Slack." % what, 403)


def _need(req: Dict[str, Any], kinds: Tuple[str, ...], statuses: Tuple[str, ...]) -> None:
    if req["kind"] not in kinds:
        raise ActionError("Esta acción no aplica a este tipo de tarjeta.", 409)
    if req["status"] not in statuses:
        raise ActionError("La tarjeta ya no está abierta (estado: %s)." % req["status"], 409)


def _run(polar: Polar, req: Dict[str, Any], action: str, body: Dict[str, Any]) -> None:
    rid, owner = req["id"], polar.owner_id
    if action == "seen":
        return  # a report needs no read mark: it is «noted» from the start
    if action == "choose":
        key = body.get("option")
        if req["kind"] == "report":
            _need(req, ("report",), ("shown", "noted"))
            opts = ["ok"] + (["resume"] if (req.get("report") or {}).get("resume") else [])
        else:
            _need(req, ("decision",), ("shown",))
            if is_prod_op(req):
                _gate("prod", "El Go de un paso de producción")
            opts = [o["key"] for o in req["options"]]
        if key not in opts:
            raise ActionError("Opción no válida.", 400)
        polar.on_choose(owner, rid, key, src=SRC, and_continue=bool(body.get("and_continue")))
    elif action == "answer":
        _answer(polar, req, body)
    elif action == "send":
        _need(req, db.REPLY_KINDS, ("shown",))
        _gate("send", "Enviar un mensaje a otra persona")
        if body.get("text") is not None:
            text = _text(body["text"], "el texto editado")
            polar.on_edit_submit(owner, blocks.text_view(text, rid))
        polar.on_send(owner, rid, src=SRC)
    elif action == "discard":
        _need(req, db.REPLY_KINDS, ("shown",))
        polar.on_discard(owner, rid, src=SRC)
    elif action == "revise":
        _need(req, ("reply", "decision", "email", "report", "questions"), ("shown", "noted"))
        text = _text(body.get("text"), "qué cambio quieres")
        polar.on_revise_submit(owner, blocks.text_view(text, rid), src=SRC)
    elif action == "next":
        _need(req, ("report",), ("shown", "noted"))
        steps = (req.get("report") or {}).get("next") or []
        if "indexes" in body:
            idxs = body["indexes"]
            if not isinstance(idxs, list) or not 1 <= len(idxs) <= len(steps) or len(set(map(repr, idxs))) != len(idxs):
                raise ActionError("Seguimiento no válido.", 400)
        else:
            idxs = [body.get("index")]
        if any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(steps) for i in idxs):
            raise ActionError("Seguimiento no válido.", 400)
        polar.on_next(owner, rid, src=SRC, indexes=idxs)
    else:
        raise ActionError("Acción desconocida.", 400)
    # The core refuses in silence. A status that did not change means this call did not act.
    if _status(polar, rid) == req["status"]:
        raise ActionError("La tarjeta ya cambió (estado: %s)." % _status(polar, rid), 409)


def _answer(polar: Polar, req: Dict[str, Any], body: Dict[str, Any]) -> None:
    _need(req, ("questions",), ("shown",))
    rid, owner = req["id"], polar.owner_id
    given = body.get("answers")
    note = body.get("note")
    if not isinstance(given, dict) or not given and not blocks.answers_of(req):
        raise ActionError("Faltan las respuestas.", 400)
    questions = {q["key"]: q for q in req["options"]}
    plan = []
    for qkey, value in given.items():
        q = questions.get(qkey)
        if q is None:
            raise ActionError("Pregunta desconocida: %s." % qkey, 400)
        if not isinstance(value, str) or not value.strip():
            raise ActionError("Respuesta vacía a la pregunta %s." % qkey, 400)
        if value in [o["key"] for o in q.get("options", [])]:
            plan.append((qkey, value, None))
        else:
            plan.append((qkey, None, _text(value, "la respuesta a %s" % qkey)))
    if note is not None and not isinstance(note, str):
        raise ActionError("La nota no es texto.", 400)
    if note and len(note.strip()) > db.MAX_REPLY_CHARS:
        raise ActionError("Máximo %d caracteres." % db.MAX_REPLY_CHARS, 400)
    # Check the whole set before any write, so a refusal leaves the card as it was.
    have = set(blocks.answers_of(req)) | set(given)
    gap = [i for i, q in enumerate(req["options"], 1) if q["key"] not in have]
    if gap:
        raise ActionError("Faltan respuestas: " + ", ".join(str(n) for n in gap) + ".", 400)
    for qkey, okey, text in plan:
        if okey is not None:
            polar.on_q_answer(owner, rid, qkey, okey)
        else:
            polar.on_q_submit(owner, blocks.text_view(text, json.dumps({"id": rid, "q": qkey})))
    if note and note.strip():
        polar.on_q_submit(owner, blocks.text_view(note.strip(), json.dumps({"id": rid, "q": None})))
    polar.on_q_send(owner, rid, src=SRC)


class _Handler(BaseHTTPRequestHandler):
    server_version = "polar-web"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # no request line in the log: it is not needed
        pass

    def _json(self, code: int, obj: Dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self) -> bool:
        port = self.server.server_address[1]  # type: ignore[attr-defined]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            self._json(403, {"ok": False, "error": "Host no permitido."})
            return False
        got = self.headers.get("Authorization") or ""
        want = "Bearer " + self.server.token  # type: ignore[attr-defined]
        if not hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8")):
            self._json(401, {"ok": False, "error": "No autorizado."})
            return False
        return True

    def do_GET(self) -> None:
        if not self._guard():
            return
        if self.path == "/health":
            self._json(200, {"ok": True, "allow": list(allowed())})
        else:
            self._json(404, {"ok": False, "error": "No existe."})

    def do_POST(self) -> None:
        if not self._guard():
            return
        if self.path != "/act":
            self._json(404, {"ok": False, "error": "No existe."})
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._json(400, {"ok": False, "error": "Falta Content-Length."})
            return
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._json(413, {"ok": False, "error": "Cuerpo demasiado grande."})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self._json(400, {"ok": False, "error": "JSON no válido."})
            return
        try:
            code, out = perform(self.server.polar, body)  # type: ignore[attr-defined]
        except Exception as exc:  # never show the text: it could hold a token
            log.error("web action failed: %s", type(exc).__name__)
            code, out = 500, {"ok": False, "error": "Error interno."}
            alarm.alarm("polar:web500", "Polar: la API web devolvió un error 500", "web action failed: %s" % type(exc).__name__)
        self._json(code, out)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, polar: Polar, token: str):
        super().__init__(("127.0.0.1", port), _Handler)
        self.polar, self.token = polar, token


BIND_WAIT = 30.0  # seconds: at a restart the old Polar holds the port for a moment
late_server: Optional[WebServer] = None  # the server that came up after a wait (tests close it)


def _serve(server: WebServer, port: int) -> WebServer:
    threading.Thread(target=server.serve_forever, daemon=True, name="polar-web").start()
    log.info("web API on 127.0.0.1:%d", port)
    return server


def _bind_later(polar: Polar, port: int, token: str, wait: float) -> None:
    """Try the port once a second for `wait` seconds. Polar itself runs meanwhile."""
    global late_server
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(1.0 if wait >= 2 else 0.1)
        try:
            server = WebServer(port, polar, token)
        except OSError:
            continue
        late_server = _serve(server, port)
        return
    log.warning("web API off: port %s is not available (waited %d s)", port, wait)


def start(polar: Polar, port: Optional[int] = None, token: Optional[str] = None,
          wait: Optional[float] = None) -> Optional[WebServer]:
    """Start the API in a daemon thread. Return None (and log one line) when it cannot or must not run.
    A busy port: a restart starts the new Polar while the old one still holds the port, so try again in the
    background for `wait` seconds (BIND_WAIT) and then give up. `wait=0`: give up at once."""
    if port is None:
        try:
            port = int(os.environ.get("POLAR_WEB_PORT", str(DEFAULT_PORT)))
        except ValueError:
            log.warning("web API off: POLAR_WEB_PORT is not a number")
            return None
    if port == 0:
        log.info("web API off: POLAR_WEB_PORT=0")
        return None
    try:
        token = token or load_token()
    except Exception as exc:
        log.warning("web API off: %s", type(exc).__name__)
        return None
    try:
        server = WebServer(port, polar, token)
    except OSError as exc:
        wait = BIND_WAIT if wait is None else wait
        if wait <= 0:
            log.warning("web API off: port %s is not available (%s)", port, type(exc).__name__)
            return None
        log.warning("web API waits for port %s (%s); it tries again for %d s", port, type(exc).__name__, wait)
        threading.Thread(target=_bind_later, args=(polar, port, token, wait), daemon=True,
                         name="polar-web-wait").start()
        return None
    except Exception as exc:
        log.warning("web API off: %s", type(exc).__name__)
        return None
    return _serve(server, port)
