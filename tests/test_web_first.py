from datetime import datetime, timedelta, timezone

import pytest

from polar import db
from polar.core import Polar

from .conftest import OWNER

BASE = datetime(2026, 11, 23, 18, 0, 0, tzinfo=timezone.utc)  # 12:00 in Mexico City


@pytest.fixture(autouse=True)
def web(monkeypatch):
    monkeypatch.setenv("POLAR_DELIVERY", "web")
    monkeypatch.setenv("POLAR_WEB_URL", "https://m.test")
    monkeypatch.setenv("POLAR_TZ", "America/Mexico_City")


def card(conn, title="Carta", created=BASE, kind="decision"):
    if kind == "report":
        rid = db.create_request(conn, "report", title, "", "s", draft_text="texto")
    else:
        rid = db.create_request(conn, "decision", title, "c", "s",
                                options=[{"key": "go", "label": "Go"}, {"key": "no", "label": "No"}])
    conn.execute("UPDATE requests SET created_at=? WHERE id=?", (created.strftime("%Y-%m-%dT%H:%M:%SZ"), rid))
    return rid


def shown(conn, **kw):
    rid = card(conn, **kw)
    conn.execute("UPDATE requests SET status='shown' WHERE id=? AND kind!='report'", (rid,))
    return rid


def posts(bot):
    return bot.of("chat_postMessage")


def test_web_mode_delivers_without_slack(polar, bot, conn):
    a = card(conn)
    r = card(conn, kind="report")
    assert polar.deliver_pending() == 2
    assert db.get(conn, a)["status"] == "shown" and not db.get(conn, a)["dm_ts"]
    assert db.get(conn, r)["status"] == "noted"
    assert posts(bot) == [] and bot.of("files_upload_v2") == []
    polar.sync_pending()
    polar.sync_prod_group()
    assert posts(bot) == []


def test_dm_mode_unchanged(monkeypatch, polar, bot, conn):
    monkeypatch.setenv("POLAR_DELIVERY", "dm")
    a = card(conn)
    polar.deliver_pending()
    assert db.get(conn, a)["dm_ts"]
    assert len(posts(bot)) == 1
    polar.sync_reminders(BASE + timedelta(hours=5))
    assert len(posts(bot)) == 1


def test_thresholds_once_each(polar, bot, conn):
    rid = shown(conn)
    polar.sync_reminders(BASE + timedelta(minutes=10))
    assert posts(bot) == []
    for mins, label in ((30, "30 min"), (60, "1 h"), (240, "4 h")):
        polar.sync_reminders(BASE + timedelta(minutes=mins))
        polar.sync_reminders(BASE + timedelta(minutes=mins + 1))
        last = posts(bot)[-1]["text"]
        assert last == "⏰ %s · Carta → <https://m.test/?c=%s|Abrir>" % (label, rid)
    assert len(posts(bot)) == 3
    polar.sync_reminders(BASE + timedelta(minutes=300))
    assert len(posts(bot)) == 3


def test_restart_past_thresholds_sends_highest_once(conn, bot, user):
    rid = shown(conn)
    now = BASE + timedelta(minutes=250)
    Polar(conn, bot, user, OWNER).sync_reminders(now)
    assert len(posts(bot)) == 1 and "4 h" in posts(bot)[0]["text"]
    Polar(conn, bot, user, OWNER).sync_reminders(now + timedelta(minutes=1))  # a new process
    assert len(posts(bot)) == 1


def test_quiet_hours_then_one_morning_batch(polar, bot, conn):
    created = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)  # 21:00 local
    a = shown(conn, title="Uno", created=created)
    b = shown(conn, title="Dos", created=created + timedelta(minutes=20))
    polar.sync_reminders(created + timedelta(hours=1))      # 22:00 local: quiet
    polar.sync_reminders(created + timedelta(hours=8))      # 05:00 local: quiet
    assert posts(bot) == []
    polar.sync_reminders(created + timedelta(hours=11, minutes=5))  # 08:05 local
    assert len(posts(bot)) == 1
    text = posts(bot)[0]["text"]
    assert text.startswith("2 cartas te esperan en Mando")
    assert "⏰ 11 h · Uno" in text and "Dos" in text and a in text and b in text
    polar.sync_reminders(created + timedelta(hours=11, minutes=10))
    assert len(posts(bot)) == 1


def test_batch_in_one_message(polar, bot, conn):
    for t in ("A", "B", "C"):
        shown(conn, title=t)
    polar.sync_reminders(BASE + timedelta(minutes=31))
    assert len(posts(bot)) == 1
    lines = posts(bot)[0]["text"].split("\n")
    assert lines[0] == "3 cartas te esperan en Mando" and len(lines) == 4


def test_reports_never_reminded(polar, bot, conn):
    card(conn, kind="report")
    polar.deliver_pending()
    polar.sync_reminders(BASE + timedelta(hours=5))
    assert posts(bot) == []


def test_reminder_deleted_when_cards_close(polar, bot, conn):
    a = shown(conn)
    b = shown(conn)
    polar.sync_reminders(BASE + timedelta(minutes=31))
    ts = posts(bot)[0]
    conn.execute("UPDATE requests SET status='chosen' WHERE id=?", (a,))
    polar.sync_reminders(BASE + timedelta(minutes=32))
    assert bot.of("chat_delete") == []
    conn.execute("UPDATE requests SET status='chosen' WHERE id=?", (b,))
    polar.sync_reminders(BASE + timedelta(minutes=33))
    assert bot.of("chat_delete") == [{"channel": "D1", "ts": "100.1"}]
    assert db.get_meta(conn, "reminders")["msgs"] == []


def test_post_failure_backs_off_and_retries_later(polar, bot, conn):
    shown(conn)
    bot.fail = "chat_postMessage"
    polar.sync_reminders(BASE + timedelta(minutes=31))
    polar.sync_reminders(BASE + timedelta(minutes=31))
    assert len(posts(bot)) == 1  # backed off
    bot.fail = None
    polar._sync_retry_at.clear()
    polar.sync_reminders(BASE + timedelta(minutes=32))
    assert len(posts(bot)) == 2


def test_say_thread_without_dm_is_safe(polar, bot, conn):
    rid = shown(conn)
    polar._say_thread(db.get(conn, rid), "x")
    assert posts(bot) == []


def test_web_mode_removes_old_pendientes_list(polar, bot, conn):
    db.set_meta(conn, "pendientes", {"ts": "50.1", "channel": "D1", "ids": ["x"]})
    polar.sync_pending()
    assert bot.of("chat_delete") == [{"channel": "D1", "ts": "50.1"}]
    assert db.get_meta(conn, "pendientes") is None
    polar.sync_pending()
    assert len(bot.of("chat_delete")) == 1


def test_reminders_off_posts_nothing_and_still_cleans_up(monkeypatch, polar, bot, conn):
    a = shown(conn)
    polar.sync_reminders(BASE + timedelta(minutes=31))
    assert len(posts(bot)) == 1
    monkeypatch.setenv("POLAR_SLACK_REMINDERS", "off")
    shown(conn)
    polar.sync_reminders(BASE + timedelta(hours=5))
    assert len(posts(bot)) == 1  # no new reminder
    conn.execute("UPDATE requests SET status='chosen' WHERE id=?", (a,))
    polar.sync_reminders(BASE + timedelta(hours=5, minutes=1))
    assert bot.of("chat_delete") == [{"channel": "D1", "ts": "100.1"}]  # the old reminder still goes away
