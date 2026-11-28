"""The web UI: request checks, pages, actions through webapi.perform, files and the demo seed."""
import http.client
import json
import logging
import os
import re
import socket
import threading
from pathlib import Path

import pytest

from polar import db
from polar.web import demo, server, strings, views

OWNER = "U0000000000"


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    (d / "files").mkdir(parents=True)
    monkeypatch.setenv("POLAR_DATA_DIR", str(d))
    for name in ("POLAR_UI_OWNER", "POLAR_UI_HOSTS", "POLAR_UI_OWNER_HEADER", "POLAR_UI_LANG", "POLAR_WEB_ALLOW",
                 "POLAR_UI_PORT", "POLAR_TZ"):
        monkeypatch.delenv(name, raising=False)
    strings.set_lang(None)
    return d


@pytest.fixture
def ui(data, polar):
    srv = server.UIServer(0, polar, data_dir=data)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def session_cookie(srv):
    """The Cookie header of a logged-in browser (what GET /login sets)."""
    return "%s=%s" % (server.COOKIE, srv.session)


def call(srv, method, path, body=None, headers=None, host=None, login=True):
    """One request. `login=True` sends the session cookie, as a browser that opened the login link."""
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = {"Host": host or "127.0.0.1:%d" % srv.port}
    if login:
        h["Cookie"] = session_cookie(srv)
    h.update(headers or {})
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.status, r.read(), dict(r.getheaders())
    c.close()
    return out


def post(srv, body, **kw):
    h = {"Origin": "http://127.0.0.1:%d" % srv.port, "X-CSRF-Token": srv.csrf(), "Content-Type": "application/json"}
    h.update(kw.pop("headers", {}))
    code, raw, _ = call(srv, "POST", "/api/act", body, headers=h, **kw)
    return code, json.loads(raw.decode() or "{}")


def show(conn, rid):
    db.transition(conn, rid, "pending", "shown")


def report(conn, title="Weekly report", body="All good.", **rep):
    rid = db.create_request(conn, "report", title, "ctx", "agent-1", draft_text=body, report=rep or None)
    db.transition(conn, rid, "pending", "noted")
    return rid


# -- request checks ------------------------------------------------------------------------------------------------------
def test_page_has_security_headers(ui):
    code, raw, h = call(ui, "GET", "/")
    assert code == 200
    assert "script-src 'self'" in h["Content-Security-Policy"] and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "unsafe-inline" not in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff" and h["Cache-Control"] == "no-store"
    assert h["Referrer-Policy"] == "no-referrer"
    assert b"<script>" not in raw and b' style="' not in raw  # no inline script and no inline style
    assert b'<html lang="en">' in raw and b">Polar</a>" in raw


def test_bad_host_is_refused(ui):
    assert call(ui, "GET", "/", host="evil.example:%d" % ui.port)[0] == 403
    assert call(ui, "GET", "/", host="localhost:%d" % ui.port)[0] == 200
    assert call(ui, "GET", "/", host="127.0.0.1:1")[0] == 403


def test_extra_hosts(data, polar, monkeypatch):
    monkeypatch.setenv("POLAR_UI_HOSTS", "polar.example.net, Other.Example")
    srv = server.UIServer(0, polar, data_dir=data)
    assert "polar.example.net" in srv.hosts and "other.example" in srv.hosts
    assert "https://polar.example.net" in srv.origins


def test_owner_header(data, polar, monkeypatch):
    monkeypatch.setenv("POLAR_UI_OWNER", "owner@example.com")
    srv = server.UIServer(0, polar, data_dir=data)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(srv, "GET", "/")[0] == 403
        assert call(srv, "GET", "/", headers={"Tailscale-User-Login": "other@example.com"})[0] == 403
        assert call(srv, "GET", "/", headers={"Tailscale-User-Login": "owner@example.com"})[0] == 200
        monkeypatch.setenv("POLAR_UI_OWNER_HEADER", "X-Remote-User")
    finally:
        srv.shutdown()
        srv.server_close()
    srv2 = server.UIServer(0, polar, data_dir=data)
    assert srv2.owner_header == "X-Remote-User"


def test_post_needs_origin_and_csrf(ui, conn, decision_id):
    show(conn, decision_id)
    body = {"id": decision_id, "action": "choose", "option": "go"}
    assert post(ui, body, headers={"Origin": "http://evil.example"})[0] == 403
    assert post(ui, body, headers={"Origin": ""})[0] == 403
    assert post(ui, body, headers={"X-CSRF-Token": ""})[0] == 403
    assert post(ui, body, headers={"X-CSRF-Token": "0" * 64})[0] == 403
    assert db.get(conn, decision_id)["status"] == "shown"
    assert post(ui, body)[0] == 200


def test_ui_reuses_the_web_api_secret(ui, data):
    f = data / "web-token"
    assert f.exists() and (f.stat().st_mode & 0o777) == 0o600
    assert not (data / "web-ui-secret").exists()  # one secret file for the API and the UI
    secret = f.read_text().strip()
    assert ui.login == server.login_token(secret) and ui.session == server.session_value(secret)
    assert ui.csrf() in call(ui, "GET", "/")[1].decode()


# -- login ---------------------------------------------------------------------------------------------------------------
def test_no_session_gets_401_everywhere(ui, conn, decision_id, data):
    show(conn, decision_id)
    f = data / "files" / "chart.png"
    f.write_bytes(demo.chart_png(8, 8))
    rid = report(conn, files=[str(f)])
    for path in ("/", "/history", "/reports", "/reports/" + rid, "/?c=" + decision_id, "/files/%s/0" % rid, "/nope"):
        code, raw, h = call(ui, "GET", path, login=False)
        page = raw.decode()
        assert code == 401 and h["Content-Type"].startswith("text/html"), path
        assert "polar ui-url" in page and strings.t("login_need") in page
        assert ui.csrf() not in page and "csrf" not in page and "Release" not in page and decision_id not in page
        assert "PNG" not in page and h["Connection"] == "close"
    code, raw, h = call(ui, "GET", "/api/state", login=False)
    assert code == 401 and json.loads(raw) == {"ok": False, "error": strings.t("err_login")}
    body = {"id": decision_id, "action": "choose", "option": "go"}
    code, out = post(ui, body, headers={"Cookie": ""})
    assert code == 401 and out["error"] == strings.t("err_login")
    code, out = post(ui, body, headers={"Cookie": "%s=%s" % (server.COOKIE, "0" * 64)})
    assert code == 401 and db.get(conn, decision_id)["status"] == "shown"
    assert call(ui, "GET", "/", headers={"Cookie": "%s=wrong" % server.COOKIE}, login=False)[0] == 401
    assert call(ui, "GET", "/healthz", login=False)[0] == 200
    assert call(ui, "GET", "/static/app.css", login=False)[0] == 200


def test_401_page_follows_the_language(ui, monkeypatch):
    monkeypatch.setenv("POLAR_UI_LANG", "es")
    raw = call(ui, "GET", "/", login=False)[1].decode()
    assert '<html lang="es">' in raw and strings.t("login_need") in raw and "Abre tu enlace" in raw


def test_login_link(ui):
    for q in ("", "?token=", "?token=" + "0" * 64, "?token=" + ui.session):
        code, raw, h = call(ui, "GET", "/login" + q, login=False)
        assert code == 403 and "Set-Cookie" not in h and h["Connection"] == "close"
        assert strings.t("login_bad") in raw.decode() and ui.login not in raw.decode()
    assert call(ui, "GET", "/login?token=" + ui.login, login=False, host="evil.example")[0] == 403
    code, _, h = call(ui, "GET", "/login?token=" + ui.login, login=False)
    assert code == 303 and h["Location"] == "/"
    cookie = h["Set-Cookie"]
    assert cookie.startswith("%s=%s;" % (server.COOKIE, ui.session))
    for part in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=%d" % server.COOKIE_MAX_AGE):
        assert part in cookie
    assert 150 * 86400 < server.COOKIE_MAX_AGE < 200 * 86400
    jar = cookie.split(";")[0]
    code, raw, _ = call(ui, "GET", "/", headers={"Cookie": "other=1; " + jar}, login=False)
    assert code == 200 and ui.csrf() in raw.decode()


def test_login_url_matches_the_server(ui, data):
    assert ui.login_url == server.login_url(ui.port, data) == "http://127.0.0.1:%d/login?token=%s" % (ui.port, ui.login)


def test_csrf_and_session_do_not_cross_secrets(data, polar, tmp_path, conn, decision_id):
    show(conn, decision_id)
    other = tmp_path / "other"
    other.mkdir()
    a = server.UIServer(0, polar, data_dir=data)
    b = server.UIServer(0, polar, data_dir=other)
    for s_ in (a, b):
        threading.Thread(target=s_.serve_forever, daemon=True).start()
    try:
        assert a.secret != b.secret and a.csrf() != b.csrf() and a.session != b.session and a.login != b.login
        body = {"id": decision_id, "action": "choose", "option": "go"}
        assert post(b, body, headers={"X-CSRF-Token": a.csrf()})[0] == 403  # CSRF token of another secret
        assert post(b, body, headers={"Cookie": session_cookie(a)})[0] == 401  # session of another secret
        assert call(b, "GET", "/login?token=" + a.login, login=False)[0] == 403
        assert db.get(conn, decision_id)["status"] == "shown"
        assert post(b, body)[0] == 200
    finally:
        for s_ in (a, b):
            s_.shutdown()
            s_.server_close()


def test_ui_url_command(data, monkeypatch, capsys):
    from polar import cli
    assert cli.main(["ui-url"]) == 2 and "POLAR_UI_PORT" in capsys.readouterr().out
    monkeypatch.setenv("POLAR_UI_PORT", "8795")
    assert cli.main(["ui-url"]) == 0
    out = capsys.readouterr().out.strip()
    secret = (data / "web-token").read_text().strip()
    assert out == "http://127.0.0.1:8795/login?token=" + server.login_token(secret)


def test_demo_prints_the_login_link(tmp_path, monkeypatch, capsys):
    for k in ["POLAR_DATA_DIR"] + list(demo.DEMO_ENV):
        monkeypatch.setenv(k, os.environ.get(k, ""))
    seen = {}

    def stop(self):
        seen["url"] = self.login_url
        seen["secret"] = (self.data_dir / "web-token").exists()
        raise KeyboardInterrupt
    monkeypatch.setattr(server.UIServer, "serve_forever", stop)
    assert demo.run(port=0) == 0
    out = capsys.readouterr().out
    assert seen["secret"] and seen["url"] in out and "/login?token=" in out


def raw(port, data, timeout=2.0):
    """Send raw bytes; return (everything the server wrote, True when the server closed the socket)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
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


@pytest.mark.parametrize("why", ["host", "origin", "csrf", "path", "login"])
def test_rejected_post_closes_the_connection(ui, why):
    inner = b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % ui.port
    host = b"evil.example" if why == "host" else b"127.0.0.1:%d" % ui.port
    origin = b"http://evil.example" if why == "origin" else b"http://127.0.0.1:%d" % ui.port
    csrf = b"" if why == "csrf" else ui.csrf().encode()
    path = b"/nope" if why == "path" else b"/api/act"
    cookie = b"" if why == "login" else session_cookie(ui).encode()
    req = (b"POST %s HTTP/1.1\r\nHost: %s\r\nOrigin: %s\r\nX-CSRF-Token: %s\r\nCookie: %s\r\n"
           b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
           % (path, host, origin, csrf, cookie, len(inner))) + inner
    out, closed = raw(ui.port, req)
    assert out.count(b"HTTP/1.1 ") == 1, out[:400]
    assert closed and b"Connection: close" in out and b"<article" not in out


def test_get_with_body_closes_the_connection(ui):
    inner = b"GET /history HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % ui.port
    req = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: %d\r\n\r\n" % (ui.port, len(inner)) + inner
    out, closed = raw(ui.port, req)
    assert out.count(b"HTTP/1.1 ") == 1 and closed


@pytest.mark.parametrize("req", [b"PUT / HTTP/1.1\r\nHost: @@\r\nContent-Length: 0\r\n\r\n",
                                 b"BREW\r\n\r\n", b"GET / HTTP/9.9\r\nHost: @@\r\n\r\n"])
def test_http_errors_are_plain_and_safe(ui, req):
    out, closed = raw(ui.port, req.replace(b"@@", b"127.0.0.1:%d" % ui.port))
    head, _, body = out.partition(b"\r\n\r\n")
    assert out.count(b"HTTP/1.1 ") == 1 and closed
    assert b"Server: polar-ui\r\n" in head and b"Python" not in out
    assert b"Content-Security-Policy: default-src 'none'" in head and b"X-Content-Type-Options: nosniff" in head
    assert b"Connection: close" in head and b"<" not in body and len(body) < 60


def test_idle_keepalive_times_out_quietly(ui, monkeypatch, capsys):
    monkeypatch.setattr(server.Handler, "timeout", 0.3)
    s = socket.create_connection(("127.0.0.1", ui.port), timeout=3)
    s.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % ui.port)
    out = b""
    while True:  # the server closes the idle connection after the timeout
        got = s.recv(65536)
        if not got:
            break
        out += got
    s.close()
    assert out.startswith(b"HTTP/1.1 200") and out.count(b"HTTP/1.1 ") == 1
    assert "Traceback" not in capsys.readouterr().err


def test_handler_timeout_is_set():
    assert server.Handler.timeout == 30


def test_body_limit_and_bad_json(ui):
    assert post(ui, b"x" * (server.MAX_BODY + 1))[0] == 413
    assert post(ui, b"{not json")[0] == 400
    assert post(ui, {"id": "abc", "action": "explode"})[0] == 400


# -- cards ---------------------------------------------------------------------------------------------------------------
def test_each_kind_renders_its_actions(conn, monkeypatch):
    monkeypatch.delenv("POLAR_UI_LANG", raising=False)
    rid = db.create_request(conn, "decision", "Pick", "c", "s", options=[
        {"key": "a", "label": "Alpha (recommended)"}, {"key": "b", "label": "Beta"}])
    h = views.card_html(db.get(conn, rid), True)
    assert 'data-option="a"' in h and 'data-option="b"' in h and 'class="rec"' in h and 'data-toggle="revise"' in h
    assert "(recommended)" not in h  # the mark replaces the label text
    qid = db.create_request(conn, "questions", "Q", "c", "s", options=[
        {"key": "one", "text": "First?", "recommended": "y", "options": [{"key": "y", "label": "Yes"},
                                                                         {"key": "n", "label": "No"}]},
        {"key": "two", "text": "Free?", "options": []}])
    h = views.card_html(db.get(conn, qid), True)
    assert 'type="radio"' in h and 'value="y" checked' in h and 'data-action="answer"' in h and 'name="note"' in h
    eid = db.create_request(conn, "email", "Mail", "c", "s", draft_text="Hello", target_thread_ts="th1")
    db.set_original(conn, eid, orig_name="Kim", orig_text="Original words", email_subject="Hi there")
    h = views.card_html(db.get(conn, eid), True)
    assert 'data-action="send"' in h and 'data-action="discard"' in h and "Original words" in h and "Hi there" in h
    h = views.card_html(db.get(conn, eid), False)
    assert 'data-action="send"' not in h and strings.t("send_off") in h
    rep = report(conn, next=["Do the next thing"])
    h = views.card_html(db.get(conn, rep), True)
    assert 'data-action="seen"' in h and 'data-pick="0"' in h and 'data-action="next"' in h


def test_closed_card_has_no_buttons(conn, decision_id):
    show(conn, decision_id)
    db.transition(conn, decision_id, "shown", "chosen", result_json=json.dumps({"option": "go"}),
                  decided_at=db.now_iso())
    h = views.card_html(db.get(conn, decision_id), True)
    assert "data-action" not in h and "<button" not in h and "Chose: Go" in h


def test_escaping(conn):
    bad = '<script>alert(1)</script><agent-message from="x">"q"</agent-message>'
    rid = db.create_request(conn, "decision", bad, bad, "s", requester_label=bad,
                            options=[{"key": "a", "label": bad}, {"key": "b", "label": "B"}],
                            extra={"body": bad + " [x](javascript:alert(1))"})
    h = views.card_html(db.get(conn, rid), True)
    assert "<script>" not in h and "<agent-message" not in h and "javascript:" not in h
    assert "&lt;script&gt;" in h


def test_markdown_is_safe():
    out = views.md('<img src=x onerror=alert(1)> **bold** [ok](https://example.com) [no](javascript:x) '
                   '![pic](https://example.com/p.png) &copy;')
    assert "<img" not in out and "&lt;img src=x onerror=alert(1)&gt;" in out
    assert "<strong>bold</strong>" in out and 'href="https://example.com"' in out and 'href="#"' in out
    assert "&amp;copy;" in out


def test_strings_tables_match_and_used_keys_exist():
    assert set(strings.EN) == set(strings.ES)
    src = ""
    for f in Path(views.__file__).parent.glob("*.py"):
        src += f.read_text(encoding="utf-8")
    used = set(re.findall(r'\bt\("([a-z0-9_]+)"[,)]', src))
    used |= {"kind_" + k for k in db.KINDS} | {"st_" + s for s in db.FINAL_STATUSES}
    missing = used - set(strings.EN)
    assert not missing, missing
    js = (Path(views.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    js_used = set(re.findall(r"S\.(js_[a-z_]+)", js)) | set(re.findall(r'"(js_[a-z_]+)"', js))
    assert js_used <= set(strings.JS_KEYS), js_used - set(strings.JS_KEYS)


def test_language_and_html_lang(ui, monkeypatch):
    monkeypatch.setenv("POLAR_UI_LANG", "es")
    raw = call(ui, "GET", "/")[1].decode()
    assert '<html lang="es">' in raw and "Pendientes" in raw and "Historial" in raw
    strings.set_lang("en")
    try:
        assert '<html lang="en">' in call(ui, "GET", "/")[1].decode()
    finally:
        strings.set_lang(None)


def test_dates_follow_tz_and_language(monkeypatch):
    monkeypatch.setenv("POLAR_TZ", "America/New_York")
    strings.set_lang("es")
    try:
        assert views.when("2026-03-05T15:04:00Z") == "5 mar, 10:04"
    finally:
        strings.set_lang(None)
    assert views.when("2026-03-05T15:04:00Z") == "Mar 5, 10:04"


# -- pages ---------------------------------------------------------------------------------------------------------------
def test_pending_lists_open_cards_and_count(ui, conn, decision_id, reply_id):
    show(conn, decision_id)
    show(conn, reply_id)
    rep = report(conn, title="Read me")
    raw = call(ui, "GET", "/")[1].decode()
    assert raw.index("c-" + decision_id) < raw.index("c-" + rep)  # action cards before reports
    assert 'data-count>3<' in raw


def test_empty_state(ui):
    raw = call(ui, "GET", "/")[1].decode()
    assert strings.t("empty") in raw


def test_focus_card_first_and_closed(ui, conn, decision_id, reply_id):
    show(conn, decision_id)
    show(conn, reply_id)
    raw = call(ui, "GET", "/?c=" + reply_id)[1].decode()
    assert raw.index("c-" + reply_id) < raw.index("c-" + decision_id) and "card k-reply focus" in raw
    db.transition(conn, decision_id, "shown", "expired", decided_at=db.now_iso())
    raw = call(ui, "GET", "/?c=" + decision_id)[1].decode()
    assert strings.t("closed_note") in raw and 'data-option="go"' not in raw


def test_report_page_and_alias(ui, conn):
    rid = report(conn, title="Release summary", body="## Done\n\nAll **good**.")
    for prefix in ("/reports/", "/informes/"):
        code, raw, _ = call(ui, "GET", prefix + rid)
        assert code == 200 and b"<strong>good</strong>" in raw and b"data-stay" in raw
    assert call(ui, "GET", "/reports/nope")[0] == 404
    assert call(ui, "GET", "/informes")[0] == 200


def test_reports_list_filter_and_paging(ui, conn, monkeypatch):
    monkeypatch.setattr(views, "PAGE_SIZE", 2)
    for i in range(3):
        report(conn, title="Disk report %d" % i)
    report(conn, title="Coverage 100%_done")
    raw = call(ui, "GET", "/reports")[1].decode()
    assert raw.count('class="row"') == 2 and 'rel="next"' in raw
    raw = call(ui, "GET", "/reports?page=1")[1].decode()
    assert raw.count('class="row"') == 2 and 'rel="prev"' in raw
    raw = call(ui, "GET", "/reports?q=Disk")[1].decode()
    assert "Coverage" not in raw and "Disk report" in raw
    raw = call(ui, "GET", "/reports?q=100%25_")[1].decode()  # % and _ are literal
    assert "Coverage" in raw and "Disk report" not in raw


def test_history_groups_and_pages(ui, conn, monkeypatch):
    monkeypatch.setattr(views, "PAGE_SIZE", 2)
    ids = []
    for i in range(3):
        rid = db.create_request(conn, "decision", "Old %d" % i, "c", "s",
                                options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
        show(conn, rid)
        db.transition(conn, rid, "shown", "chosen", result_json='{"option": "a"}',
                      decided_at="2026-01-0%dT12:00:00Z" % (i + 1))
        ids.append(rid)
    raw = call(ui, "GET", "/history")[1].decode()
    assert raw.count('class="row"') == 2 and raw.index("Old 2") < raw.index("Old 1") and 'class="day"' in raw
    assert "/?c=" + ids[2] in raw and "Chose: A" in raw
    raw = call(ui, "GET", "/history?page=1")[1].decode()
    assert "Old 0" in raw and "Old 2" not in raw


def test_state_and_health(ui, conn, decision_id):
    code, raw, _ = call(ui, "GET", "/api/state")
    v1 = json.loads(raw)
    assert code == 200 and v1["waiting"] == 1
    show(conn, decision_id)
    db.transition(conn, decision_id, "shown", "discarded", decided_at=db.now_iso())
    v2 = json.loads(call(ui, "GET", "/api/state")[1])
    assert v2["v"] != v1["v"] and v2["waiting"] == 0
    assert json.loads(call(ui, "GET", "/healthz")[1]) == {"ok": True}
    code, raw, h = call(ui, "GET", "/static/app.js")
    assert code == 200 and h["Content-Type"].startswith("text/javascript")
    assert call(ui, "GET", "/static/../server.py")[0] == 404


# -- actions -------------------------------------------------------------------------------------------------------------
def test_choose_moves_card(ui, conn, decision_id):
    show(conn, decision_id)
    code, out = post(ui, {"id": decision_id, "action": "choose", "option": "wait"})
    assert code == 200 and out["ok"] and out["waiting"] == 0
    row = db.get(conn, decision_id)
    assert row["status"] == "chosen" and row["result"]["option"] == "wait" and row["result"]["src"] == "web"
    code, out = post(ui, {"id": decision_id, "action": "choose", "option": "go"})
    assert code == 409 and out["error"] == strings.t("err_changed", status=strings.t("st_chosen"))


def test_answer(ui, conn):
    rid = db.create_request(conn, "questions", "Q", "c", "s", options=[
        {"key": "one", "text": "First?", "options": [{"key": "y", "label": "Yes"}, {"key": "n", "label": "No"}]},
        {"key": "two", "text": "Free?", "options": []}])
    show(conn, rid)
    assert post(ui, {"id": rid, "action": "answer", "answers": {"one": "y"}})[0] == 400  # one is missing
    code, _ = post(ui, {"id": rid, "action": "answer", "answers": {"one": "y", "two": "Friday"}, "note": "thanks"})
    assert code == 200
    row = db.get(conn, rid)
    assert row["status"] == "answered" and row["result"]["answers"]["two"]["text"] == "Friday"


def test_revise_and_discard(ui, conn, decision_id, reply_id):
    show(conn, decision_id)
    show(conn, reply_id)
    assert post(ui, {"id": decision_id, "action": "revise", "text": "  "})[0] == 400
    assert post(ui, {"id": decision_id, "action": "revise", "text": "Try again"})[0] == 200
    assert db.get(conn, decision_id)["status"] == "revise"
    assert post(ui, {"id": reply_id, "action": "discard"})[0] == 200
    assert db.get(conn, reply_id)["status"] == "discarded"


def test_seen_and_next_on_reports(ui, conn):
    a = report(conn)
    assert post(ui, {"id": a, "action": "seen"})[0] == 200
    assert db.get(conn, a)["status"] == "chosen"
    b = report(conn, next=["Step one", "Step two"])
    assert post(ui, {"id": b, "action": "next", "indexes": [0, 0]})[0] == 400
    assert post(ui, {"id": b, "action": "next", "indexes": [1]})[0] == 200
    row = db.get(conn, b)
    assert row["status"] == "revise" and row["result"]["steps"] == ["Step two"]


def test_send_gate(ui, conn, reply_id, user, monkeypatch):
    show(conn, reply_id)
    code, out = post(ui, {"id": reply_id, "action": "send", "text": "Hello there"})
    assert code == 403 and out["error"] == strings.t("err_gate")
    assert db.get(conn, reply_id)["status"] == "shown" and not user.of("chat_postMessage")
    assert 'data-action="send"' not in call(ui, "GET", "/")[1].decode()
    monkeypatch.setenv("POLAR_WEB_ALLOW", "send")
    assert 'data-action="send"' in call(ui, "GET", "/")[1].decode()
    code, out = post(ui, {"id": reply_id, "action": "send", "text": "Hello there"})
    assert code == 200 and db.get(conn, reply_id)["status"] == "sent"
    assert user.of("chat_postMessage")[0]["markdown_text"] == "Hello there"


def test_send_failure_shows_error(ui, conn, reply_id, user, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_ALLOW", "send")
    show(conn, reply_id)
    user.fail = "chat_postMessage"
    code, out = post(ui, {"id": reply_id, "action": "send"})
    assert code == 502 and out["error"] == strings.t("err_send") and "xoxp" not in json.dumps(out)


# -- files ---------------------------------------------------------------------------------------------------------------
def test_files_inside_root_only(ui, conn, data, tmp_path):
    good = data / "files" / "chart.png"
    good.write_bytes(demo.chart_png(8, 8))
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = data / "files" / "link.txt"
    link.symlink_to(outside)
    rid = report(conn, files=[str(good), str(outside), str(data / "files" / ".." / ".." / "secret.txt"), str(link)])
    code, raw, h = call(ui, "GET", "/files/%s/0" % rid)
    assert code == 200 and raw.startswith(b"\x89PNG") and h["Content-Type"] == "image/png"
    assert "sandbox" in h["Content-Security-Policy"]
    for n in (1, 2, 3, 4, 99):
        assert call(ui, "GET", "/files/%s/%d" % (rid, n))[0] == 404
    assert call(ui, "GET", "/files/%s/-1" % rid)[0] == 404
    assert call(ui, "GET", "/files/..%2F..%2Fetc/0")[0] == 404


def test_other_files_download(ui, conn, data):
    f = data / "files" / "data.csv"
    f.write_text("a,b\n")
    rid = report(conn, files=[str(f)])
    code, _, h = call(ui, "GET", "/files/%s/0" % rid)
    assert code == 200 and h["Content-Disposition"].startswith("attachment")
    page = call(ui, "GET", "/")[1].decode()
    assert 'class="file"' in page and "data.csv" in page


# -- start-up and demo ---------------------------------------------------------------------------------------------------
def test_ui_off_without_port(polar, monkeypatch):
    monkeypatch.delenv("POLAR_UI_PORT", raising=False)
    assert server.start(polar) is None
    monkeypatch.setenv("POLAR_UI_PORT", "0")
    assert server.start(polar) is None
    monkeypatch.setenv("POLAR_UI_PORT", "nope")
    assert server.start(polar) is None


def test_ui_starts_with_port(polar, data, monkeypatch, caplog):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("POLAR_UI_PORT", str(port))
    caplog.set_level(logging.INFO, logger="polar")
    srv = server.start(polar)
    try:
        assert srv is not None and srv.server_address[0] == "127.0.0.1" and srv.port == port
        assert call(srv, "GET", "/healthz")[0] == 200
        assert "polar ui-url" in caplog.text and srv.login not in caplog.text and srv.session not in caplog.text
    finally:
        srv.shutdown()
        srv.server_close()


def test_demo_seed_makes_every_kind(tmp_path, monkeypatch):
    real = tmp_path / "real-data"
    monkeypatch.setenv("POLAR_DATA_DIR", str(real))
    d = tmp_path / "demo"
    p, ids = demo.prepare(d)
    assert not real.exists()  # the seed never touches POLAR_DATA_DIR
    rows = [db.get(p.conn, i) for i in ids.values()]
    assert 12 <= len(rows) <= 16
    assert {r["kind"] for r in rows} == set(db.KINDS)
    waiting = views.pending_cards(p.conn)
    kinds = [c["kind"] for c in waiting]
    assert kinds.count("decision") == 2 and kinds.count("report") == 2
    assert {"questions", "reply", "email"} <= set(kinds)
    assert any(c["kind"] == "report" and (c["report"].get("next")) for c in waiting)
    hist, _ = views.history(p.conn, 0)
    assert len(hist) >= 8 and len({h["decided_at"][:10] for h in hist}) >= 3
    png = Path(db.get(p.conn, ids["search"])["report"]["files"][0])
    assert png.read_bytes().startswith(b"\x89PNG") and str(png).startswith(str(d))


def test_demo_actions_change_only_the_db(tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_ALLOW", "send")
    monkeypatch.setenv("POLAR_TASK_NOTICES", "off")
    p, ids = demo.prepare(tmp_path / "demo")
    srv = server.UIServer(0, p, demo=True, data_dir=tmp_path / "demo")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert strings.t("demo_banner") in call(srv, "GET", "/")[1].decode()
        assert post(srv, {"id": ids["release"], "action": "choose", "option": "ship"})[0] == 200
        assert post(srv, {"id": ids["email"], "action": "send"})[0] == 200
        assert post(srv, {"id": ids["reply"], "action": "send", "text": "Fixed, thanks."})[0] == 200
        assert post(srv, {"id": ids["database"], "action": "revise", "text": "Add the cost."})[0] == 200
        assert post(srv, {"id": ids["search"], "action": "next", "indexes": [0, 2]})[0] == 200
        assert db.get(p.conn, ids["email"])["status"] == "sent"
        assert db.get(p.conn, ids["reply"])["status"] == "sent"
        hist = call(srv, "GET", "/history")[1].decode()
        assert "Ship release 2.4" in hist
        assert call(srv, "GET", "/files/%s/0" % ids["search"])[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_demo_cli_parser():
    from polar import cli
    args = cli.build_parser().parse_args(["demo", "--port", "8793", "--lang", "es"])
    assert args.cmd == "demo" and args.port == 8793 and args.lang == "es"
