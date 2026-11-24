"""A button press that finds the card in another state must answer (an ephemeral line) and redraw the card."""
import json

from polar import db
from tests.conftest import OWNER

QS = [{"key": "a", "text": "¿A?", "options": [{"key": "x", "label": "X"}]}]


def shown(polar, rid):
    polar.deliver_pending()


def said(bot):
    return [c["text"] for c in bot.of("chat_postEphemeral")]


def test_send_on_sent_card_names_the_state(polar, bot, reply_id):
    shown(polar, reply_id)
    polar.on_send(OWNER, reply_id)
    n = len(bot.of("chat_update"))
    polar.on_send(OWNER, reply_id)
    assert said(bot) == ["Esta tarjeta ya estaba enviada. No hice nada."]
    assert bot.of("chat_postEphemeral")[0]["user"] == OWNER
    assert bot.of("chat_postEphemeral")[0]["channel"] == "D1"
    assert len(bot.of("chat_update")) == n + 1  # redrawn: the stale buttons go away


def test_discard_and_choose_on_dead_cards(polar, bot, reply_id, decision_id):
    shown(polar, reply_id)
    polar.on_discard(OWNER, reply_id)
    polar.on_discard(OWNER, reply_id)
    polar.on_send(OWNER, reply_id)
    assert said(bot) == ["Esta tarjeta ya estaba descartada. No hice nada."] * 2
    polar.on_choose(OWNER, decision_id, "go")  # still pending, no DM: no channel to answer in
    assert said(bot) == ["Esta tarjeta ya estaba descartada. No hice nada."] * 2
    shown(polar, decision_id)
    polar.on_choose(OWNER, decision_id, "go")
    polar.on_choose(OWNER, decision_id, "wait")
    assert said(bot)[-1] == "Esta tarjeta ya estaba respondida. No hice nada."


def test_reopen_and_replaced(polar, bot, decision_id):
    shown(polar, decision_id)
    db.mark_replaced(polar.conn, decision_id)
    polar.on_choose(OWNER, decision_id, "go")
    assert said(bot) == ["Esta tarjeta ya estaba reemplazada. No hice nada."]
    polar.on_reopen(OWNER, decision_id)
    assert said(bot)[-1] == "Esta tarjeta ya estaba reemplazada. No hice nada."


def test_question_handlers_answer_when_card_is_closed(polar, bot, conn):
    rid = db.create_request(conn, "questions", "Dudas", "", "s", options=QS)
    shown(polar, rid)
    db.transition(conn, rid, "shown", "answered", result_json=json.dumps({"answers": {}}))
    polar.on_q_answer(OWNER, rid, "a", "x")
    polar.on_q_reset(OWNER, rid, "a")
    polar.on_q_accept(OWNER, rid)
    polar.on_q_send(OWNER, rid)
    polar.on_q_other_click(OWNER, rid, "a", "trig")
    polar.on_q_note_click(OWNER, rid, "trig")
    assert said(bot) == ["Esta tarjeta ya estaba respondida. No hice nada."] * 6


def test_web_calls_stay_quiet(polar, bot, reply_id):
    shown(polar, reply_id)
    polar.on_discard(OWNER, reply_id)
    polar.on_discard(OWNER, reply_id, src="web")  # the web API answers its caller itself
    assert said(bot) == []
