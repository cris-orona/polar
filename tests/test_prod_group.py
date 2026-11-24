import json
from datetime import datetime, timedelta, timezone

from polar import blocks, db

from tests.conftest import OWNER

GO_NO = [{"key": "go", "label": "Go"}, {"key": "no", "label": "No"}]


def step(conn, title="Producción: paso", requester="prod-op abc", options=GO_NO, kind="decision", n=[0]):
    n[0] += 1
    rid = db.create_request(conn, kind, title + " " + str(n[0]), "ctx", requester, options=options)
    assert db.transition(conn, rid, "pending", "shown", dm_channel="D1", dm_ts="200.%d" % n[0])
    return rid


def later(seconds=60):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def state(conn):
    return db.get_meta(conn, "prod_group")


def test_one_card_no_message(polar, conn, bot):
    step(conn)
    polar.sync_prod_group(later())
    assert not bot.of("chat_postMessage") and state(conn) is None


def test_two_cards_message_and_button(polar, conn, bot):
    a, b = step(conn), step(conn)
    polar.sync_prod_group(later())
    (post,) = bot.of("chat_postMessage")
    st = state(conn)
    assert st["ids"] == [a, b] and st["channel"] == "D1" and len(st["gid"]) == 8
    text = json.dumps(post["blocks"], ensure_ascii=False)
    assert "2 esperan tu Go" in text and text.count("• <") == 2 and "paso" in text
    assert "Producción: paso" not in text
    assert "archives/D1/p200" in text
    button = post["blocks"][-1]["elements"][0]
    assert button["value"] == st["gid"] and button["action_id"] == "polar_group_go"
    assert button["style"] == "primary" and "confirm" in button
    polar.sync_prod_group(later())  # same ids: nothing new
    assert len(bot.of("chat_postMessage")) == 1


def test_debounce(polar, conn, bot):
    step(conn), step(conn)
    polar.sync_prod_group()  # the cards are only seconds old
    assert not bot.of("chat_postMessage") and state(conn) is None


def test_third_card_replaces_message(polar, conn, bot):
    step(conn), step(conn)
    polar.sync_prod_group(later())
    old = state(conn)
    c = step(conn)
    polar.sync_prod_group(later())
    new = state(conn)
    assert new["gid"] != old["gid"] and new["ids"][-1] == c and len(new["ids"]) == 3
    assert bot.of("chat_delete") == [{"channel": "D1", "ts": old["ts"]}]
    assert len(bot.of("chat_postMessage")) == 2


def test_decided_alone_updates_then_deletes(polar, conn, bot):
    a, b, c = step(conn), step(conn), step(conn)
    polar.sync_prod_group(later())
    gid = state(conn)["gid"]
    polar.on_choose(OWNER, a, "go")
    polar.sync_prod_group(later())
    st = state(conn)
    assert st["gid"] == gid and st["ids"] == [b, c]
    assert bot.of("chat_update")[-1]["blocks"][0]["text"]["text"].count("2 esperan") == 1
    polar.on_choose(OWNER, b, "no")
    polar.sync_prod_group(later())
    assert state(conn) is None and len(bot.of("chat_delete")) == 1


def test_click_approves_the_listed_steps(polar, conn, bot):
    a, b = step(conn), step(conn)
    polar.sync_prod_group(later())
    st = state(conn)
    bot.calls.clear()
    polar.on_group_go(OWNER, st["gid"])
    for rid in (a, b):
        req = db.get(conn, rid)
        assert req["status"] == "chosen"
        assert req["result"] == {"option": "go", "via": "group"}
    updates = bot.of("chat_update")
    assert [u["ts"] for u in updates if u["ts"] == st["ts"]] == [st["ts"]]
    done = [u for u in updates if u["ts"] == st["ts"]][0]
    assert "Diste Go a 2 pasos" in done["blocks"][0]["text"]["text"]
    assert len(updates) == 3 and state(conn) is None


def test_click_skips_a_step_decided_alone(polar, conn, bot):
    a, b = step(conn), step(conn)
    polar.sync_prod_group(later())
    gid = state(conn)["gid"]
    polar.on_choose(OWNER, a, "no")
    polar.on_group_go(OWNER, gid)
    assert db.get(conn, a)["result"] == {"option": "no"}
    assert db.get(conn, b)["result"]["via"] == "group"
    assert "Diste Go a 1 paso de" in bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]


def test_old_gid_does_nothing(polar, conn, bot):
    a, b = step(conn), step(conn)
    polar.sync_prod_group(later())
    polar.on_group_go(OWNER, "oldgid00")
    assert db.get(conn, a)["status"] == "shown" and state(conn) is not None


def test_other_user_does_nothing(polar, conn, bot):
    a, b = step(conn), step(conn)
    polar.sync_prod_group(later())
    polar.on_group_go("U_OTHER", state(conn)["gid"])
    assert db.get(conn, a)["status"] == "shown" and state(conn) is not None


def test_late_card_is_not_approved(polar, conn, bot):
    a, b = step(conn), step(conn)
    polar.sync_prod_group(later())
    gid = state(conn)["gid"]
    c = step(conn)
    polar.on_group_go(OWNER, gid)
    assert db.get(conn, c)["status"] == "shown"
    assert db.get(conn, a)["status"] == "chosen"


def test_other_cards_never_grouped(polar, conn, bot):
    step(conn, requester="sess-1 pane")
    step(conn, requester="sess-2 pane")
    step(conn, options=[{"key": "go", "label": "Go"}, {"key": "wait", "label": "Esperar"}])
    step(conn, options=GO_NO + [{"key": "x", "label": "X"}])
    only = step(conn)
    assert [r["id"] for r in polar.prod_group_cards()] == [only]
    polar.sync_prod_group(later())
    assert not bot.of("chat_postMessage")


def test_failed_post_keeps_state_and_retries(polar, conn, bot):
    step(conn), step(conn)
    bot.fail = "chat_postMessage"
    polar.sync_prod_group(later())
    assert state(conn) is None
    bot.fail = None
    calls = len(bot.calls)
    polar.sync_prod_group(later())  # inside the back-off: no Slack call
    assert state(conn) is None and len(bot.calls) == calls
    polar._sync_retry_at.clear()    # the back-off time passed
    polar.sync_prod_group(later())
    assert state(conn) is not None


def test_group_message_that_is_gone_is_posted_again(polar, conn, bot):
    a, b, c = step(conn), step(conn), step(conn)
    polar.sync_prod_group(later())
    first = state(conn)
    conn.execute("UPDATE requests SET status='chosen' WHERE id=?", (c,))

    class Gone(Exception):
        response = {"error": "message_not_found"}

    def gone(**kw):
        raise Gone()
    real = bot.chat_update
    bot.chat_update = gone
    polar.sync_prod_group(later())
    assert state(conn) is None  # no loop on a message that does not exist
    bot.chat_update = real
    polar.sync_prod_group(later())
    assert state(conn)["ids"] == [a, b] and state(conn)["gid"] != first["gid"]


def test_many_steps_are_capped(polar, conn, bot):
    reqs = [step(conn) for _ in range(23)]
    polar.sync_prod_group(later())
    st = state(conn)
    assert st["ids"] == reqs[:20]
    text = json.dumps(bot.of("chat_postMessage")[0]["blocks"], ensure_ascii=False)
    assert "… y 3 más en sus tarjetas" in text and "20 esperan" in text


def test_done_blocks_singular():
    assert "1 paso de" in blocks.prod_group_done(1)[0]["text"]["text"]
