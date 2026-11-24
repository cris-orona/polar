"""Loud failures: the alarm helper, the canary marker and the wired call sites."""
import json
import stat

import pytest

from polar import alarm, cli, db
from tests.conftest import OWNER

OPTS = [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]


@pytest.fixture
def rec(monkeypatch, tmp_path):
    """A fake alarm command: appends its arguments to a file."""
    out = tmp_path / "alarms.txt"
    script = tmp_path / "fake-alarm"
    script.write_text("#!/bin/sh\necho \"$@\" >> %s\n" % out)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("POLAR_ALARM_BIN", str(script))
    return out


def test_noop_when_unset(monkeypatch):
    monkeypatch.delenv("POLAR_ALARM_BIN", raising=False)
    assert alarm.alarm("k", "t") is False


def test_runs_command_with_key_and_title(rec):
    assert alarm.alarm("polar:x", "Titulo", "texto") is True
    line = rec.read_text()
    assert "--key polar:x" in line and "--title Titulo" in line and "--text texto" in line


def test_unset_only_logs(monkeypatch, caplog):
    monkeypatch.delenv("POLAR_ALARM_BIN", raising=False)
    with caplog.at_level("WARNING", logger="polar"):
        assert alarm.alarm("polar:x", "Titulo") is False
    assert "Titulo" in caplog.text


def test_bad_command_never_raises(monkeypatch):
    monkeypatch.setenv("POLAR_ALARM_BIN", "/no/such/command")
    assert alarm.alarm("k", "t") is False


def test_burst_needs_three_in_window():
    b = alarm.Burst(3, 300)
    assert [b.hit(0), b.hit(10), b.hit(20)] == [False, False, True]
    b2 = alarm.Burst(3, 300)
    assert [b2.hit(0), b2.hit(10), b2.hit(400)] == [False, False, False]  # the old two aged out


def test_approved_reply_that_fails_alarms_at_once(polar, user, conn, rec):
    rid = db.create_request(conn, "reply", "R", "c", "s1", draft_text="hola", target_channel="C1")
    polar.deliver_pending()
    user.fail = "chat_postMessage"
    polar.on_send(OWNER, rid)
    assert db.get(conn, rid)["status"] == "failed"
    assert "--key polar:send-failed" in rec.read_text()


def test_sent_reply_does_not_alarm(polar, conn, rec):
    rid = db.create_request(conn, "reply", "R", "c", "s1", draft_text="hola", target_channel="C1")
    polar.deliver_pending()
    polar.on_send(OWNER, rid)
    assert db.get(conn, rid)["status"] == "sent"
    assert not rec.exists()


def test_recover_alarms_for_interrupted_send(polar, conn, rec):
    rid = db.create_request(conn, "reply", "R", "c", "s1", draft_text="hola", target_channel="C1")
    conn.execute("UPDATE requests SET status='sending' WHERE id=?", (rid,))
    polar.recover()
    assert "polar:send-failed" in rec.read_text()


# -- canary card
def test_canary_card_has_marker_and_sends_no_dm(polar, bot, conn):
    rid = db.create_request(conn, "decision", "canary", "", "s1", options=OPTS, canary=True)
    assert db.get(conn, rid)["extra"]["canary"] is True
    polar.deliver_pending()
    req = db.get(conn, rid)
    assert req["status"] == "shown" and req["dm_ts"] is None
    assert not [c for c in bot.calls if c[0] == "chat_postMessage"]


def test_canary_only_for_decision(conn):
    with pytest.raises(ValueError):
        db.create_request(conn, "reply", "R", "c", "s1", draft_text="x", target_channel="C1", canary=True)


def test_cli_canary_flag(conn, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(db, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "unused"))
    rc = cli.main(["ask", "decision", "--title", "canary", "--option", "a:A", "--option", "b:B",
                   "--requester", "s1 canary", "--canary"])
    assert rc == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    assert db.get(conn, rid)["extra"].get("canary") is True
