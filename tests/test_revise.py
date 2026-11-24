import json

from polar import blocks, cli, db
from polar.core import Polar
from tests.conftest import OWNER, FakeClient


def view(rid, text):
    return {"private_metadata": rid, "state": {"values": {"text_block": {"text": {"value": text}}}}}


def actions(bl):
    return [e["action_id"] for b in bl if b["type"] == "actions" for e in b["elements"]]


def test_button_on_reply_and_decision(polar, conn, reply_id, decision_id, bot):
    polar.deliver_pending()
    for call in bot.of("chat_postMessage"):
        assert "polar_revise" in actions(call["blocks"])


def test_click_opens_modal(polar, conn, reply_id, bot):
    polar.deliver_pending()
    polar.on_revise_click(OWNER, reply_id, "trig")
    modal = bot.of("views_open")[0]["view"]
    assert modal["callback_id"] == "polar_revise_submit"
    assert modal["blocks"][0]["label"]["text"] == "¿Qué cambio quieres?"
    assert modal["blocks"][0]["element"]["multiline"]


def test_submit_sets_revise(polar, conn, reply_id, bot):
    polar.deliver_pending()
    assert polar.on_revise_submit(OWNER, view(reply_id, "Usa otro argumento")) is None
    r = db.get(conn, reply_id)
    assert r["status"] == "revise" and r["result"] == {"instructions": "Usa otro argumento"} and r["decided_at"]
    text = bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]
    assert text == "🔁 Pediste cambios: Usa otro argumento"
    assert cli._public(r)["instructions"] == "Usa otro argumento"


def test_submit_empty_or_stranger(polar, conn, reply_id):
    polar.deliver_pending()
    assert polar.on_revise_submit(OWNER, view(reply_id, " "))["response_action"] == "errors"
    assert polar.on_revise_submit("U9", view(reply_id, "x")) is None
    assert db.get(conn, reply_id)["status"] == "shown"


def test_replaces_marks_old(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "polar.db")
    bot = FakeClient("bot")
    polar = Polar(conn, bot, FakeClient("user"), OWNER)
    reply_id = db.create_request(conn, "decision", "Old", "", "s", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    polar.deliver_pending()
    polar.on_revise_submit(OWNER, view(reply_id, "cambia"))
    assert cli.main(["ask", "decision", "--title", "T", "--option", "a:A", "--option", "b:B",
                     "--replaces", reply_id, "--requester", "sess-1 pane"]) == 0
    new = json.loads(capsys.readouterr().out.splitlines()[-1])["id"]
    assert db.get(conn, reply_id)["status"] == "replaced"
    assert db.get(conn, new)["replaces"] == reply_id
    polar.tick()
    assert bot.of("chat_update")[-1]["blocks"][0]["text"]["text"] == "↪️ Reemplazada por una tarjeta nueva"
    blk = json.dumps(blocks.request_blocks(db.get(conn, new)), ensure_ascii=False)
    assert "Reemplaza una tarjeta anterior" in blk


def test_replaces_ignores_decided(polar, conn, decision_id):
    polar.deliver_pending()
    polar.on_choose(OWNER, decision_id, "go")
    assert db.mark_replaced(conn, decision_id) is False
    assert db.get(conn, decision_id)["status"] == "chosen"


def test_discard_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    conn = db.connect(tmp_path / "polar.db")
    bot = FakeClient("bot")
    polar = Polar(conn, bot, FakeClient("user"), OWNER)
    reply_id = db.create_request(conn, "decision", "Old", "", "s", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    polar.deliver_pending()
    assert cli.main(["discard", reply_id]) == 0
    assert db.get(conn, reply_id)["status"] == "discarded"
    polar.sweep()
    assert bot.of("chat_update")[-1]["blocks"][0]["text"]["text"] == "🗑️ Descartado"
    assert cli.main(["discard", reply_id]) == 1  # already discarded


def test_discard_revise(polar, conn, reply_id):
    polar.deliver_pending()
    polar.on_revise_submit(OWNER, view(reply_id, "x"))
    assert db.withdraw(conn, reply_id)


def test_open_list_includes_recent_revise(polar, conn, reply_id, decision_id):
    """A card in `revise` waits for its session (a new card with --replaces): `list --status open` shows it, so
    server-tasks does not idle-close the session and agent-bus does not auto-park it."""
    polar.deliver_pending()
    polar.on_revise_submit(OWNER, view(reply_id, "Cambia el tono"))
    ids = {r["id"] for r in db.list_requests(conn, "open")}
    assert reply_id in ids and decision_id in ids


def test_open_list_drops_old_revise(polar, conn, reply_id):
    polar.deliver_pending()
    polar.on_revise_submit(OWNER, view(reply_id, "Cambia el tono"))
    conn.execute("UPDATE requests SET decided_at='2026-01-01T00:00:00Z' WHERE id=?", (reply_id,))
    assert reply_id not in {r["id"] for r in db.list_requests(conn, "open")}


def test_open_list_drops_revise_when_its_session_sent_a_later_card(polar, conn, reply_id):
    """A follow-up of a report: the session did the work and sent its new report. Nothing is owed, so the old
    card must not keep the session open (2026-11-23: two finished laptop tabs held 2 of 3 task slots)."""
    polar.deliver_pending()
    polar.on_revise_submit(OWNER, view(reply_id, "Haz el seguimiento"))
    conn.execute("UPDATE requests SET requester='sid-1 w8:p1', decided_at='2099-01-01T00:00:00Z' WHERE id=?", (reply_id,))
    conn.execute("INSERT INTO requests(id,kind,status,title,requester,created_at,expires_at) "
                 "VALUES('other','report','noted','de otra sesión','sid-2 w8:p2','2099-01-02T00:00:00Z','2099-02-01T00:00:00Z')")
    assert reply_id in {r["id"] for r in db.list_requests(conn, "open")}  # a card of another session changes nothing
    conn.execute("INSERT INTO requests(id,kind,status,title,requester,created_at,expires_at) "
                 "VALUES('before','report','noted','antes','sid-1 w8:p1','2098-12-31T00:00:00Z','2099-02-01T00:00:00Z')")
    assert reply_id in {r["id"] for r in db.list_requests(conn, "open")}  # a card from before the answer
    conn.execute("INSERT INTO requests(id,kind,status,title,requester,created_at,expires_at,silent) "
                 "VALUES('notice','report','noted','sigo en esto','sid-1 w8:p1','2099-01-02T00:00:00Z','2099-02-01T00:00:00Z',1)")
    assert reply_id in {r["id"] for r in db.list_requests(conn, "open")}  # a silent notice is not the answer
    conn.execute("INSERT INTO requests(id,kind,status,title,requester,created_at,expires_at) "
                 "VALUES('later','report','noted','el informe nuevo','sid-1 w8:pZ','2099-01-02T00:00:00Z','2099-02-01T00:00:00Z')")
    assert reply_id not in {r["id"] for r in db.list_requests(conn, "open")}


def test_open_list_drops_replaced(polar, conn, reply_id):
    polar.deliver_pending()
    polar.on_revise_submit(OWNER, view(reply_id, "Cambia el tono"))
    conn.execute("UPDATE requests SET status='replaced' WHERE id=?", (reply_id,))
    assert reply_id not in {r["id"] for r in db.list_requests(conn, "open")}


# -- «Pedir cambios» on a questions card ---------------------------------------------
QS = [
    {"key": "a", "text": "¿Alerta?", "options": [{"key": "si", "label": "Sí"}, {"key": "no", "label": "No"}]},
    {"key": "u", "text": "¿Umbral?"},
    {"key": "c", "text": "¿Canal?", "options": [{"key": "x", "label": "X"}]},
]


def qcard(conn):
    return db.create_request(conn, "questions", "Alertas", "c", "s1", options=QS)


def test_questions_card_has_revise_button(polar, conn, bot):
    qcard(conn)
    polar.deliver_pending()
    bl = bot.of("chat_postMessage")[0]["blocks"]
    last = [b for b in bl if b.get("block_id") == "polar_actions"][0]["elements"][-1]
    assert last["action_id"] == "polar_revise"


def test_questions_click_opens_modal(polar, conn, bot):
    rid = qcard(conn)
    polar.deliver_pending()
    polar.on_revise_click(OWNER, rid, "trig")
    assert bot.of("views_open")[0]["view"]["callback_id"] == "polar_revise_submit"


def test_questions_submit_keeps_answers(polar, conn, bot):
    rid = qcard(conn)
    polar.deliver_pending()
    req = db.get(conn, rid)
    db.set_result(conn, rid, {"answers": {"a": {"option": "si", "text": "Sí"}, "c": {"option": None, "text": "#ops"}},
                              "note": "ojo"})
    assert polar.on_revise_submit(OWNER, view(rid, "Cambia las preguntas")) is None
    r = db.get(conn, rid)
    assert r["status"] == "revise"
    assert r["result"] == {"instructions": "Cambia las preguntas\n\nRespuestas ya dadas: 1 = Sí; 3 = #ops. Nota: ojo"}
    assert cli._public(r)["instructions"] == r["result"]["instructions"]


def test_questions_submit_without_answers_or_with_note_only(polar, conn):
    rid = qcard(conn)
    polar.deliver_pending()
    polar.on_revise_submit(OWNER, view(rid, "Otra cosa"))
    assert db.get(conn, rid)["result"] == {"instructions": "Otra cosa"}
    rid2 = qcard(conn)
    polar.deliver_pending()
    db.set_result(conn, rid2, {"answers": {}, "note": "solo nota"})
    polar.on_revise_submit(OWNER, view(rid2, "Otra"))
    assert db.get(conn, rid2)["result"]["instructions"] == "Otra\n\nRespuestas ya dadas: ninguna. Nota: solo nota"


def test_questions_fallback_and_last_asked_wins(polar, conn, bot):
    import time
    rid = qcard(conn)
    polar.deliver_pending()
    bot.views_fail = True
    polar.on_q_note_click(OWNER, rid, "trig")
    assert db.get_meta(conn, "fallback:" + rid)["kind"] == "qnote"
    polar.on_revise_click(OWNER, rid, "trig")
    assert db.get_meta(conn, "fallback:" + rid)["kind"] == "revise"
    req = db.get(conn, rid)
    polar.on_message({"type": "message", "channel_type": "im", "channel": req["dm_channel"], "user": OWNER,
                      "text": "cambia", "ts": "%.6f" % (time.time() + 5), "thread_ts": req["dm_ts"]})
    r = db.get(conn, rid)
    assert r["status"] == "revise" and r["result"]["instructions"] == "cambia"
    assert not (r["result"].get("note"))


def test_ask_questions_accepts_replaces(tmp_path):
    a = cli.build_parser().parse_args(["ask", "questions", "--title", "t", "--question-file", "q.json", "--replaces", "abc"])
    assert a.replaces == "abc"
