"""`polar report --silent`: stored as a posted report, never sent to Slack."""
import json

import pytest

from polar import cli, db, webapi
from tests.conftest import OWNER

SID = "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


@pytest.fixture(autouse=True)
def _data(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))


def silent(conn, requester="s1", **rep):
    return db.create_request(conn, "report", "Aviso", "", requester, draft_text="hecho", report=rep, silent=True)


def test_stored_as_noted_without_dm(conn):
    rid = silent(conn)
    req = db.get(conn, rid)
    assert (req["status"], req["dm_done"], req["dm_ts"], req["dm_channel"]) == ("noted", 1, None, None)
    assert req["silent"] == 1  # Mando reads this column to keep machine notices out of Foco
    other = db.create_request(conn, "report", "Informe", "", "s1", draft_text="hecho")
    assert db.get(conn, other)["silent"] == 0
    with pytest.raises(ValueError):
        db.create_request(conn, "decision", "t", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                          silent=True)
    with pytest.raises(ValueError):
        silent(conn, files=["/x"])


def test_deliver_sweep_and_fold_never_call_slack(polar, bot, conn):
    rid = silent(conn)
    assert polar.deliver_pending() == 0
    polar.refresh_dm(rid)
    polar.sweep()
    assert bot.calls == []
    conn.execute("UPDATE requests SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (rid,))
    assert db.fold_reports(conn) == [rid]
    polar.sweep()
    assert bot.calls == [] and db.get(conn, rid)["status"] == "filed"


def test_catch_up_skips_a_row_with_null_ts(polar, bot, conn):
    silent(conn)
    assert polar.tasks._catch_up_threads() == []
    polar.tasks.catch_up(now=2_000_000_000.0)
    assert bot.of("conversations_replies") == []


def test_cli_prints_id_and_refuses_file(capsys, tmp_path):
    f = tmp_path / "r.md"
    f.write_text("hola")
    att = tmp_path / "a.pdf"
    att.write_text("x")
    assert cli.main(["report", "--title", "T", "--text-file", str(f), "--silent", "--next", "Sigue"]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    req = db.get(db.connect(), rid)
    assert req["status"] == "noted" and req["dm_ts"] is None and req["report"]["next"] == ["Sigue"]
    assert cli.main(["report", "--title", "T", "--text-file", str(f), "--silent", "--file", str(att)]) == 2
    assert cli.main(["status", rid]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "noted"
    assert cli.main(["list", "--status", "noted"]) == 0
    assert rid in capsys.readouterr().out


def test_web_actions_on_a_silent_report(polar, bot, conn):
    rid = silent(conn, requester=SID + " task-abc", next=["Sigue"], resume=SID)
    code, out = webapi.perform(polar, {"id": rid, "action": "next", "index": 0})
    assert code == 200 and db.get(conn, rid)["status"] == "revise"
    assert bot.calls == []  # a session reads it: no task, no Slack
    rid2 = silent(conn, resume=SID)
    code, _ = webapi.perform(polar, {"id": rid2, "action": "choose", "option": "ok"})
    assert code == 200 and db.get(conn, rid2)["status"] == "chosen"
    assert bot.calls == []


def test_next_on_a_silent_report_with_no_reader_starts_a_task(polar, bot, conn):
    rid = silent(conn, requester="watchdog", next=["Revisa esto"])
    code, _ = webapi.perform(polar, {"id": rid, "action": "next", "index": 0})
    assert code == 200
    task = conn.execute("SELECT * FROM tasks WHERE kind='task'").fetchone()
    posts = bot.of("chat_postMessage")
    assert len(posts) == 1 and "thread_ts" not in posts[0]  # the notice is the thread root
    assert task["thread_ts"] == "100.1"


def _run(tmp_path, *extra):
    f = tmp_path / "r.md"
    f.write_text("hola")
    assert cli.main(["report", "--title", "T", "--text-file", str(f), *extra]) == 0


def test_report_that_asks_nothing_is_stored_silent(capsys, tmp_path):
    _run(tmp_path)
    req = db.get(db.connect(), json.loads(capsys.readouterr().out)["id"])
    assert req["silent"] == 1 and req["status"] == "noted" and req["dm_done"] == 1


def test_report_with_an_ask_stays_a_card(capsys, tmp_path):
    for extra in (["--next", "Sigue"], ["--resume", SID]):
        _run(tmp_path, *extra)
        req = db.get(db.connect(), json.loads(capsys.readouterr().out)["id"])
        assert req["silent"] == 0 and req["status"] == "pending"


def test_card_flag_keeps_the_card(capsys, tmp_path):
    _run(tmp_path, "--card")
    req = db.get(db.connect(), json.loads(capsys.readouterr().out)["id"])
    assert req["silent"] == 0 and req["status"] == "pending"


def test_report_with_a_file_stays_a_card(capsys, tmp_path):
    att = tmp_path / "a.pdf"
    att.write_text("x")
    _run(tmp_path, "--file", str(att))
    req = db.get(db.connect(), json.loads(capsys.readouterr().out)["id"])
    assert req["silent"] == 0
