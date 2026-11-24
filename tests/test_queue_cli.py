import json

import pytest

from polar import cli, db


def run(capsys, monkeypatch, tmp_path, *argv):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    return code, [json.loads(l) for l in out]


def test_ask_reply_and_status(capsys, monkeypatch, tmp_path):
    code, out = run(capsys, monkeypatch, tmp_path, "ask", "reply", "--channel", "C1",
                    "--thread", "1.2", "--text", "Hola", "--title", "T", "--requester", "r")
    assert code == 0
    rid = out[0]["id"]
    _, st = run(capsys, monkeypatch, tmp_path, "status", rid)
    assert st[0]["status"] == "pending" and st[0]["final_text"] is None


def test_ask_reply_text_file(capsys, monkeypatch, tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("desde archivo\n")
    _, out = run(capsys, monkeypatch, tmp_path, "ask", "reply", "--channel", "C1",
                 "--text-file", str(f), "--title", "T", "--requester", "sess-1 pane")
    conn = db.connect(tmp_path / "polar.db")
    assert db.get(conn, out[0]["id"])["draft_text"] == "desde archivo"


def test_ask_decision_options(capsys, monkeypatch, tmp_path):
    _, out = run(capsys, monkeypatch, tmp_path, "ask", "decision", "--title", "T",
                 "--option", "go:Go", "--option", "wait:Esperar", "--requester", "sess-1 pane")
    conn = db.connect(tmp_path / "polar.db")
    assert db.get(conn, out[0]["id"])["options"] == [
        {"key": "go", "label": "Go"}, {"key": "wait", "label": "Esperar"}]


@pytest.mark.parametrize("opts", [["go:Go"], ["a:A", "a:B"], ["a:A", "b:B", "c:C", "d:D", "e:E"], ["bad", "b:B"]])
def test_bad_options_rejected(capsys, monkeypatch, tmp_path, opts):
    argv = ["ask", "decision", "--title", "T", "--requester", "sess-1 pane"]
    for o in opts:
        argv += ["--option", o]
    code, out = run(capsys, monkeypatch, tmp_path, *argv)
    assert code == 2 and "error" in out[0]


def test_reply_too_long_rejected(capsys, monkeypatch, tmp_path):
    code, _ = run(capsys, monkeypatch, tmp_path, "ask", "reply", "--channel", "C1",
                  "--text", "x" * 3000, "--title", "T", "--requester", "sess-1 pane")
    assert code == 2


def test_status_unknown(capsys, monkeypatch, tmp_path):
    code, out = run(capsys, monkeypatch, tmp_path, "status", "nope")
    assert code == 1 and out[0]["error"]


def test_list_filters(conn, reply_id, decision_id):
    db.transition(conn, reply_id, "pending", "shown")
    db.transition(conn, reply_id, "shown", "discarded")
    assert [r["id"] for r in db.list_requests(conn, "pending")] == [decision_id]
    assert [r["id"] for r in db.list_requests(conn, "open")] == [decision_id]
    assert len(db.list_requests(conn, "all")) == 2
    assert db.list_requests(conn, "all", since="2999-01-01T00:00:00Z") == []


def test_cli_never_imports_slack(capsys, monkeypatch, tmp_path):
    import sys
    run(capsys, monkeypatch, tmp_path, "list")
    assert "slack_bolt" not in sys.modules


def test_transition_is_atomic(conn, reply_id):
    assert db.transition(conn, reply_id, "pending", "shown")
    assert not db.transition(conn, reply_id, "pending", "shown")


def test_expiry(conn, reply_id, decision_id, capsys, monkeypatch, tmp_path):
    db.transition(conn, reply_id, "pending", "shown")
    assert db.expire_due(conn, "2000-01-01T00:00:00Z") == []
    ids = db.expire_due(conn, "2999-01-01T00:00:00Z")
    assert sorted(ids) == sorted([reply_id, decision_id])
    assert db.get(conn, reply_id)["status"] == "expired"
    assert db.expire_due(conn, "2999-01-01T00:00:00Z") == []


def test_connection_works_across_threads(tmp_path):
    import threading
    from polar import db as _db
    conn = _db.connect(tmp_path / "t.db")
    errors = []

    def work():
        try:
            for _ in range(50):
                conn.execute("SELECT COUNT(*) FROM requests").fetchone()
        except Exception as exc:  # pragma: no cover - the test fails on any error
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
