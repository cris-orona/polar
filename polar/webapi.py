"""A local HTTP action API: a second front end (a web page on this server) records the owner's decisions.

Each action calls the same `Polar.on_*` method as the matching Slack button, as the owner, with src="web".
It binds 127.0.0.1 only and needs `Authorization: Bearer <token>` (the token is in data/web-token)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from polar import alarm, blocks, db, followups
from polar.core import Polar

log = logging.getLogger("polar")
DEFAULT_PORT = 8791
MAX_BODY = 64 * 1024
SRC = "web"
PLACE = re.compile(r"[\w.-]+")
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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


def _refused(err: Any) -> None:
    """The core returns a Slack modal error dict when it refuses a typed text. Raise it as a 400."""
    if err:
        text = ((err.get("errors") or {}).get("text_block") if isinstance(err, dict) else None) or "Texto no válido."
        raise ActionError(str(text), 400)


def new_task(polar: Polar, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """The owner writes a task in the web composer: the same path as a DM to Polar. A notice in the owner DM is the
    thread of the task (triage finds the session that owns the subject, or a new session starts)."""
    try:
        text = _text(body.get("text"), "la tarea")
    except ActionError as exc:
        return exc.code, {"ok": False, "error": exc.error}
    from polar.tasks import OWNER_DM, clip_title
    loud = polar.tasks.notices_on()
    head, msg_ts = "", "web:" + uuid.uuid4().hex  # quiet mode: no notice, so no Slack thread
    if loud:
        head = polar.tasks.say(OWNER_DM, None, "🖥️ Desde Mando: %s" % clip_title(text))
        if not head:
            return 502, {"ok": False, "error": "Slack no tomó el aviso; la tarea no se creó."}
        msg_ts = "web:" + head
    context = body.get("context")
    context = context[:6000] if isinstance(context, str) else ""
    tid = polar.tasks.create_task(OWNER_DM, head, msg_ts, text, intake=True, context=context)
    if not tid:
        return 409, {"ok": False, "error": "Esa tarea ya existe."}
    if loud:
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
    if action == "respond":
        try:
            if not respond_on():
                raise ActionError("La caja Responder está apagada.", 403)
            if req["status"] not in ("shown", "noted"):
                return _respond_closed(polar, req, body)
            try:
                _respond(polar, req, body)
            except _Moved as moved:
                # Someone else moved the card first: the note still goes to the agent.
                if not moved.note:
                    raise ActionError("La tarjeta ya cambió (estado: %s)." % _status(polar, rid), 409)
                return _respond_closed(polar, db.get(polar.conn, rid), dict(body, option=None, answers=None,
                                                                            indexes=None))
            return 200, {"ok": True, "status": _status(polar, rid)}
        except ActionError as exc:
            out = {"ok": False, "error": exc.error}
            if exc.code in (403, 409):
                out["status"] = _status(polar, rid)
            return exc.code, out
    if action in ("later", "fu_cancel"):
        return _followup_action(polar, req, action, body)
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


def _followup_action(polar: Polar, req: Dict[str, Any], action: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """`later` and `fu_cancel` do not change the card status, so they skip the «status did not change» check."""
    try:
        if action == "later":
            data = polar.later(req["id"], body.get("when"))
        else:
            data = polar.fu_cancel(req["id"], body.get("fu"))
    except followups.FollowupError as exc:
        out: Dict[str, Any] = {"ok": False, "error": exc.error}
        if exc.code == 409:
            out["status"] = _status(polar, req["id"])
        return exc.code, out
    return 200, {"ok": True, "followup": data}


def respond_on() -> bool:
    """The «Responder» box is off until POLAR_RESPOND=on."""
    return (os.environ.get("POLAR_RESPOND") or "").strip().lower() == "on"


GATED = ("send", "prod")  # actions with an effect outside this machine


def allowed() -> Tuple[str, ...]:
    """The gated actions that POLAR_WEB_ALLOW turns on (a comma list of «send» and «prod»). Default: none."""
    asked = {x.strip() for x in (os.environ.get("POLAR_WEB_ALLOW") or "").split(",")}
    return tuple(g for g in GATED if g in asked) + (("respond",) if respond_on() else ())


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
            _refused(polar.on_edit_submit(owner, blocks.text_view(text, rid)))
            # The edit must be stored before the send: else the old draft would go out.
            if (db.get(polar.conn, rid) or {}).get("final_text") != text:
                raise ActionError("La tarjeta ya cambió (estado: %s)." % _status(polar, rid), 409)
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


class _Moved(Exception):
    """The card left its open status, but not by this call."""
    def __init__(self, note: str) -> None:
        super().__init__("moved")
        self.note = note


def _clip(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _note_of(body: Dict[str, Any]) -> str:
    note = body.get("note")
    if note is None:
        return ""
    if not isinstance(note, str):
        raise ActionError("La nota no es texto.", 400)
    note = note.strip()
    if len(note) > db.MAX_REPLY_CHARS:
        raise ActionError("Máximo %d caracteres." % db.MAX_REPLY_CHARS, 400)
    return note


def _stored(summary: str, note: str) -> Dict[str, Any]:
    """What the card keeps of one «Responder» answer. polar-notify builds the one agent line from it."""
    out: Dict[str, Any] = {"respond_summary": summary}
    if note:
        out["respond_note"] = note
    return out


def _respond(polar: Polar, req: Dict[str, Any], body: Dict[str, Any]) -> None:
    """One «Responder» answer on an open card: the same core call as the Slack button, with the note along."""
    kind, rid, owner = req["kind"], req["id"], polar.owner_id
    note = _note_of(body)
    option, answers, indexes = body.get("option"), body.get("answers"), body.get("indexes")
    given = [x for x in (option, answers, indexes) if x is not None]
    if not given and not note:
        raise ActionError("Escribe una respuesta.", 400)
    if len(given) > 1:
        raise ActionError("Responde con una sola cosa: opción, respuestas o seguimientos.", 400)
    _need(req, ("reply", "decision", "email", "report", "questions"), ("shown", "noted"))
    if option is not None:
        if kind == "decision":
            keys = [o["key"] for o in req["options"]]
            if not isinstance(option, str) or option not in keys:
                raise ActionError("Opción no válida.", 400)
            if is_prod_op(req):
                _gate("prod", "El Go de un paso de producción")
            summary = "eligió %d) %s" % (keys.index(option) + 1, _clip(req["options"][keys.index(option)]["label"]))
        elif kind == "report" and option in ("ok", "resume"):
            if option == "resume" and not (req.get("report") or {}).get("resume"):
                raise ActionError("Opción no válida.", 400)
            summary = "eligió «%s»" % option
        else:
            raise ActionError("Opción no válida.", 400)
        stored = _stored(summary, note)
        polar.on_choose(owner, rid, option, src=SRC, and_continue=bool(body.get("and_continue")), respond=stored)
    elif answers is not None:
        if kind != "questions":
            raise ActionError("Esta tarjeta no tiene preguntas.", 400)
        stored = _answer(polar, req, body, respond=True)
    elif indexes is not None:
        if kind != "report":
            raise ActionError("Esta tarjeta no tiene seguimientos.", 400)
        steps = (req.get("report") or {}).get("next") or []
        if (not isinstance(indexes, list) or not 1 <= len(indexes) <= min(len(steps), db.MAX_REPORT_NEXT)
                or len(set(map(repr, indexes))) != len(indexes)
                or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(steps) for i in indexes)):
            raise ActionError("Seguimiento no válido.", 400)
        summary = "eligió " + ", ".join("%d) %s" % (i + 1, _clip(steps[i])) for i in sorted(indexes))
        stored = _stored(summary, note)
        polar.on_next(owner, rid, src=SRC, indexes=indexes, respond=stored)
    else:
        stored = _stored("pidió cambios", note)
        polar.on_revise_submit(owner, blocks.text_view(note, rid), src=SRC, respond=stored)
    after = db.get(polar.conn, rid) or {}
    if after.get("status") == req["status"]:
        raise ActionError("La tarjeta ya cambió (estado: %s)." % after.get("status"), 409)
    got = after.get("result") or {}
    if got.get("respond_summary") != stored.get("respond_summary") or got.get("respond_note") != stored.get("respond_note"):
        raise _Moved(note)


def _bus_bin() -> str:
    return os.environ.get("AGENT_BUS_BIN") or os.path.join(os.path.expanduser("~"), ".local/bin/agent-bus")


def _session_of(req: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """(session id, pane or tmux name) of the card's requester «<session id> <pane>». A prod-op card names
    «prod-op <op id> <session id> <pane>». ("", None) when there is no session."""
    parts = (req.get("requester") or "").split()
    if parts[:1] == ["prod-op"]:
        parts = parts[2:]
    if not parts or not SESSION_ID.fullmatch(parts[0]):
        return "", None
    return parts[0], parts[1] if len(parts) > 1 else None


def _respond_closed(polar: Polar, req: Dict[str, Any], body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """A note on a card that is not open: Polar tells the requester's session at once, with agent-bus."""
    rid = req["id"]
    try:
        note = _note_of(body)
        if any(body.get(k) is not None for k in ("option", "answers", "indexes")):
            raise ActionError("La tarjeta ya no está abierta (estado: %s)." % req["status"], 409)
        if not note:
            raise ActionError("Escribe una nota.", 400)
        sid, place = _session_of(req)
        if not sid:
            raise ActionError("Esta tarjeta no tiene agente.", 409)
    except ActionError as exc:
        out: Dict[str, Any] = {"ok": False, "error": exc.error}
        if exc.code in (403, 409):
            out["status"] = req["status"]
        return exc.code, out
    text = ("Polar (respuesta): the owner wrote more about your card %s «%s». Nota: %s"
            % (rid, req["title"], note))
    cmd = [sys.executable, _bus_bin(), "deliver", sid, "--text-file", "-", "--from", "polar",
           "--ref", "polar:%s:%s" % (rid, hashlib.sha1(note.encode("utf-8")).hexdigest()[:10])]
    if place and PLACE.fullmatch(place):
        cmd += ["--place", place]
    try:
        done = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=300).returncode == 0
    except (OSError, subprocess.SubprocessError):
        done = False
    if not done:
        log.error("respond: agent-bus did not take the note for %s", rid)
        return 502, {"ok": False, "status": req["status"], "error": "No se pudo avisar al agente."}
    db.add_owner_note(polar.conn, rid, note)
    return 200, {"ok": True, "status": "Entregado al agente."}


def _answer(polar: Polar, req: Dict[str, Any], body: Dict[str, Any], respond: bool = False) -> Optional[Dict[str, Any]]:
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
            _refused(polar.on_q_submit(owner, blocks.text_view(text, json.dumps({"id": rid, "q": qkey}))))
    if note and note.strip():
        _refused(polar.on_q_submit(owner, blocks.text_view(note.strip(), json.dumps({"id": rid, "q": None}))))
    stored = None
    if respond:
        shown = dict(blocks.answers_of(req))
        for qkey, okey, text in plan:
            q = questions[qkey]
            label = next((o["label"] for o in q.get("options", []) if o["key"] == okey), text)
            shown[qkey] = {"text": label}
        stored = _stored("eligió " + ", ".join(
            "%d) %s" % (i, _clip(blocks.answer_text(shown[q["key"]])))
            for i, q in enumerate(req["options"], 1)), (note or "").strip())
    polar.on_q_send(owner, rid, src=SRC, respond=stored)
    return stored


LENGTH_RE = re.compile(r"[0-9]{1,7}")


class CloseOnError(BaseHTTPRequestHandler):
    """Request framing for both local servers, in one place.

    On a kept-alive connection, a body that was not read would be parsed as the next request (request smuggling).
    So: `parse_request` refuses, before any route or auth logic, every request whose body length is not one plain
    Content-Length (no Transfer-Encoding, no second Content-Length, digits only, none on GET or HEAD). Every answer
    with status 400 or more closes the connection. As a backstop, an answer sent before the declared body was read
    with `read_body` closes the connection too, so a new route cannot bring the bug back."""

    body_left = 0  # bytes of the declared body not read yet
    response_started = False  # True once a status line went out for this request

    def parse_request(self) -> bool:
        self.body_left, self.response_started = 0, False
        if not super().parse_request():
            return False
        lengths = self.headers.get_all("Content-Length") or []
        bad = bool(self.headers.get_all("Transfer-Encoding")) or len(lengths) > 1
        if not bad and lengths:
            bad = not LENGTH_RE.fullmatch(lengths[0])
            if not bad:
                self.body_left = int(lengths[0])
                bad = self.command in ("GET", "HEAD") and self.body_left != 0
        if bad:
            self.body_left = 0
            self.send_error(400)
            return False
        return True

    def body_length(self) -> Optional[int]:
        """The declared body length (already checked by `parse_request`), or None without Content-Length."""
        got = self.headers.get("Content-Length")
        return int(got) if got is not None else None

    def read_body(self, length: int) -> bytes:
        data = self.rfile.read(length)
        self.body_left = max(0, self.body_left - len(data))
        return data

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        super().send_response(code, message)
        self.response_started = True
        if code >= 400 or self.body_left:
            self.close_connection = True
            self.send_header("Connection", "close")

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """A short JSON error with no server details. All answers pass `send_response`, so this one closes too."""
        data = json.dumps({"ok": False, "error": "HTTP %d" % code}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD" and code >= 200 and code not in (204, 304):
            self.wfile.write(data)


class _Handler(CloseOnError):
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

    def do_GET(self) -> None:  # framing is checked first, in CloseOnError.parse_request
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
        length = self.body_length()
        if length is None:
            self._json(400, {"ok": False, "error": "Falta Content-Length."})
            return
        if length > MAX_BODY:
            self._json(413, {"ok": False, "error": "Cuerpo demasiado grande."})
            return
        try:
            body = json.loads(self.read_body(length).decode("utf-8"))
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
