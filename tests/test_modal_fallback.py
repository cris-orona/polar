import json
import time

import pytest

from polar import db
from polar.tasks import Tasks
from tests.conftest import OWNER

QS = [
    {"key": "alerta", "text": "¿Alerta?", "options": [{"key": "si", "label": "Sí"}, {"key": "no", "label": "No"}]},
    {"key": "umbral", "text": "¿Qué umbral?"},
]


def make(conn, kind):
    if kind == "questions":
        rid = db.create_request(conn, "questions", "Alertas", "c", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa task-x", options=QS)
    elif kind == "edit":
        rid = db.create_request(conn, "reply", "Responder", "c", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa task-x", draft_text="Hola",
                                target_channel="C1", target_thread_ts="55.5")
    else:
        rid = db.create_request(conn, "decision", "¿Sí?", "c", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa task-x",
                                options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    return rid


def shown(polar, rid):
    polar.deliver_pending()
    return db.get(polar.conn, rid)


def after():
    """A Slack ts later than the moment Polar asked for the text (a reply is always written after it)."""
    return "%.6f" % (time.time() + 5)


def reply(req, text, ts=None, **kw):
    ev = {"type": "message", "channel_type": "im", "channel": req["dm_channel"], "user": OWNER, "text": text,
          "ts": ts or after(), "thread_ts": req["dm_ts"]}
    ev.update(kw)
    return ev


def click(polar, kind, rid):
    if kind == "revise":
        polar.on_revise_click(OWNER, rid, "trig")
    elif kind == "edit":
        polar.on_edit_click(OWNER, rid, "trig")
    elif kind == "qother":
        polar.on_q_other_click(OWNER, rid, "umbral", "trig")
    else:
        polar.on_q_note_click(OWNER, rid, "trig")


CASES = [("revise", "decision"), ("edit", "edit"), ("qother", "questions"), ("qnote", "questions")]


def thread_posts(bot, req):
    return [c["text"] for c in bot.of("chat_postMessage") if c.get("thread_ts") == req["dm_ts"]]


def check_result(conn, kind, rid, text):
    r = db.get(conn, rid)
    if kind == "revise":
        assert r["status"] == "revise" and r["result"]["instructions"] == text
    elif kind == "edit":
        assert r["final_text"] == text
    elif kind == "qother":
        assert r["result"]["answers"]["umbral"] == {"option": None, "text": text}
    else:
        assert r["result"]["note"] == text


@pytest.mark.parametrize("kind,rk", CASES)
def test_fallback_applies_thread_reply(polar, conn, bot, kind, rk):
    rid = make(conn, rk)
    req = shown(polar, rid)
    bot.views_fail = True
    click(polar, kind, rid)
    notice = thread_posts(bot, req)[-1]
    assert notice.startswith("No pude abrir la ventana. Escribe aquí, en este hilo, ")
    if kind == "qother":
        assert "pregunta 2" in notice
    fb = db.get_meta(conn, "fallback:" + rid)
    assert fb["kind"] == kind and fb["at"]
    ev = reply(req, "texto nuevo")
    polar.on_message(ev)
    check_result(conn, kind, rid, "texto nuevo")
    assert db.get_meta(conn, "fallback:" + rid) is None
    assert thread_posts(bot, req)[-1] == "✅ Tomado."
    steers = polar.tasks.rows("steer")
    assert len(steers) == 1 and steers[0]["status"] == "delivered" and steers[0]["notified"] == 1
    assert steers[0]["msg_ts"] == ev["ts"] and not steers[0]["target_task"] and not steers[0]["requester"]
    posts = len(bot.of("chat_postMessage"))
    polar.on_message(ev)  # same event again
    assert len(bot.of("chat_postMessage")) == posts and len(polar.tasks.rows("steer")) == 1
    check_result(conn, kind, rid, "texto nuevo")
    polar.tasks.catch_up(now=time.time() + 100)  # a replay changes nothing either
    assert len(polar.tasks.rows("steer")) == 1


def test_old_fallback_goes_the_normal_way(polar, conn, bot):
    rid = make(conn, "decision")
    req = shown(polar, rid)
    tid = polar.tasks.create_task("D1", "9.9", "9.9", "tarea")
    polar.tasks.update(tid, session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", status="running", tmux="task-x")
    bot.views_fail = True
    click(polar, "revise", rid)
    db.set_meta(conn, "fallback:" + rid, {"kind": "revise", "q": None, "at": "2020-01-01T00:00:00Z"})
    polar.on_message(reply(req, "tarde"))
    assert db.get(conn, rid)["status"] == "shown"
    assert db.get_meta(conn, "fallback:" + rid) is None
    s = polar.tasks.rows("steer")[0]
    assert s["status"] == "new"


def test_modal_works_no_notice(polar, conn, bot):
    rid = make(conn, "decision")
    req = shown(polar, rid)
    click(polar, "revise", rid)
    assert bot.of("views_open") and thread_posts(bot, req) == []
    assert db.get_meta(conn, "fallback:" + rid) is None


def test_non_owner_reply_ignored(polar, conn, bot):
    rid = make(conn, "decision")
    req = shown(polar, rid)
    bot.views_fail = True
    click(polar, "revise", rid)
    polar.on_message(reply(req, "hack", user="U9"))
    assert db.get(conn, rid)["status"] == "shown"
    assert db.get_meta(conn, "fallback:" + rid) is not None


def test_error_keeps_fallback(polar, conn, bot):
    rid = make(conn, "decision")
    req = shown(polar, rid)
    bot.views_fail = True
    click(polar, "revise", rid)
    polar.on_message(reply(req, "x" * (db.MAX_REPLY_CHARS + 1)))
    assert thread_posts(bot, req)[-1].startswith("⚠️ Máximo")
    assert db.get(conn, rid)["status"] == "shown"
    assert db.get_meta(conn, "fallback:" + rid) is not None


def test_notice_failure_does_not_raise(polar, conn, bot):
    rid = make(conn, "decision")
    shown(polar, rid)
    bot.views_fail = True
    bot.fail = "chat_postMessage"
    click(polar, "revise", rid)
    assert db.get_meta(conn, "fallback:" + rid) is not None


def test_reply_written_before_the_ask_is_not_the_answer(polar, conn, bot):
    """Catch-up can replay an older reply of the same thread. It must go the normal way."""
    rid = make(conn, "decision")
    req = shown(polar, rid)
    bot.views_fail = True
    click(polar, "revise", rid)
    polar.on_message(reply(req, "un mensaje viejo", ts="%.6f" % (time.time() - 600)))
    assert db.get(conn, rid)["status"] == "shown"
    assert db.get_meta(conn, "fallback:" + rid) is not None
    assert "✅ Tomado." not in thread_posts(bot, req)


def test_modal_that_opens_later_clears_the_marker(polar, conn, bot):
    """A failed try, then the window opens: the next thread reply is a normal message again."""
    rid = make(conn, "questions")
    req = shown(polar, rid)
    bot.views_fail = True
    click(polar, "qnote", rid)
    assert db.get_meta(conn, "fallback:" + rid) is not None
    bot.views_fail = False
    click(polar, "qnote", rid)
    assert db.get_meta(conn, "fallback:" + rid) is None
    polar.on_message(reply(req, "esto es para la sesión"))
    assert "✅ Tomado." not in thread_posts(bot, req)
    assert (db.get(conn, rid)["result"] or {}).get("note") is None
