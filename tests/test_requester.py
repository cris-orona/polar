import json

import pytest

from polar import cli, db


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    return tmp_path


def test_explicit_requester_kept(monkeypatch):
    monkeypatch.setenv("POLAR_SESSION_ID", "other")
    assert cli.resolve_requester("abc pane-1") == "abc pane-1"
    assert cli.resolve_requester("watchdog") == "watchdog"


def test_unknown_session_is_replaced(monkeypatch):
    monkeypatch.setenv("POLAR_SESSION_ID", "sid-1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1-2")
    assert cli.resolve_requester("unknown-session w9") == "sid-1 w1-2"
    assert cli.resolve_requester("  ") == "sid-1 w1-2"


def test_env_with_tmux_name(monkeypatch):
    monkeypatch.setenv("POLAR_SESSION_ID", "sid-2")
    monkeypatch.setenv("TMUX", "/tmp/tmux,1,0")

    class R:
        returncode = 0
        stdout = "mi-sesion\n"
    monkeypatch.setattr(cli.shutil, "which", lambda n: "/usr/bin/tmux")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: R())
    assert cli.resolve_requester("") == "sid-2 mi-sesion"


def test_session_id_alone(monkeypatch):
    monkeypatch.setenv("POLAR_SESSION_ID", "sid-3")
    assert cli.resolve_requester("") == "sid-3"


def test_parent_pid_session_file(monkeypatch, tmp_path):
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "300.json").write_text(json.dumps({"sessionId": "sid-file"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.os, "getppid", lambda: 100)
    chain = {100: 200, 200: 300}
    monkeypatch.setattr(cli, "_parent_pid", lambda pid: chain.get(pid))
    monkeypatch.setenv("HERDR_PANE_ID", "w2-1")
    assert cli.resolve_requester("") == "sid-file w2-1"


def test_no_session_found(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.resolve_requester("") == ""


def _count(tmp_path):
    return len(db.list_requests(db.connect(tmp_path / "polar.db")))


def _qfile(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"key": "k", "text": "¿?", "options": []}]))
    return str(f)


def test_refusal_for_each_kind(data, monkeypatch, capsys):
    cases = [
        ["ask", "reply", "--channel", "C1", "--title", "T", "--text", "Hola"],
        ["ask", "decision", "--title", "T", "--option", "a:A", "--option", "b:B"],
        ["ask", "questions", "--title", "T", "--question-file", _qfile(data)],
        ["ask", "email", "--thread", "T1", "--title", "T", "--text", "Hola"],
    ]
    for argv in cases:
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        assert "names no session" in err and "--requester" in err
    assert _count(data) == 0


def test_agent_and_report_pass_without_session(data, capsys):
    brief = data / "b.txt"
    brief.write_text("haz X")
    assert cli.main(["ask", "agent", "--title", "Haz X", "--context", "Nadie lo tiene",
                     "--brief-file", str(brief)]) == 0
    text = data / "r.md"
    text.write_text("Listo")
    assert cli.main(["report", "--title", "R", "--text-file", str(text)]) == 0
    reqs = db.list_requests(db.connect(data / "polar.db"))
    assert len(reqs) == 2 and all(r["requester"] == "" for r in reqs)


def test_report_and_ask_use_env_session(data, monkeypatch, capsys):
    monkeypatch.setenv("POLAR_SESSION_ID", "sid-9")
    monkeypatch.setenv("HERDR_PANE_ID", "w3-1")
    assert cli.main(["ask", "decision", "--title", "T", "--option", "a:A", "--option", "b:B",
                     "--requester", "unknown-session w3-1"]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    assert db.get(db.connect(data / "polar.db"), rid)["requester"] == "sid-9 w3-1"
