import base64
import email
import email.policy
import json
import os
import stat

import pytest

from polar import cli, db, gmail, inbox
from tests.conftest import OWNER


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def msg(mid, frm, to, subject="Cotización", body="Hola", mid_hdr=None, refs=None, ms=1700000000000):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to},
               {"name": "Subject", "value": subject}, {"name": "Date", "value": "Mon, 1 Jan 2026 10:00:00 -0600"},
               {"name": "Message-ID", "value": mid_hdr or f"<{mid}@mail>"}]
    if refs:
        headers.append({"name": "References", "value": refs})
    return {"id": mid, "threadId": "T1", "snippet": body[:50], "internalDate": str(ms),
            "payload": {"mimeType": "multipart/alternative", "headers": headers, "parts": [
                {"mimeType": "text/plain", "body": {"data": b64(body)}},
                {"mimeType": "text/html", "body": {"data": b64("<p>x</p>")}}]}}


class Exec:
    def __init__(self, val, log, name):
        self.val, self.log, self.name = val, log, name

    def execute(self):
        return self.val


class Chain:
    """users().messages().list(...) style fake. Records calls."""
    def __init__(self, svc, path=()):
        self.svc, self.path = svc, path

    def __getattr__(self, name):
        def call(*a, **kw):
            path = self.path + (name,)
            if name in ("users", "messages", "threads", "drafts"):
                return Chain(self.svc, path)
            self.svc.calls.append((".".join(path), kw))
            return Exec(self.svc.responses[".".join(path)](kw), self.svc.calls, name)
        return call


class FakeGmail:
    def __init__(self, messages):
        self.calls = []
        self.messages = messages
        self.responses = {
            "messages.list": lambda kw: {"messages": [{"id": m["id"]} for m in messages]},
            "messages.get": lambda kw: next(m for m in messages if m["id"] == kw["id"]),
            "threads.get": lambda kw: {"id": "T1", "messages": messages},
            "getProfile": lambda kw: {"emailAddress": "me@corp.example"},
            "drafts.create": lambda kw: {"id": "D77"},
            "messages.send": lambda kw: {"id": "M9", "threadId": "T1"},
        }
        self.users = lambda: Chain(self, ())

    def of(self, name):
        return [kw for n, kw in self.calls if n.endswith(name)]


def make(messages=None):
    return FakeGmail(messages or [
        msg("m1", "Ana Pérez <ana@client.example>", "me@corp.example", body="Hola, ¿precio?"),
        msg("m2", "me@corp.example", "ana@client.example", subject="Re: Cotización", body="Va", refs="<m1@mail>"),
        msg("m3", "Ana Pérez <ana@client.example>", "me@corp.example", subject="Re: Cotización", body="Gracias, y el plazo?",
            refs="<m1@mail> <m2@mail>"),
    ])


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    return tmp_path


def decode(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)


def test_search_and_thread():
    svc = make()
    rows = gmail.search(svc, "from:ana", 5)
    assert svc.of("messages.list") == [{"userId": "me", "q": "from:ana", "maxResults": 5}]
    assert rows[0]["from"].startswith("Ana") and rows[0]["subject"] == "Cotización" and rows[0]["threadId"] == "T1"
    t = gmail.thread(svc, "T1")
    assert [m["body"] for m in t["messages"]][2] == "Gracias, y el plazo?"
    assert t["messages"][0]["message_id"] == "<m1@mail>" and "cc" in t["messages"][0]


def test_html_only_body_becomes_text():
    m = msg("x", "a@b.c", "me@corp.example")
    m["payload"]["parts"] = [{"mimeType": "text/html", "body": {"data": b64("<p>Hola<br>mundo</p>")}}]
    assert gmail.body_text(m) == "Hola\nmundo"


def test_reply_headers():
    svc = make()
    body = gmail.build_reply(svc, "T1", "Hola **Ana**", cc="jefe@corp.example")
    assert body["threadId"] == "T1"
    m = decode(body["raw"])
    assert m["To"] == "Ana Pérez <ana@client.example>" and m["Cc"] == "jefe@corp.example"
    assert m["Subject"] == "Re: Cotización"  # no double Re:
    assert m["In-Reply-To"] == "<m3@mail>"
    assert m["References"] == "<m1@mail> <m2@mail> <m3@mail>"
    assert m.get_body(("plain",)).get_content().strip() == "Hola **Ana**"
    assert "<b>Ana</b>" in m.get_body(("html",)).get_content()


def test_reply_subject_gets_re_and_owner_last_writes_to_same_people():
    svc = make([msg("m1", "me@corp.example", "Ana <ana@client.example>", subject="Hola")])
    m = decode(gmail.build_reply(svc, "T1", "x")["raw"])
    assert m["Subject"] == "Re: Hola" and m["To"] == "Ana <ana@client.example>"


def test_draft_returns_id():
    svc = make()
    assert gmail.create_draft(svc, "T1", "Hola") == "D77"
    sent = svc.of("drafts.create")[0]
    assert sent["userId"] == "me" and sent["body"]["message"]["threadId"] == "T1"


def test_to_html():
    out = gmail.to_html("Hola <b>\n\n- uno\n- [dos](https://x.example)\n\nlinea1\nlinea2")
    assert "&lt;b&gt;" in out and "<ul><li>uno</li><li><a href=\"https://x.example\">dos</a></li></ul>" in out
    assert "linea1<br>linea2" in out


def test_no_credentials(data, monkeypatch, capsys):
    for argv in (["mail", "search", "x"], ["mail", "thread", "T"], ["mail", "draft", "--thread", "T", "--text", "x"]):
        assert cli.main(argv) == 2
        assert "Gmail no está conectado: corre polar mail auth" in capsys.readouterr().err
    assert cli.main(["ask", "email", "--thread", "T", "--title", "t", "--text", "x", "--requester", "sess-1 pane"]) == 2
    assert "Gmail no está conectado" in capsys.readouterr().err


def test_cli_mail_with_fake_service(data, monkeypatch, capsys):
    svc = make()
    monkeypatch.setattr(gmail, "service", lambda: svc)
    assert cli.main(["mail", "search", "from:ana"]) == 0
    first = json.loads(capsys.readouterr().out.splitlines()[0])
    assert set(first) == {"id", "threadId", "from", "to", "subject", "date", "snippet"}
    assert cli.main(["mail", "thread", "T1"]) == 0
    assert len(json.loads(capsys.readouterr().out)["messages"]) == 3
    f = data / "t.txt"
    f.write_text("Hola")
    assert cli.main(["mail", "draft", "--thread", "T1", "--text-file", str(f)]) == 0
    assert json.loads(capsys.readouterr().out) == {"draft_id": "D77"}


def test_token_file_is_private_and_refreshes(data, monkeypatch):
    class Creds:
        valid, expired, refresh_token = False, True, "r"

        def refresh(self, _req):
            self.valid = True

        def to_json(self):
            return '{"token": "secret"}'

    from google.oauth2 import credentials as gc
    monkeypatch.setattr(gc.Credentials, "from_authorized_user_file", classmethod(lambda cls, p, s: Creds()))
    gmail.token_path().write_text("{}")
    gmail.credentials()
    assert stat.S_IMODE(os.stat(gmail.token_path()).st_mode) == 0o600
    assert "secret" in gmail.token_path().read_text()


def test_auth_flow_saves_private_token(data, monkeypatch):
    class Flow:
        def __init__(self):
            self.got = None
            self.credentials = type("C", (), {"to_json": lambda s: '{"token":"t"}'})()

        def fetch_token(self, **kw):
            self.got = kw

    flow = Flow()
    gmail.finish_auth(flow, "http://localhost:1/?state=s&code=4%2Fabc&scope=x")
    assert "code=" in flow.got["authorization_response"]
    assert stat.S_IMODE(os.stat(gmail.token_path()).st_mode) == 0o600
    gmail.finish_auth(flow, "4/abc")
    assert flow.got == {"code": "4/abc"}
    with pytest.raises(ValueError):
        gmail.finish_auth(flow, "  ")


def test_auth_url_needs_client_file(data):
    with pytest.raises(ValueError):
        gmail.auth_url()


# -- the email card ------------------------------------------------------------------
@pytest.fixture
def email_id(conn):
    return db.create_request(conn, "email", "Responder a Ana", "plazo", "s1", draft_text="Hola Ana",
                             target_thread_ts="T1", email_cc="jefe@corp.example")


def test_email_card_quotes_last_message_and_has_same_buttons(polar, bot, conn, email_id):
    polar._gmail = make()
    polar.deliver_pending()
    req = db.get(conn, email_id)
    assert req["status"] == "shown" and req["orig_name"] == "Ana Pérez"
    assert "plazo" in req["orig_text"] and req["email_subject"] == "Re: Cotización"
    blocks = bot.of("chat_postMessage")[0]["blocks"]
    ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert ids == ["polar_send", "polar_edit", "polar_discard", "polar_revise"]
    text = json.dumps(blocks, ensure_ascii=False)
    assert "Ana Pérez" in text and "Cc: jefe@corp.example" in text and "✉️" in text


def test_email_send_as_owner_in_thread(polar, user, conn, email_id):
    polar._gmail = svc = make()
    polar.deliver_pending()
    polar.on_send(OWNER, email_id)
    sent = svc.of("messages.send")[0]
    assert sent["body"]["threadId"] == "T1"
    assert decode(sent["body"]["raw"])["Cc"] == "jefe@corp.example"
    req = db.get(conn, email_id)
    assert req["status"] == "sent" and req["result"]["message_id"] == "M9"
    assert req["result"]["permalink"].startswith("https://mail.google.com/")
    assert user.calls == []  # no Slack post
    polar.on_send(OWNER, email_id)  # second click does nothing
    assert len(svc.of("messages.send")) == 1


def test_email_edit_then_send_uses_edited_text(polar, conn, email_id):
    polar._gmail = svc = make()
    polar.deliver_pending()
    view = {"private_metadata": email_id, "state": {"values": {"text_block": {"text": {"value": "Nuevo"}}}}}
    assert polar.on_edit_submit(OWNER, view) is None
    polar.on_send(OWNER, email_id)
    assert decode(svc.of("messages.send")[0]["body"]["raw"]).get_body(("plain",)).get_content().strip() == "Nuevo"
    assert "Editado y enviado" in polar.bot.of("chat_update")[-1]["blocks"][-1]["text"]["text"]


def test_email_discard_and_revise(polar, conn, email_id, bot):
    polar._gmail = make()
    polar.deliver_pending()
    polar.on_revise_click(OWNER, email_id, "trig")
    assert bot.of("views_open")
    polar.on_discard(OWNER, email_id)
    assert db.get(conn, email_id)["status"] == "discarded"


def test_email_send_failure_is_failed_without_secrets(polar, conn, email_id):
    class Boom:
        def users(self):
            raise RuntimeError("secret ya29.token")
    polar._gmail = make()
    polar.deliver_pending()
    polar._gmail = Boom()
    polar.on_send(OWNER, email_id)
    req = db.get(conn, email_id)
    assert req["status"] == "failed" and "ya29" not in json.dumps(req["result"])


def test_ask_email_cli(data, monkeypatch, capsys):
    gmail.token_path().write_text("{}")
    assert cli.main(["ask", "email", "--thread", "T1", "--title", "Responder", "--context", "plazo",
                     "--text", "Hola", "--cc", "a@b.example", "--requester", "sess-1 pane"]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    req = db.get(db.connect(), rid)
    assert req["kind"] == "email" and req["target_thread_ts"] == "T1" and req["email_cc"] == "a@b.example"


# -- polar thread ----------------------------------------------------------------------
class FakeSlack:
    def __init__(self):
        self.calls = []

    def conversations_replies(self, **kw):
        self.calls.append(kw)
        if kw.get("cursor"):
            return {"messages": [{"ts": "3.0", "user": "U3", "text": "c"}], "response_metadata": {}}
        return {"messages": [{"ts": "1.0", "user": "U1", "text": "a"},
                             {"ts": "2.0", "user": "U2", "text": "b", "thread_ts": "1.0"}],
                "response_metadata": {"next_cursor": "NEXT"}}


def test_thread_messages_paginates_with_inbox_fields():
    c = FakeSlack()
    rows = inbox.thread_messages(c, "C1", "1.0", "https://x.slack.com/", "general")
    assert [r["ts"] for r in rows] == ["1.0", "2.0", "3.0"]
    assert set(rows[0]) == {"ts", "channel", "channel_name", "is_dm", "thread_ts", "sender", "text", "permalink"}
    assert rows[1]["permalink"] == "https://x.slack.com/archives/C1/p20?thread_ts=1.0&cid=C1"
    assert c.calls[1]["cursor"] == "NEXT"


def test_status_shows_sent_message_id(polar, conn, email_id, capsys, monkeypatch):
    polar._gmail = make()
    polar.deliver_pending()
    polar.on_send(OWNER, email_id)
    pub = cli._public(db.get(conn, email_id))
    assert pub["message_id"] == "M9" and pub["thread_id"] == "T1" and pub["permalink"].startswith("https://mail")
