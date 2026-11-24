import json

from polar import blocks, db
from tests.conftest import OWNER

LATER = "2999-01-01T00:00:00Z"


def mk(conn, kind, created, title="T"):
    if kind == "reply":
        rid = db.create_request(conn, "reply", title, draft_text="x", target_channel="C1")
    elif kind == "decision":
        rid = db.create_request(conn, "decision", title, options=[
            {"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    else:
        rid = db.create_request(conn, "questions", title, options=[{"key": "q", "text": "?"}])
    conn.execute("UPDATE requests SET created_at=? WHERE id=?", (created, rid))
    return rid


def setup(polar, conn, old_kind="reply"):
    old = mk(conn, old_kind, "2026-11-18T10:00:00Z", "old")
    new = mk(conn, "decision", "2026-11-18T10:05:00Z", "new")
    polar.deliver_pending()
    return old, new


def state(conn):
    return db.get_meta(conn, "pendientes")


def test_sweep_never_skips(polar, conn):
    old, new = setup(polar, conn)
    polar.on_choose(OWNER, new, "a")
    polar.sweep()
    assert db.get(conn, old)["status"] == "shown"
    assert not hasattr(db, "skip_due")


def test_passed_over_rules(polar, conn):
    old, new = setup(polar, conn, "questions")
    assert db.passed_over(conn, LATER) == []  # nothing decided
    polar.on_choose(OWNER, new, "a")
    assert [r["id"] for r in db.passed_over(conn, LATER)] == [old]  # questions count
    conn.execute("UPDATE requests SET created_at=? WHERE id=?", (db.now_iso(), old))
    conn.execute("UPDATE requests SET created_at=? WHERE id=?", (db.now_iso(), new))
    assert db.passed_over(conn) == []  # too young


def test_older_decided_does_not_list(polar, conn):
    old, new = setup(polar, conn)
    polar.on_discard(OWNER, old)
    other = mk(conn, "reply", "2026-11-18T09:00:00Z", "older")
    polar.deliver_pending()
    conn.execute("UPDATE requests SET status='sent' WHERE id=?", (other,))
    assert db.passed_over(conn, LATER) == []  # new is shown, nothing newer is decided


def test_reopen_of_skipped_row_still_works(polar, conn, bot):
    old, new = setup(polar, conn)
    conn.execute("UPDATE requests SET status='skipped', decided_at='2026-11-18T11:00:00Z',"
                 " dm_done=1 WHERE id=?", (old,))
    polar.on_choose(OWNER, new, "a")
    polar.on_reopen("U999", old)
    assert db.get(conn, old)["status"] == "skipped"  # not the owner
    polar.on_reopen(OWNER, old)
    r = db.get(conn, old)
    assert r["status"] == "shown" and r["reopened_at"] and r["result"] == {}
    assert any(b["type"] == "actions" for b in bot.of("chat_update")[-1]["blocks"])
    assert db.passed_over(conn) == []  # reopened just now
    assert [r["id"] for r in db.passed_over(conn, LATER)] == [old]


def test_list_all_includes_skipped(polar, conn):
    old, new = setup(polar, conn)
    conn.execute("UPDATE requests SET status='skipped' WHERE id=?", (old,))
    assert old in [r["id"] for r in db.list_requests(conn, "all")]
    assert old in [r["id"] for r in db.list_requests(conn, "skipped")]


def test_pending_blocks_format(polar, conn):
    old, new = setup(polar, conn)
    polar.on_choose(OWNER, new, "a")
    from datetime import datetime
    reqs = db.passed_over(conn, LATER)
    b = blocks.pending_blocks(reqs, datetime(2026, 11, 18, 10, 45))
    text = b[0]["text"]["text"]
    assert text.startswith("⏳ *Pendientes: 1 tarjeta espera tu respuesta*")
    ts = reqs[0]["dm_ts"].replace(".", "")
    assert "• <https://example.slack.com/archives/D1/p%s|old> · hace 45 min" % ts in text
    assert all(x["type"] == "section" for x in b)
    many = [dict(reqs[0], title="t%d" % i) for i in range(17)]
    t = blocks.pending_blocks(many, datetime(2026, 11, 18, 13, 0))[0]["text"]["text"]
    assert "17 tarjetas esperan" in t and "hace 3 h" in t and "… y 2 más" in t
    assert t.count("• ") == 15


# -- sync_pending ------------------------------------------------------------
def ready(polar, conn):
    old, new = setup(polar, conn)
    polar.on_choose(OWNER, new, "a")
    return old, new


def test_sync_no_cards_no_message(polar, conn, bot):
    polar.sync_pending(LATER)
    assert bot.of("chat_postMessage") == [] and state(conn) is None


def test_sync_posts_once(polar, conn, bot):
    old, new = ready(polar, conn)
    before = len(bot.of("chat_postMessage"))
    polar.sync_pending(LATER)
    assert len(bot.of("chat_postMessage")) == before + 1
    st = state(conn)
    assert st["ids"] == [old] and st["posted_ids"] == [old] and st["posted_at"] == LATER
    assert st["channel"] == "D1" and st["ts"]
    n = len(bot.calls)
    polar.sync_pending(LATER)  # same ids: no Slack call
    assert len(bot.calls) == n


def test_sync_removed_updates_in_place(polar, conn, bot):
    old, new = ready(polar, conn)
    extra = mk(conn, "reply", "2026-11-18T10:01:00Z", "extra")
    polar.deliver_pending()
    polar.sync_pending(LATER)
    assert state(conn)["ids"] == [old, extra]
    polar.on_discard(OWNER, extra)
    polar.sync_pending(LATER)
    st = state(conn)
    assert st["ids"] == [old] and st["posted_ids"] == [old, extra]
    assert bot.of("chat_update")[-1]["ts"] == st["ts"] and bot.of("chat_delete") == []


def test_sync_new_card_young_updates_in_place(polar, conn, bot):
    old, new = ready(polar, conn)
    polar.sync_pending(LATER)
    first = state(conn)
    extra = mk(conn, "reply", "2026-11-18T10:01:00Z", "extra")
    polar.deliver_pending()
    polar.sync_pending("2999-01-01T00:10:00Z")  # 10 min after the post
    st = state(conn)
    assert st["ids"] == [old, extra] and st["posted_ids"] == [old]
    assert st["ts"] == first["ts"] and st["posted_at"] == LATER
    assert bot.of("chat_delete") == []
    assert "extra" in bot.of("chat_update")[-1]["blocks"][0]["text"]["text"]


def test_sync_new_card_old_reposts(polar, conn, bot):
    old, new = ready(polar, conn)
    polar.sync_pending(LATER)
    first = state(conn)
    extra = mk(conn, "reply", "2026-11-18T10:01:00Z", "extra")
    polar.deliver_pending()
    later = "2999-01-01T00:31:00Z"
    polar.sync_pending(later)
    st = state(conn)
    assert bot.of("chat_delete") == [{"channel": "D1", "ts": first["ts"]}]
    assert st["ts"] != first["ts"] and st["posted_ids"] == [old, extra] and st["posted_at"] == later


def test_sync_clears_when_none_left(polar, conn, bot):
    old, new = ready(polar, conn)
    polar.sync_pending(LATER)
    ts = state(conn)["ts"]
    polar.on_discard(OWNER, old)
    polar.sync_pending(LATER)
    assert bot.of("chat_delete") == [{"channel": "D1", "ts": ts}] and state(conn) is None


def test_sync_delete_failure_clears_state(polar, conn, bot):
    old, new = ready(polar, conn)
    polar.sync_pending(LATER)
    polar.on_discard(OWNER, old)
    bot.fail = "chat_delete"
    polar.sync_pending(LATER)
    assert state(conn) is None


def test_sync_failed_post_retries(polar, conn, bot):
    old, new = ready(polar, conn)
    bot.fail = "chat_postMessage"
    polar.sync_pending(LATER)  # must not raise
    assert state(conn) is None
    bot.fail = None
    calls = len(bot.calls)
    polar.sync_pending(LATER)  # inside the back-off: no Slack call
    assert state(conn) is None and len(bot.calls) == calls
    polar._sync_retry_at.clear()  # the back-off time passed
    polar.sync_pending(LATER)
    assert state(conn)["ids"] == [old]


def test_sweep_calls_sync(polar, conn, bot):
    old, new = ready(polar, conn)
    conn.execute("UPDATE requests SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (old,))
    polar.sweep()
    assert state(conn)["ids"] == [old]
