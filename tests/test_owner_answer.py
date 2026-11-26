import json

from polar import cli, db, tasks
from tests.conftest import OWNER


def run(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_reply_to_owner_dm_becomes_report(conn, polar, bot, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLAR_REQUESTER", "")
    f = tmp_path / "t.md"
    f.write_text("Vamos bien")
    rc, out, err = run(capsys, "ask", "reply", "--channel", db.DEFAULT_OWNER_DM, "--thread", "17.1",
                       "--title", "Estado", "--text-file", str(f), "--requester", "sesion-1")
    assert rc == 0
    rid = json.loads(out)["id"]
    assert "an answer to the owner is a report, not a reply; sent as report " + rid in err
    req = db.get(db.connect(), rid)
    assert req["kind"] == "report" and req["draft_text"] == "Vamos bien"
    assert req["target_thread_ts"] == "17.1" and req["requester"] == "sesion-1"


def test_known_dm_channel_from_meta(conn):
    db.set_meta(conn, "owner_dm", "D999")
    rid = db.create_request(conn, "reply", "T", "", "s1", draft_text="x", target_channel="D999")
    assert db.get(conn, rid)["kind"] == "report"
    rid2 = db.create_request(conn, "reply", "T", "", "s1", draft_text="x", target_channel="C1")
    assert db.get(conn, rid2)["kind"] == "reply"


def test_reply_to_other_person_unchanged(conn):
    rid = db.create_request(conn, "reply", "T", "", "s1", draft_text="x", target_channel="D777")
    assert db.get(conn, rid)["kind"] == "reply"


def test_report_posts_in_thread(polar, bot, conn):
    rid = db.create_request(conn, "report", "R", "", "s1", draft_text="x", target_thread_ts="17.1")
    polar.deliver_pending()
    post = bot.of("chat_postMessage")[-1]
    assert post["thread_ts"] == "17.1"
    assert db.get(conn, rid)["status"] == "noted"
    assert db.get_meta(conn, "owner_dm") == "D1"


def test_instruction_texts_carry_the_rule():
    assert "polar ask reply` is only for a message to ANOTHER person" in tasks.BRIEF
    assert "polar report --thread {thread} --channel {channel}" in tasks.BRIEF
    assert "ANOTHER person" in tasks.RESUME_PROMPT
