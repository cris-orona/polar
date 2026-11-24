import json
import sqlite3

import pytest

from polar import blocks, cli, db
from tests.conftest import OWNER


def texts(bl):
    return json.dumps(bl, ensure_ascii=False)


# -- item 1: icons ------------------------------------------------------------
@pytest.mark.parametrize("kind,title,who,icon", [
    ("reply", "T", "s", "💬"), ("decision", "T", "s", "✅"),
    ("decision", "T", "release-desk abc", "🚀"), ("decision", "Paso pendiente: x", "s", "⚙️"),
    ("decision", "T", "watchdog", "🚨")])
def test_icons(kind, title, who, icon):
    assert blocks.icon({"kind": kind, "title": title, "requester": who}) == icon


def test_header_is_short():
    req = {"kind": "decision", "title": "x" * 400, "requester": ""}
    assert len(blocks.header_text(req)) <= 150


# -- item 2: original message ---------------------------------------------------
class Fetcher:
    def __init__(self, msgs, fail=None):
        self.msgs, self.fail, self.calls = msgs, fail, []

    def conversations_replies(self, **kw):
        if self.fail == "replies":
            raise RuntimeError("xoxp-secret")
        self.calls.append(kw)
        return {"messages": self.msgs}

    def conversations_info(self, **kw):
        return {"channel": {"name": "team-product-channel"}}

    def users_info(self, user):
        return {"user": {"profile": {"display_name": "Leo"}}}

    def chat_getPermalink(self, **kw):
        return {"permalink": "https://x/p" + kw["message_ts"]}


MSGS = [{"user": "ULEO", "ts": "1795000000.000100", "text": "Primero"},
        {"user": "ULEO", "ts": "1795000100.000100", "text": "Ya quedó\nel cambio " + "x" * 400},
        {"user": OWNER, "ts": "1795000200.000100", "text": "mío"}]


def test_fetch_newest_non_owner(polar, conn, reply_id):
    polar.user = Fetcher(MSGS)
    polar.fetch_original(db.get(conn, reply_id))
    r = db.get(conn, reply_id)
    assert r["orig_name"] == "Leo" and r["orig_text"].startswith("Ya quedó el cambio")
    assert len(r["orig_text"]) <= 300 and "\n" not in r["orig_text"]
    assert r["orig_permalink"].endswith("1795000100.000100")
    assert r["orig_channel_name"] == "team-product-channel"
    assert len(r["orig_time"]) == 5 and r["orig_time"][2] == ":"


def test_fetch_exact_reply_to(polar, conn):
    rid = db.create_request(conn, "reply", "T", "c", "", draft_text="x", target_channel="C1",
                            reply_to_ts="1795000000.000100")
    polar.user = Fetcher(MSGS)
    polar.fetch_original(db.get(conn, rid))
    assert db.get(conn, rid)["orig_text"] == "Primero"
    assert polar.user.calls[0]["ts"] == "1795000000.000100"


def test_fetch_failure_shows_card_without_quote(polar, conn, reply_id, bot, caplog):
    polar.user = Fetcher(MSGS, fail="replies")
    assert polar.deliver_pending() == 1
    sent = bot.of("chat_postMessage")[0]["blocks"]
    assert "escribió" not in texts(sent)
    assert "xoxp" not in caplog.text


def test_card_with_quote(conn, reply_id):
    db.set_original(conn, reply_id, orig_name="Leo", orig_time="16:25", orig_text="Hola",
                    orig_permalink="https://l", orig_channel_name="ops")
    b = blocks.request_blocks(db.get(conn, reply_id))
    assert b[0]["text"]["text"] == "💬 Respuesta a Leo · #ops"
    assert b[1]["text"]["text"].startswith('> Leo escribió (16:25): "Hola"')
    assert "<https://l|Ver hilo ↗>" in b[1]["text"]["text"]


def test_dm_header(conn):
    rid = db.create_request(conn, "reply", "T", "", "", draft_text="x", target_channel="D1")
    db.set_original(conn, rid, orig_name="Gina")
    assert blocks.header_text(db.get(conn, rid)) == "💬 Respuesta a Gina · DM"


# -- item 3: gate -----------------------------------------------------------------
@pytest.mark.parametrize("ctx,bad", [
    ("Leo no pudo enviar la corrección.", False),
    ("Mira https://x.com/a_b_c y #live_ops", False),
    ("route_fact_route_already_priced", True),
    ("x" * 281, True), ("a\nb\nc", True)])
def test_check_context(ctx, bad):
    assert bool(cli.check_context(ctx)) is bad


def test_gate_exit_code(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    base = ["ask", "decision", "--title", "T", "--option", "a:A", "--option", "b:B", "--requester", "sess-1 pane"]
    assert cli.main(base + ["--context", "bad_code_here"]) == 2
    assert "snake_case" in capsys.readouterr().err
    assert cli.main(base + ["--context", "bad_code_here", "--force-context"]) == 0


def test_por_que_line(conn, decision_id):
    assert "*Por qué:* lote 3" in texts(blocks.request_blocks(db.get(conn, decision_id)))


# -- item 4: session label --------------------------------------------------------
def test_session_label(conn, decision_id):
    req = db.get(conn, decision_id)
    assert "🧵 Sesión: Sesión sin nombre" in texts(blocks.request_blocks(req))
    assert "sesion-2" not in texts(blocks.request_blocks(req))
    req["requester_label"] = "Ajustes de flete"
    assert "🧵 Sesión: Ajustes de flete" in texts(blocks.request_blocks(req))


def test_label_cli(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    assert cli.main(["ask", "decision", "--title", "T", "--option", "a:A", "--option", "b:B",
                     "--requester-label", "Mi tema", "--requester", "uuid pane"]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    assert db.get(db.connect(tmp_path / "polar.db"), rid)["requester_label"] == "Mi tema"


# -- migration ----------------------------------------------------------------------
def test_migration_old_schema(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA)
    raw.execute("INSERT INTO requests (id,kind,status,created_at,title,expires_at)"
                " VALUES ('a','decision','shown','t','T','e')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}
    assert set(db.NEW_COLUMNS) <= cols
    assert db.get(conn, "a")["orig_name"] is None
    db.connect(path)  # a second run is safe


# -- item 5: shrink ------------------------------------------------------------------
def test_sent_line_and_edited(conn, reply_id):
    r = db.get(conn, reply_id)
    r.update(status="sent", result={"permalink": "https://l"}, orig_name="Leo", final_text="Hola Ana")
    assert blocks.result_line(r) == "✅ Enviado a Leo · <https://l|ver ↗>"
    r["final_text"] = "otro"
    assert blocks.result_line(r).startswith("✏️ Editado y enviado a Leo")


def test_other_lines(conn, reply_id):
    r = db.get(conn, reply_id)
    for st, line in [("expired", "⌛ Expiró"), ("discarded", "🗑️ Descartado")]:
        r["status"] = st
        assert blocks.result_line(r) == line
    r.update(status="failed", result={"error": "ratelimited"})
    assert blocks.result_line(r) == "⚠️ Falló: ratelimited"


def test_shrunk_card_is_one_section(polar, bot, conn, reply_id):
    db.transition(conn, reply_id, "pending", "shown", dm_channel="D1", dm_ts="1.1")
    polar.on_discard(OWNER, reply_id)
    upd = bot.of("chat_update")[-1]
    assert len(upd["blocks"]) == 1 and upd["text"] == "🗑️ Descartado"
