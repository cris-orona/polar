import pytest

from polar import blocks, db
from polar.core import MAX_CARD_FAILS


class SlackErr(Exception):
    def __init__(self, code):
        super().__init__("secret xoxb-token")
        self.response = {"error": code}


def fail_post(bot, exc):
    def boom(**kw):
        bot._rec("chat_postMessage", **kw)
        raise exc
    bot.chat_postMessage = boom


def one_reply(conn):
    return db.create_request(conn, "reply", "Responder a Ana", draft_text="x", target_channel="C1",
                             target_thread_ts="55.5")


@pytest.mark.parametrize("exc", [SlackErr("invalid_blocks"), SlackErr("msg_too_long"), TypeError("x")])
def test_counted_failure_stops_after_five(polar, conn, bot, exc):
    rid = one_reply(conn)
    fail_post(bot, exc)
    for _ in range(MAX_CARD_FAILS - 1):
        polar.deliver_pending()
    assert db.get(conn, rid)["status"] == "pending"
    polar.deliver_pending()
    req = db.get(conn, rid)
    assert req["status"] == "failed" and req["decided_at"]
    assert req["result"]["error"] == (exc.response["error"] if hasattr(exc, "response") else "TypeError")
    n = len(bot.of("chat_postMessage"))
    polar.deliver_pending()  # nothing more is tried
    assert len(bot.of("chat_postMessage")) == n


def test_owner_is_told_once(polar, conn, bot):
    rid = one_reply(conn)
    calls = []

    def post(**kw):
        calls.append(kw)
        if "blocks" in kw:
            raise SlackErr("invalid_blocks")
        return {"ts": "1.1"}
    bot.chat_postMessage = post
    for _ in range(MAX_CARD_FAILS):
        polar.deliver_pending()
    notice = calls[-1]
    assert "blocks" not in notice
    assert notice["text"] == ("⚠️ No pude mostrar la tarjeta «Responder a Ana» (invalid_blocks). "
                              "Avisé a la sesión que la pidió.")
    assert db.get(conn, rid)["status"] == "failed"


def test_notice_failure_is_only_logged(polar, conn, bot):
    one_reply(conn)

    def post(**kw):
        raise SlackErr("invalid_blocks")
    bot.chat_postMessage = post
    for _ in range(MAX_CARD_FAILS):
        polar.deliver_pending()  # must not raise


@pytest.mark.parametrize("exc", [SlackErr("ratelimited"), SlackErr("invalid_auth"), SlackErr("not_authed"),
                                 OSError("net"), RuntimeError("boom"), TimeoutError()])
def test_transport_errors_never_count(polar, conn, bot, exc):
    rid = one_reply(conn)
    fail_post(bot, exc)
    for _ in range(MAX_CARD_FAILS + 3):
        polar.deliver_pending()
    assert db.get(conn, rid)["status"] == "pending"


def test_build_failure_counts(polar, conn, monkeypatch):
    rid = one_reply(conn)

    def bad(req, hint=None):
        raise KeyError("x")
    monkeypatch.setattr(blocks, "request_blocks", bad)
    for _ in range(MAX_CARD_FAILS):
        polar.deliver_pending()
    assert db.get(conn, rid)["status"] == "failed"


def test_success_resets_count(polar, conn, bot):
    rid = one_reply(conn)
    good = bot.chat_postMessage
    fail_post(bot, SlackErr("invalid_blocks"))
    for _ in range(MAX_CARD_FAILS - 1):
        polar.deliver_pending()
    bot.chat_postMessage = good
    assert polar.deliver_pending() == 1
    assert db.get(conn, rid)["status"] == "shown" and rid not in polar._card_fails


def test_new_message_does_not_fetch_replies(polar, conn, user):
    rid = db.create_request(conn, "reply", "Nuevo", draft_text="x", target_channel="C1")
    user.conversations_info = lambda **kw: {"channel": {"name": "general"}}
    user.conversations_replies = lambda **kw: user._rec("conversations_replies", **kw)
    polar.fetch_original(db.get(conn, rid))
    assert user.of("conversations_replies") == []
    assert db.get(conn, rid)["orig_channel_name"] == "general"
