"""A card that Slack refuses as too long: Polar cuts the body step by step, and says so when no step fits."""
from polar import blocks, db
from polar.core import MAX_CARD_FAILS

LONG = "palabra " * 1500  # 12,000 characters: longer than every body step


class SlackErr(Exception):
    def __init__(self, code):
        super().__init__("secret xoxb-token")
        self.response = {"error": code}


def long_report(conn, body=LONG):
    return db.create_request(conn, "report", "Informe largo", "", "s1", draft_text=body, expires_hours=72,
                             report={"next": ["Paso A", "Paso B", "Paso C"]})


def body_len(card):
    return max(len(b["text"]) for b in card["blocks"] if b["type"] == "markdown")


def refuse_over(bot, method, limit):
    """Slack refuses a card whose body is longer than `limit`. Other messages pass."""
    real = getattr(bot, method)

    def call(**kw):
        if "blocks" in kw and any(b["type"] == "markdown" for b in kw["blocks"]) and body_len(kw) > limit:
            bot._rec(method, **kw)
            raise SlackErr("msg_blocks_too_long")
        return real(**kw)
    setattr(bot, method, call)


def test_too_long_report_is_cut_until_slack_accepts(polar, conn, bot):
    rid = long_report(conn)
    refuse_over(bot, "chat_postMessage", 6000)
    assert polar.deliver_pending() == 1  # in one tick: no wait, no notice
    posts = bot.of("chat_postMessage")
    assert [body_len(p) for p in posts] == [11000, 9000, 6000]
    req = db.get(conn, rid)
    assert req["status"] == "noted" and req["dm_ts"] and req["compact"] == 2
    assert req["draft_text"] == LONG  # the stored report keeps the full text
    assert rid not in polar._card_fails


def test_later_edit_starts_from_the_accepted_step(polar, conn, bot):
    rid = long_report(conn)
    refuse_over(bot, "chat_postMessage", 9000)
    polar.deliver_pending()
    polar.refresh_dm(rid)
    updates = bot.of("chat_update")
    assert [body_len(u) for u in updates] == [9000]


def test_edit_that_does_not_fit_is_cut_too(polar, conn, bot):
    rid = long_report(conn)
    polar.deliver_pending()  # Slack accepted the full body
    refuse_over(bot, "chat_update", 6000)  # the edit adds a line and no longer fits
    polar.refresh_dm(rid, "una nota")
    assert [body_len(u) for u in bot.of("chat_update")] == [11000, 9000, 6000]
    assert db.get(conn, rid)["compact"] == 2


def test_no_step_fits_then_the_card_fails_and_the_owner_is_told(polar, conn, bot):
    rid = long_report(conn)
    refuse_over(bot, "chat_postMessage", 0)
    for _ in range(MAX_CARD_FAILS - 1):
        polar.deliver_pending()
    assert db.get(conn, rid)["status"] == "pending"
    assert len(bot.of("chat_postMessage")) == (MAX_CARD_FAILS - 1) * len(blocks.BODY_LIMITS)
    polar.deliver_pending()
    req = db.get(conn, rid)
    assert req["status"] == "failed" and req["result"]["error"] == "msg_blocks_too_long"
    assert bot.of("chat_postMessage")[-1]["text"] == (
        "⚠️ No pude mostrar la tarjeta «Informe largo» (msg_blocks_too_long). Avisé a la sesión que la pidió.")


def test_card_with_a_short_body_is_not_sent_again(polar, conn, bot):
    rid = long_report(conn, "corto")

    def post(**kw):
        bot._rec("chat_postMessage", **kw)
        raise SlackErr("msg_blocks_too_long")
    bot.chat_postMessage = post
    polar.deliver_pending()
    assert len(bot.of("chat_postMessage")) == 1  # a shorter body is not possible: one try per tick
    assert db.get(conn, rid)["status"] == "pending" and polar._card_fails[rid] == 1


def test_other_slack_errors_do_not_cut_the_body(polar, conn, bot):
    rid = long_report(conn)

    def post(**kw):
        bot._rec("chat_postMessage", **kw)
        raise SlackErr("ratelimited")
    bot.chat_postMessage = post
    polar.deliver_pending()
    assert len(bot.of("chat_postMessage")) == 1
    assert db.get(conn, rid)["compact"] == 0 and rid not in polar._card_fails


def test_reply_is_never_cut(polar, conn, bot):
    rid = db.create_request(conn, "reply", "Responder a Ana", draft_text="x" * db.MAX_REPLY_CHARS,
                            target_channel="C1")

    def post(**kw):
        bot._rec("chat_postMessage", **kw)
        raise SlackErr("msg_blocks_too_long")
    bot.chat_postMessage = post
    for _ in range(MAX_CARD_FAILS - 1):
        polar.deliver_pending()
    assert len(bot.of("chat_postMessage")) == MAX_CARD_FAILS - 1  # one try per tick: a reply has no steps
    polar.deliver_pending()
    req = db.get(conn, rid)
    assert req["status"] == "failed" and req["compact"] == 0  # counted, then the owner is told


def test_next_level_skips_steps_that_change_nothing():
    rep = {"id": "r1", "kind": "report", "draft_text": "x" * 5000}
    assert blocks.next_level(rep, 0) == 3  # 9000 and 6000 would show the same 5000 characters
    assert blocks.next_level(rep, 3) is None
    assert blocks.next_level({"id": "d1", "kind": "decision"}, 0) is None
    assert blocks.body_level({"compact": 99}) == len(blocks.BODY_LIMITS) - 1
    assert blocks.body_level({"compact": None}) == 0


def cut_notes(req):
    return [b for b in blocks.request_blocks(req)
            if b["type"] == "context" and b["elements"][0]["text"].startswith("✂️")]


def test_cut_report_says_where_the_full_text_is(conn, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_URL", "https://mando.example")
    req = db.get(conn, long_report(conn))
    assert len(cut_notes(req)) == 1
    assert cut_notes(db.get(conn, long_report(conn, "corto"))) == []
    monkeypatch.delenv("POLAR_WEB_URL")
    assert cut_notes(req) == []  # no page to point to
