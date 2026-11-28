import http.client
import json
import logging
import os
import socket
import stat
import threading

import pytest

from polar import blocks, db, webapi
from tests.conftest import OWNER

TOKEN = "tok-test-123"
QS = [
    {"key": "alerta", "text": "¿Alerta?", "options": [{"key": "si", "label": "Sí"}, {"key": "no", "label": "No"}]},
    {"key": "umbral", "text": "¿Umbral?"},
]


@pytest.fixture
def web(polar, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_ALLOW", "send,prod")
    server = webapi.WebServer(0, polar, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, method="POST", path="/act", body=None, token=TOKEN, host=None, raw=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or "127.0.0.1:%d" % port}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = raw if raw is not None else (json.dumps(body) if body is not None else None)
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, out


def act(server, rid, action, **kw):
    return call(server, body=dict(id=rid, action=action, **kw))


def shown(polar, rid):
    polar.deliver_pending()
    return db.get(polar.conn, rid)


# -- auth and guards
def test_health_needs_token(web):
    assert call(web, "GET", "/health") == (200, {"ok": True, "allow": ["send", "prod"]})
    assert call(web, "GET", "/health", token="nope")[0] == 401
    assert call(web, "GET", "/health", token=None)[0] == 401
    assert call(web, "POST", body={"id": "x", "action": "seen"}, token="")[0] == 401


def test_host_check(web):
    port = web.server_address[1]
    assert call(web, "GET", "/health", host="localhost:%d" % port)[0] == 200
    assert call(web, "GET", "/health", host="evil.example:%d" % port)[0] == 403
    assert call(web, "GET", "/health", host="127.0.0.1:1")[0] == 403
    assert call(web, "GET", "/health", host="127.0.0.1")[0] == 403


def test_body_limit_and_bad_json(web):
    assert call(web, raw="x" * (webapi.MAX_BODY + 1))[0] == 413
    assert call(web, raw="{no")[0] == 400
    assert call(web, raw="[1]")[0] == 400
    assert call(web, "GET", "/nada")[0] == 404


def test_token_file_created_0600_and_reused(tmp_path):
    p = tmp_path / "d" / "web-token"
    t1 = webapi.load_token(p)
    assert len(t1) >= 40 and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert webapi.load_token(p) == t1


def test_token_never_logged(web, caplog):
    caplog.set_level(logging.DEBUG)
    call(web, "GET", "/health", token="bad")
    call(web, "GET", "/health")
    assert TOKEN not in caplog.text


def test_polar_starts_when_port_busy(polar, tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        assert webapi.start(polar, s.getsockname()[1], wait=0) is None
    finally:
        s.close()
    assert webapi.start(polar, 0) is None
    monkeypatch.setenv("POLAR_WEB_PORT", "0")
    assert webapi.start(polar) is None


# -- actions
def test_unknown_and_bad_requests(web, polar, reply_id):
    assert act(web, "nope", "discard")[0] == 409
    assert call(web, body={"action": "discard"})[0] == 400
    shown(polar, reply_id)
    assert act(web, reply_id, "wat")[0] == 400


def test_choose_decision_and_repeat(web, polar, bot, decision_id):
    shown(polar, decision_id)
    assert act(web, decision_id, "choose", option="zzz")[0] == 400
    assert act(web, decision_id, "choose", option="go") == (200, {"ok": True, "status": "chosen"})
    req = db.get(polar.conn, decision_id)
    assert req["result"] == {"option": "go", "src": "web"}
    assert "desde la web" in bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]
    code, out = act(web, decision_id, "choose", option="wait")
    assert code == 409 and out["ok"] is False and out["status"] == "chosen"
    assert db.get(polar.conn, decision_id)["result"]["option"] == "go"
    assert len(bot.of("chat_update")) == 1


def test_choose_wrong_kind(web, polar, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "choose", option="go")[0] == 409


def test_prod_op_go_is_single_card_go(web, polar, bot, conn):
    rid = db.create_request(conn, "decision", "Flag", "c", "prod-op abc",
                            options=[{"key": "go", "label": "Go"}, {"key": "no", "label": "No"}])
    shown(polar, rid)
    assert act(web, rid, "choose", option="go")[0] == 200
    assert db.get(conn, rid)["result"] == {"option": "go", "src": "web"}


def test_send_and_prod_go_are_off_by_default(web, polar, user, conn, reply_id, monkeypatch):
    monkeypatch.delenv("POLAR_WEB_ALLOW")
    assert call(web, "GET", "/health") == (200, {"ok": True, "allow": []})
    shown(polar, reply_id)
    code, out = act(web, reply_id, "send")
    assert code == 403 and out["status"] == "shown" and "Slack" in out["error"]
    assert user.of("chat_postMessage") == []
    rid = db.create_request(conn, "decision", "Flag", "c", "prod-op abc",
                            options=[{"key": "go", "label": "Go"}, {"key": "no", "label": "No"}])
    shown(polar, rid)
    assert act(web, rid, "choose", option="go")[0] == 403
    assert db.get(conn, rid)["status"] == "shown"
    monkeypatch.setenv("POLAR_WEB_ALLOW", "send")
    assert act(web, rid, "choose", option="go")[0] == 403
    assert act(web, reply_id, "send")[0] == 200


def test_send_posts_as_owner_and_marks_web(web, polar, user, bot, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "send") == (200, {"ok": True, "status": "sent"})
    post = user.of("chat_postMessage")[0]
    assert post["channel"] == "C1" and post["markdown_text"] == "Hola Ana" and post["thread_ts"] == "55.5"
    req = db.get(polar.conn, reply_id)
    assert req["result"]["src"] == "web" and req["result"]["ts"]
    assert "desde la web" in bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]
    code, out = act(web, reply_id, "send")
    assert code == 409 and out["status"] == "sent"
    assert len(user.of("chat_postMessage")) == 1


def test_send_edited_text(web, polar, user, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "send", text="  Hola Ana, editado ")[0] == 200
    assert user.of("chat_postMessage")[0]["markdown_text"] == "Hola Ana, editado"
    assert db.get(polar.conn, reply_id)["final_text"] == "Hola Ana, editado"


def test_send_bad_text_changes_nothing(web, polar, user, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "send", text="  ")[0] == 400
    assert act(web, reply_id, "send", text="x" * (db.MAX_REPLY_CHARS + 1))[0] == 400
    assert db.get(polar.conn, reply_id)["status"] == "shown" and not user.of("chat_postMessage")


def test_send_failure_is_reported(web, polar, user, bot, reply_id):
    shown(polar, reply_id)
    user.fail = "chat_postMessage"
    code, out = act(web, reply_id, "send")
    assert code == 502 and out == {"ok": False, "status": "failed", "error": out["error"]}
    assert "xoxp" not in json.dumps(out)


def test_discard(web, polar, bot, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "discard") == (200, {"ok": True, "status": "discarded"})
    assert "desde la web" in bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]
    code, out = act(web, reply_id, "discard")
    assert code == 409 and out["status"] == "discarded"


def test_discard_decision_refused(web, polar, decision_id):
    shown(polar, decision_id)
    assert act(web, decision_id, "discard")[0] == 409


def test_revise_reply_and_repeat(web, polar, bot, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "revise", text="")[0] == 400
    assert act(web, reply_id, "revise", text="Más corto") == (200, {"ok": True, "status": "revise"})
    res = db.get(polar.conn, reply_id)["result"]
    assert res == {"instructions": "Más corto", "src": "web"}
    assert "desde la web" in bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]
    assert act(web, reply_id, "revise", text="Otro")[0] == 409


def test_report_next_revise_seen(web, polar, bot, conn):
    rid = db.create_request(conn, "report", "R", "", "s1", draft_text="x", expires_hours=72,
                            report=dict(next=["Sigue con X", "Otra"]))
    shown(polar, rid)
    assert act(web, rid, "seen") == (200, {"ok": True, "status": "noted"})
    assert act(web, rid, "next", index=5)[0] == 400
    assert act(web, rid, "next", index=True)[0] == 400
    assert act(web, rid, "next", index=0) == (200, {"ok": True, "status": "revise"})
    res = db.get(conn, rid)["result"]
    assert res == {"instructions": "Sigue con X", "via": "next", "indexes": [0], "steps": ["Sigue con X"],
                   "src": "web"}
    assert act(web, rid, "next", index=1)[0] == 409


def test_report_revise_and_next_wrong_kind(web, polar, conn, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "next", index=0)[0] == 409
    rid = db.create_request(conn, "report", "R", "", "s1", draft_text="x", expires_hours=72, report={})
    shown(polar, rid)
    assert act(web, rid, "revise", text="cambia") == (200, {"ok": True, "status": "revise"})


def test_answer_questions(web, polar, bot, conn):
    rid = db.create_request(conn, "questions", "Q", "c", "s", options=QS)
    shown(polar, rid)
    assert act(web, rid, "answer")[0] == 400
    assert act(web, rid, "answer", answers={"alerta": "si"})[0] == 400      # one question missing
    assert act(web, rid, "answer", answers={"zzz": "si", "umbral": "x"})[0] == 400
    assert db.get(conn, rid)["status"] == "shown" and not db.get(conn, rid)["result"]
    code, out = act(web, rid, "answer", answers={"alerta": "si", "umbral": "Cinco"}, note="ojo")
    assert (code, out) == (200, {"ok": True, "status": "answered"})
    res = db.get(conn, rid)["result"]
    assert res["answers"] == {"alerta": {"option": "si", "text": "Sí"}, "umbral": {"option": None, "text": "Cinco"}}
    assert res["note"] == "ojo" and res["src"] == "web"
    assert "desde la web" in bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]
    code, out = act(web, rid, "answer", answers={"alerta": "no", "umbral": "Seis"})
    assert code == 409 and out["status"] == "answered"
    assert db.get(conn, rid)["result"]["answers"]["umbral"]["text"] == "Cinco"


def test_answer_uses_earlier_slack_answers(web, polar, conn):
    rid = db.create_request(conn, "questions", "Q", "c", "s", options=QS)
    shown(polar, rid)
    polar.on_q_answer(OWNER, rid, "alerta", "no")
    assert act(web, rid, "answer", answers={"umbral": "Tres"})[0] == 200
    assert db.get(conn, rid)["result"]["answers"]["alerta"]["option"] == "no"


def test_answer_wrong_kind(web, polar, decision_id):
    shown(polar, decision_id)
    assert act(web, decision_id, "answer", answers={"a": "b"})[0] == 409


def test_action_on_unshown_card(web, polar, reply_id):
    assert act(web, reply_id, "send")[0] == 409  # still pending: no DM yet


# -- report link button
def test_report_web_button(conn, monkeypatch):
    rid = db.create_request(conn, "report", "R", "", "s1", draft_text="x", expires_hours=72,
                            report=dict(links=["PR=https://a.b/1"], next=["Sigue"]))
    req = db.get(conn, rid)
    monkeypatch.delenv("POLAR_WEB_URL", raising=False)
    before = blocks.report_buttons(req)
    assert not any("Informe completo" in json.dumps(b, ensure_ascii=False) for b in before)
    monkeypatch.setenv("POLAR_WEB_URL", "https://polar-web.example.com/")
    after = blocks.report_buttons(req)
    link = [b for b in after if b["action_id"] == "polar_link:web"][0]
    assert link["url"] == "https://polar-web.example.com/informes/" + rid
    assert link["text"]["text"] == "📄 Informe completo"
    assert [b for b in after if b["action_id"] != "polar_link:web"] == before


def test_result_line_web_suffix():
    req = {"status": "discarded", "kind": "reply", "title": "t", "result": {"src": "web"}}
    assert blocks.result_line(req) == "🗑️ Descartado · desde la web"
    assert blocks.result_line(dict(req, result={})) == "🗑️ Descartado"


def test_send_email_marks_web(web, polar, bot, conn, monkeypatch):
    from polar import gmail
    monkeypatch.setattr(gmail, "send_reply", lambda *a, **k: {"id": "m1"})
    polar._gmail = object()
    rid = db.create_request(conn, "email", "Correo", "c", "s", draft_text="Hola", target_thread_ts="th1")
    shown(polar, rid)
    assert act(web, rid, "send")[0] == 200
    assert db.get(conn, rid)["result"] == {"id": "m1", "src": "web"}


def test_new_task_from_web(web, polar, bot, conn):
    st, out = call(web, body={"action": "task", "text": "Revisa el drift de staging\ny dime qué falta"})
    assert st == 200 and out["ok"] and out["task"]
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (out["task"],)).fetchone()
    assert row["text"].startswith("Revisa el drift") and row["msg_ts"].startswith("web:")
    assert call(web, body={"action": "task", "text": "  "})[0] == 400
    assert call(web, body={"action": "task"})[0] == 400


def test_calendar_not_connected_and_shape(web, polar):
    from polar import gcal
    st, out = call(web, body={"action": "cal_today"})
    assert st == 200 and out["connected"] is False
    assert call(web, body={"action": "cal_auth_finish", "pasted": "x"})[0] == 400
    ev = gcal.shape([{"summary": "1:1", "start": {"dateTime": "2026-11-23T10:00:00-06:00"}, "end": {"dateTime": "2026-11-23T10:30:00-06:00"}},
                     {"summary": "No", "status": "cancelled", "start": {}},
                     {"summary": "Rechacé", "attendees": [{"self": True, "responseStatus": "declined"}], "start": {}},
                     {"summary": "Feriado", "start": {"date": "2026-11-23"}, "end": {"date": "2026-11-24"}}])
    assert [e["title"] for e in ev] == ["1:1", "Feriado"] and ev[1]["all_day"]


def test_busy_port_at_restart_comes_up_when_it_frees(polar, tmp_path, monkeypatch):
    """A restart starts the new Polar while the old one holds the port: the API waits, it does not stay off."""
    import time
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(webapi, "late_server", None)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    assert webapi.start(polar, port, token="t" * 40, wait=1.5) is None  # Polar goes on without it
    s.close()
    end = time.monotonic() + 3
    while webapi.late_server is None and time.monotonic() < end:
        time.sleep(0.05)
    srv = webapi.late_server
    assert srv is not None and srv.server_address[1] == port
    srv.shutdown()
    srv.server_close()


def test_report_next_indexes(web, polar, conn):
    rid = db.create_request(conn, "report", "R", "", "s1", draft_text="x", expires_hours=72,
                            report=dict(next=["A", "B", "C"]))
    shown(polar, rid)
    for bad in ([], [0, 0], [3], ["0"], [True], "0", [0, 1, 2, 1]):
        assert act(web, rid, "next", indexes=bad)[0] == 400
    assert act(web, rid, "next", indexes=[2, 0]) == (200, {"ok": True, "status": "revise"})
    res = db.get(conn, rid)["result"]
    assert res["instructions"] == "1) A 2) C" and res["indexes"] == [0, 2] and res["steps"] == ["A", "C"]


def _raw(port, data):
    """Send raw bytes; return (everything the server wrote, True when the server closed the socket)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.sendall(data)
    out, closed = b"", False
    while True:
        try:
            got = s.recv(65536)
        except socket.timeout:
            break
        if not got:
            closed = True
            break
        out += got
    s.close()
    return out, closed


def test_error_response_closes_the_connection(web):
    port = web.server_address[1]
    inner = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n\r\n" % (port, TOKEN.encode())
    req = (b"POST /act HTTP/1.1\r\nHost: evil.example\r\nContent-Type: application/json\r\n"
           b"Content-Length: %d\r\n\r\n" % len(inner)) + inner
    out, closed = _raw(port, req)
    assert out.count(b"HTTP/1.1 ") == 1 and out.startswith(b"HTTP/1.1 403") and closed
    assert b"Connection: close" in out


def _framed(web, first):
    """`first` is a request head with "@@" for the host and "##" for the inner length; a second request follows."""
    port = web.server_address[1]
    inner = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n\r\n" % (port, TOKEN.encode())
    head = first.replace(b"@@", b"127.0.0.1:%d\r\nAuthorization: Bearer %s" % (port, TOKEN.encode()))
    return _raw(port, head.replace(b"##", b"%d" % len(inner)) + inner)


@pytest.mark.parametrize("first", [
    b"POST /act HTTP/1.1\r\nHost: @@\r\nTransfer-Encoding: chunked\r\nContent-Length: ##\r\n\r\n",  # (a)
    b"GET /health HTTP/1.1\r\nHost: @@\r\nTransfer-Encoding: chunked\r\n\r\n",  # (a)
    b"GET /health HTTP/1.1\r\nHost: @@\r\nContent-Length: 0\r\nContent-Length: ##\r\n\r\n",  # (b)
    b"POST /act HTTP/1.1\r\nHost: @@\r\nContent-Length: 3_2\r\n\r\n",  # (c)
    b"POST /act HTTP/1.1\r\nHost: @@\r\nContent-Length: +32\r\n\r\n",  # (c)
    b"POST /act HTTP/1.1\r\nHost: @@\r\nContent-Length: 32 \r\n\r\n",  # (c)
    b"POST /act HTTP/1.1\r\nHost: @@\r\nContent-Length: -1\r\n\r\n",  # (c)
    b"GET /health HTTP/1.1\r\nHost: @@\r\nContent-Length: ##\r\n\r\n",  # (d)
    b"HEAD /health HTTP/1.1\r\nHost: @@\r\nContent-Length: ##\r\n\r\n",  # (d)
])
def test_bad_framing_is_refused_and_closes(web, first):
    out, closed = _framed(web, first)
    assert out.count(b"HTTP/1.1 ") == 1 and out.startswith(b"HTTP/1.1 400") and closed


def test_duplicate_length_on_a_good_post_is_refused(web, polar, conn):
    rid = db.create_request(conn, "decision", "¿Sí?", "", "s1", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    shown(polar, rid)
    port = web.server_address[1]
    body = json.dumps({"id": rid, "action": "choose", "option": "a"}).encode()
    inner = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\n\r\n" % (port, TOKEN.encode())
    req = (b"POST /act HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n"
           b"Content-Length: %d\r\nContent-Length: %d\r\n\r\n" % (port, TOKEN.encode(), len(body), len(body) + len(inner)))
    out, closed = _raw(port, req + body + inner)
    assert out.count(b"HTTP/1.1 ") == 1 and out.startswith(b"HTTP/1.1 400") and closed
    assert db.get(conn, rid)["status"] == "shown"


def test_answer_before_the_body_is_read_closes(web, monkeypatch):
    monkeypatch.setattr(webapi._Handler, "do_POST", lambda self: self._json(200, {"ok": True}))
    out, closed = _framed(web, b"POST /act HTTP/1.1\r\nHost: @@\r\nContent-Length: ##\r\n\r\n")
    assert out.count(b"HTTP/1.1 ") == 1 and out.startswith(b"HTTP/1.1 200") and closed
