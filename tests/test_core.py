import json

from polar import db
from tests.conftest import OWNER

STRANGER = "U999"


def shown(polar, rid):
    polar.deliver_pending()
    return db.get(polar.conn, rid)


def test_deliver_uses_bot_token_and_marks_shown(polar, bot, user, reply_id):
    req = shown(polar, reply_id)
    assert req["status"] == "shown" and req["dm_channel"] == "D1" and req["dm_ts"]
    assert bot.of("conversations_open") == [{"users": OWNER}]
    assert bot.of("chat_postMessage")[0]["channel"] == "D1"
    assert user.calls == []
    assert polar.deliver_pending() == 0  # nothing is sent twice


def test_deliver_failure_stays_pending(polar, bot, reply_id):
    bot.fail = "chat_postMessage"
    assert polar.deliver_pending() == 0
    assert db.get(polar.conn, reply_id)["status"] == "pending"


def test_send_posts_as_owner_with_user_token(polar, bot, user, reply_id):
    shown(polar, reply_id)
    polar.on_send(OWNER, reply_id)
    post = user.of("chat_postMessage")
    assert post == [{"channel": "C1", "markdown_text": "Hola Ana", "thread_ts": "55.5"}]
    assert bot.of("chat_postMessage")[1:] == []  # the bot posts only the DM
    req = db.get(polar.conn, reply_id)
    assert req["status"] == "sent" and req["final_text"] == "Hola Ana"
    assert req["result"]["permalink"] == "https://x.slack.com/p1"
    upd = bot.of("chat_update")[-1]
    assert upd["channel"] == "D1" and upd["ts"] == req["dm_ts"]
    assert all(b["type"] != "actions" for b in upd["blocks"])
    assert "Enviado" in upd["blocks"][-1]["text"]["text"]


def test_send_without_thread(polar, user, conn):
    rid = db.create_request(conn, "reply", "T", draft_text="x", target_channel="C9")
    shown(polar, rid)
    polar.on_send(OWNER, rid)
    assert "thread_ts" not in user.of("chat_postMessage")[0]


def test_stranger_is_ignored(polar, bot, user, reply_id, decision_id, caplog):
    shown(polar, reply_id)
    polar.deliver_pending()
    polar.on_send(STRANGER, reply_id)
    polar.on_discard(STRANGER, reply_id)
    polar.on_choose(STRANGER, decision_id, "go")
    polar.on_edit_click(STRANGER, reply_id, "trig")
    assert polar.on_edit_submit(STRANGER, {"private_metadata": reply_id, "state": {"values": {
        "text_block": {"text": {"value": "hack"}}}}}) is None
    assert user.calls == [] and bot.of("views_open") == []
    assert db.get(polar.conn, reply_id)["status"] == "shown"
    assert db.get(polar.conn, reply_id)["final_text"] is None
    assert db.get(polar.conn, decision_id)["status"] == "shown"
    assert STRANGER in caplog.text


def test_second_click_does_nothing(polar, user, bot, reply_id):
    shown(polar, reply_id)
    polar.on_send(OWNER, reply_id)
    polar.on_send(OWNER, reply_id)
    polar.on_discard(OWNER, reply_id)
    assert len(user.of("chat_postMessage")) == 1
    assert len(bot.of("chat_postEphemeral")) == 2  # each dead press answers; see test_stale_click.py
    assert db.get(polar.conn, reply_id)["status"] == "sent"


def test_click_while_sending_does_not_double_post(polar, user, reply_id):
    shown(polar, reply_id)
    db.transition(polar.conn, reply_id, "shown", "sending")
    polar.on_send(OWNER, reply_id)
    assert user.calls == []


def test_discard(polar, user, bot, reply_id):
    shown(polar, reply_id)
    polar.on_discard(OWNER, reply_id)
    assert db.get(polar.conn, reply_id)["status"] == "discarded"
    assert bot.of("chat_update")[-1]["blocks"][-1]["text"]["text"] == "🗑️ Descartado"
    assert user.calls == []


def test_decision(polar, bot, decision_id):
    shown(polar, decision_id)
    polar.on_choose(OWNER, decision_id, "go")
    req = db.get(polar.conn, decision_id)
    assert req["status"] == "chosen" and req["result"] == {"option": "go"}
    assert bot.of("chat_update")[-1]["blocks"][-1]["text"]["text"] == "👉 Elegiste: Go — ¿Release?"
    polar.on_choose(OWNER, decision_id, "wait")
    assert db.get(polar.conn, decision_id)["result"] == {"option": "go"}


def test_decision_unknown_key_ignored(polar, decision_id):
    shown(polar, decision_id)
    polar.on_choose(OWNER, decision_id, "zzz")
    assert db.get(polar.conn, decision_id)["status"] == "shown"


def test_slack_error_sets_failed_without_token(polar, bot, user, reply_id):
    shown(polar, reply_id)
    user.fail = "chat_postMessage"
    polar.on_send(OWNER, reply_id)
    req = db.get(polar.conn, reply_id)
    assert req["status"] == "failed"
    assert "xoxp" not in json.dumps(req["result"])
    assert "Falló" in bot.of("chat_update")[-1]["blocks"][-1]["text"]["text"]


def test_permalink_failure_still_sent(polar, user, reply_id):
    shown(polar, reply_id)
    user.fail = "chat_getPermalink"
    polar.on_send(OWNER, reply_id)
    req = db.get(polar.conn, reply_id)
    assert req["status"] == "sent" and "permalink" not in req["result"]


def view(rid, text):
    return {"private_metadata": rid, "state": {"values": {"text_block": {"text": {"value": text}}}}}


def test_edit_flow(polar, bot, user, reply_id):
    shown(polar, reply_id)
    polar.on_edit_click(OWNER, reply_id, "trig1")
    opened = bot.of("views_open")[0]
    assert opened["trigger_id"] == "trig1"
    assert opened["view"]["blocks"][0]["element"]["initial_value"] == "Hola Ana"
    assert polar.on_edit_submit(OWNER, view(reply_id, "Hola Ana, buen día")) is None
    req = db.get(polar.conn, reply_id)
    assert req["final_text"] == "Hola Ana, buen día" and req["status"] == "shown"
    upd = bot.of("chat_update")[-1]
    assert any(b["type"] == "markdown" and "buen día" in b["text"] for b in upd["blocks"])
    assert upd["blocks"][-1]["type"] == "actions"  # buttons are back
    polar.on_send(OWNER, reply_id)
    assert user.of("chat_postMessage")[0]["markdown_text"] == "Hola Ana, buen día"


def test_edit_validation(polar, reply_id):
    shown(polar, reply_id)
    assert polar.on_edit_submit(OWNER, view(reply_id, "  "))["response_action"] == "errors"
    assert polar.on_edit_submit(OWNER, view(reply_id, "x" * 5000))["response_action"] == "errors"
    assert db.get(polar.conn, reply_id)["final_text"] is None


def test_edit_after_send_does_nothing(polar, bot, reply_id):
    shown(polar, reply_id)
    polar.on_send(OWNER, reply_id)
    polar.on_edit_click(OWNER, reply_id, "t")
    polar.on_edit_submit(OWNER, view(reply_id, "late"))
    assert bot.of("views_open") == []
    assert db.get(polar.conn, reply_id)["final_text"] == "Hola Ana"


def test_expiry_updates_dm_once(polar, bot, reply_id, monkeypatch):
    shown(polar, reply_id)
    polar.conn.execute("UPDATE requests SET expires_at='2000-01-01T00:00:00Z'")
    polar.sweep()
    assert db.get(polar.conn, reply_id)["status"] == "expired"
    assert "Expiró" in bot.of("chat_update")[-1]["blocks"][-1]["text"]["text"]
    n = len(bot.of("chat_update"))
    polar.sweep()
    assert len(bot.of("chat_update")) == n
    polar.on_send(OWNER, reply_id)  # an old button does nothing
    assert db.get(polar.conn, reply_id)["status"] == "expired"


def test_recover_marks_stuck_sending_failed(polar, reply_id):
    shown(polar, reply_id)
    db.transition(polar.conn, reply_id, "shown", "sending")
    polar.recover()
    assert db.get(polar.conn, reply_id)["status"] == "failed"
    polar.sweep()


# -- «¿Abro un agente?» cards ------------------------------------------------
def agent_card(conn, where="server"):
    return db.create_request(
        conn, "decision", "Revisar el error de pagos", "nadie tiene este mensaje", "router",
        draft_text="Revisa el error. https://x.slack.com/p1 de Ana", target_channel="C1",
        options=[{"key": "agent", "label": "Abrir agente"}, {"key": "no", "label": "No"}],
        reply_to_ts="55.5", agent_where=where)


def test_agent_card_has_two_options_and_quotes_source(polar, bot, user, conn, monkeypatch):
    fetched = []
    monkeypatch.setattr(polar, "fetch_original", lambda r: fetched.append(r["id"]))
    rid = agent_card(conn)
    req = shown(polar, rid)
    assert [o["key"] for o in req["options"]] == ["agent", "no"]
    assert req["agent_where"] == "server"
    assert fetched == [rid]  # the card quotes the source message
    assert polar.tasks.rows("task") == []


def test_choose_agent_makes_one_queued_task(polar, bot, conn):
    rid = agent_card(conn, "laptop")
    shown(polar, rid)
    polar.on_choose(OWNER, rid, "agent")
    polar.on_choose(OWNER, rid, "agent")  # a second click
    rows = polar.tasks.rows("task")
    assert len(rows) == 1
    assert rows[0]["status"] == "queued" and rows[0]["where_hint"] == "laptop"
    assert "Revisa el error" in rows[0]["text"]
    assert "Abrí un agente" in bot.of("chat_update")[-1]["blocks"][-1]["text"]["text"]


def test_choose_agent_defaults_to_server(polar, conn):
    rid = agent_card(conn)
    shown(polar, rid)
    polar.on_choose(OWNER, rid, "agent")
    assert polar.tasks.rows("task")[0]["where_hint"] == "server"


def test_choose_no_makes_no_task(polar, conn):
    rid = agent_card(conn)
    shown(polar, rid)
    polar.on_choose(OWNER, rid, "no")
    polar.on_choose(OWNER, rid, "agent")  # too late
    assert polar.tasks.rows("task") == []


def test_cli_ask_agent(tmp_path, monkeypatch, capsys):
    from polar import cli
    f = tmp_path / "brief.txt"
    f.write_text("Haz X. https://x.slack.com/p1")
    assert cli.main(["ask", "agent", "--title", "Haz X", "--context", "Nadie lo tiene",
                     "--brief-file", str(f), "--channel", "C1", "--reply-to", "5.5",
                     "--where", "laptop"]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    req = db.get(db.connect(), rid)
    assert req["agent_where"] == "laptop" and req["draft_text"].startswith("Haz X")
    assert [o["key"] for o in req["options"]] == ["agent", "no"]
