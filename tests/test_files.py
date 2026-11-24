import json
import os

import pytest

from polar import cli, files
from polar.tasks import MAX_STEER_CHARS, Tasks, with_note
from tests.conftest import OWNER
from tests.test_tasks import Sh, msg, texts


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("POLAR_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("POLAR_CLAUDE_BIN", "/x/claude")
    monkeypatch.setenv("POLAR_TMUX_BIN", "/x/tmux")
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "0")
    monkeypatch.setenv("POLAR_TRIAGE", "0")


class Fetch:
    """A fake download: writes the file id as the content. Ids in `fail` are refused."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, f, dest):
        self.calls.append(f["id"])
        if f["id"] in self.fail:
            return files.NO_ACCESS
        dest.write_bytes(f["id"].encode())
        return None


@pytest.fixture
def fetch():
    return Fetch()


@pytest.fixture
def tk(conn, bot, fetch):
    return Tasks(conn, bot, OWNER, sh=Sh(), sleep=lambda s: None, fetch=fetch)


def png(fid="F0000001", name="IMG_1988.png", **kw):
    f = {"id": fid, "name": name, "mimetype": "image/png", "filetype": "png", "size": 877206, "mode": "hosted",
         "url_private_download": "https://files.slack.com/files-pri/T1-%s/download/%s" % (fid, name)}
    f.update(kw)
    return f


def test_message_with_image_is_a_task(tk, bot, fetch, tmp_path):
    tk.on_message(msg("mira esta pantalla", ts="20.1", subtype="file_share", files=[png()]))
    t = tk.rows("task")[0]
    assert t["title"] == "mira esta pantalla" and t["msg_ts"] == "20.1"
    assert "Archivos adjuntos del mensaje (1): IMG_1988.png (image/png, 857 KB)" in t["text"]
    assert "`polar files 20.1`" in t["text"]
    saved = tmp_path / "data" / "files" / "20.1" / "IMG_1988.png"
    assert saved.read_bytes() == b"F0000001" and fetch.calls == ["F0000001"]
    assert texts(bot) == ["🧭 Recibido (📎 1 archivo). Abro una sesión…"]


def test_file_without_text_is_a_task(tk, bot):
    tk.on_message(msg("", ts="20.2", subtype="file_share", files=[png(), png("F0000002", "reporte.pdf")]))
    t = tk.rows("task")[0]
    assert t["title"] == "Revisa los archivos adjuntos." and "(2): IMG_1988.png" in t["text"]
    assert "(📎 2 archivos)" in texts(bot)[0]


def test_other_subtypes_stay_ignored(tk, bot, fetch):
    tk.on_message(msg("hola", subtype="message_changed", files=[png()]))
    tk.on_message(msg("hola", subtype="message_deleted"))
    assert tk.rows("task") == [] and bot.calls == [] and fetch.calls == []


def test_duplicate_event_downloads_once(tk, fetch):
    ev = msg("mira", ts="20.3", subtype="file_share", files=[png()])
    tk.on_message(ev)
    tk.on_message(ev)
    assert len(tk.rows("task")) == 1 and fetch.calls == ["F0000001"]


def test_failed_download_still_makes_the_task(conn, bot):
    tk = Tasks(conn, bot, OWNER, sh=Sh(), sleep=lambda s: None, fetch=Fetch(fail={"F0000001"}))
    tk.on_message(msg("mira", ts="20.4", subtype="file_share", files=[png(), png("F0000002", "b.png")]))
    t = tk.rows("task")[0]
    assert "Archivos adjuntos del mensaje (1): b.png" in t["text"]
    assert "que no pude bajar (1): IMG_1988.png (image/png, 857 KB): " + files.NO_ACCESS in t["text"]
    assert "(📎 1 archivo, ⚠️ no pude bajar 1)" in texts(bot)[0]


def test_list_with_file_is_one_task_not_a_pick(tk, conn):
    tk.on_message(msg("- uno\n- dos\n- tres", ts="20.5", subtype="file_share", files=[png()]))
    assert len(tk.rows("task")) == 1 and conn.execute("SELECT count(*) FROM picks").fetchone()[0] == 0


def test_thread_reply_with_file_steers_with_the_note(tk):
    tk.on_message(msg("tarea", ts="30.1"))
    tk.on_message(msg("", ts="30.2", thread="30.1", subtype="file_share", files=[png()]))
    s = tk.rows("steer")[0]
    assert s["target_task"] == tk.rows("task")[0]["id"]
    assert s["text"].startswith("Revisa los archivos adjuntos.") and "`polar files 30.2`" in s["text"]


def test_arrow_steer_with_file(tk):
    tk.on_message(msg("→ 3 usa esta captura", ts="30.3", subtype="file_share", files=[png()]))
    s = tk.rows("steer")[0]
    assert s["target_tab"] == 3 and s["text"].startswith("usa esta captura") and "polar files 30.3" in s["text"]


def test_note_survives_a_long_text():
    note = "Archivos adjuntos del mensaje (1): a.png."
    out = with_note("x" * (MAX_STEER_CHARS + 500), note)
    assert len(out) <= MAX_STEER_CHARS and out.endswith(note)
    assert with_note(" hola ", "") == "hola"


def test_forwarded_message_files_are_read(tk):
    ev = msg("", ts="20.6", attachments=[{"is_share": True, "author_name": "Ben", "text": "ve la foto",
                                           "files": [png("F0000009", "foto.jpg")]}])
    tk.on_message(ev)
    t = tk.rows("task")[0]
    assert "Mensaje reenviado de Ben" in t["text"] and "foto.jpg" in t["text"]


def test_mention_with_file(tk, bot, fetch):
    tk.on_mention({"type": "app_mention", "user": OWNER, "channel": "C1", "ts": "40.1",
                   "text": "<@UBOT> revisa esto", "files": [png()]})
    t = tk.rows("task")[0]
    assert "polar thread C1 40.1" in t["text"] and "`polar files 40.1`" in t["text"] and fetch.calls == ["F0000001"]


def test_save_rules(tmp_path, fetch):
    rows = files.save([
        png("F0000001", "../../etc/pass wd?.png"),
        png("F0000002", "a.png"), png("F0000003", "a.png"),
        png("F0000004", "big.mov", size=files.MAX_BYTES + 1),
        png("F0000005", "gone.png", mode="tombstone"),
        png("F0000006", "doc", mode="external", is_external=True, url_private="https://docs.google.com/d/1"),
    ], "50.1", fetch)
    by = {r["id"]: r for r in rows}
    folder = tmp_path / "data" / "files" / "50.1"
    assert by["F0000001"]["name"] == "pass wd_.png" and os.path.dirname(by["F0000001"]["path"]) == str(folder)
    assert by["F0000002"]["name"] == "a.png" and by["F0000003"]["name"] == "F0000003-a.png"
    assert "MB" in by["F0000004"]["error"] and "ya no está" in by["F0000005"]["error"]
    assert by["F0000006"]["url"].startswith("https://docs.google.com") and "path" not in by["F0000006"]
    assert fetch.calls == ["F0000001", "F0000002", "F0000003"]
    text = files.describe(rows, "50.1")
    assert "(3): pass wd_.png" in text and "no pude bajar (3)" in text and "https://docs.google.com/d/1" in text
    with pytest.raises(ValueError):
        files.save([png()], "../x", fetch)


def test_message_files_dedup_and_empty():
    assert files.message_files({"text": "hola"}) == []
    ev = {"files": [png()], "attachments": [{"files": [png(), png("F0000002")]}]}
    assert [f["id"] for f in files.message_files(ev)] == ["F0000001", "F0000002"]


def test_sweep_deletes_old_folders(tmp_path, fetch):
    files.save([png()], "60.1", fetch)
    old = tmp_path / "data" / "files" / "60.1"
    stamp = old.stat().st_mtime - (files.KEEP_DAYS + 1) * 86400
    os.utime(old, (stamp, stamp))
    files.save([png()], "60.2", fetch)
    assert not old.exists() and (tmp_path / "data" / "files" / "60.2").is_dir()


def test_download_refuses_a_link_outside_slack(tmp_path):
    assert files.http_get("https://evil.example/x.png", "xoxb-secret", tmp_path / "x.png") == "enlace fuera de Slack"
    assert files.http_get("http://files.slack.com/x.png", "xoxb-secret", tmp_path / "x.png") == "enlace fuera de Slack"
    assert not (tmp_path / "x.png").exists()


def test_cli_files_lists_paths(tk, capsys):
    tk.on_message(msg("mira", ts="70.1", subtype="file_share", files=[png()]))
    assert cli.main(["files", "70.1"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "IMG_1988.png" and os.path.isfile(rows[0]["path"]) and rows[0]["size"] == 8
    assert cli.main(["files", "99.9"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "not found"}


def test_fetch_by_id(tmp_path, fetch):
    rows = files.fetch_by_id("F0000007", fetch, info=lambda fid: png(fid, "hilo.png"))
    assert rows[0]["path"].endswith("files/F0000007/hilo.png")
    assert files.fetch_by_id("F0000008", fetch, info=lambda fid: None) is None
    assert files.fetch_by_id("not-an-id", fetch) is None


def test_thread_lists_files():
    from polar import inbox

    class C:
        def conversations_replies(self, **kw):
            return {"messages": [{"ts": "1.0", "user": "U1", "text": "foto", "files": [png()]},
                                 {"ts": "2.0", "user": "U1", "text": "sin archivo"}]}

    out = inbox.thread_messages(C(), "C1", "1.0")
    assert out[0]["files"] == [{"id": "F0000001", "name": "IMG_1988.png", "mimetype": "image/png"}]
    assert "files" not in out[1]


def test_replay_reads_a_missed_message(tk, bot, fetch):
    history = {"messages": [{"ts": "80.1", "user": OWNER, "text": "se perdió", "files": [png()]}]}
    bot.conversations_history = lambda **kw: history
    assert tk.replay("80.1", "D1") is True
    t = tk.rows("task")[0]
    assert t["msg_ts"] == "80.1" and "`polar files 80.1`" in t["text"] and fetch.calls == ["F0000001"]
    assert tk.replay("80.1", "D1") is False and len(tk.rows("task")) == 1  # already taken
    bot.conversations_history = lambda **kw: {"messages": []}
    assert tk.replay("80.2", "D1") is False


def test_key_cannot_leave_the_files_folder(tmp_path, fetch):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "polar.db").write_text("x")
    for key in ("..", ".", ".hidden", "a/b", ""):
        assert files.listing(key) is None
        with pytest.raises(ValueError):
            files.save([png()], key, fetch)


def test_names_that_collide_on_macos_get_the_file_id(tmp_path, fetch):
    rows = files.save([png("F0000001", "Foto.png"), png("F0000002", "foto.PNG"), png("F0000003", "foto.heic")],
                      "90.1", fetch)
    names = [r["name"] for r in rows]
    assert names[0] == "Foto.png" and names[1] == "F0000002-foto.PNG" and names[2].startswith("F0000003-foto.")
    assert len({os.path.splitext(n)[0].lower() for n in names}) == 3


def test_save_failure_keeps_the_message(conn, bot):
    def boom(f, dest):
        raise OSError("disk full")

    tk = Tasks(conn, bot, OWNER, sh=Sh(), sleep=lambda s: None, fetch=boom)
    tk.on_message(msg("mira", ts="91.1", subtype="file_share", files=[png()]))
    t = tk.rows("task")[0]
    assert t["text"].startswith("mira") and "no pude bajar (1)" in t["text"] and "OSError" in t["text"]

    import polar.files as pf
    real = pf.save
    pf.save = lambda *a, **k: (_ for _ in ()).throw(OSError("no folder"))
    try:
        tk.on_message(msg("otra", ts="91.2", subtype="file_share", files=[png()]))
    finally:
        pf.save = real
    t2 = tk.rows("task")[1]
    assert t2["text"].startswith("otra") and "error al guardarlos" in t2["text"]
    assert "(⚠️ no pude bajar 1)" in texts(bot)[1]


def test_plain_arrow_steer_is_still_cut(tk):
    tk.on_message(msg("→ 2 " + "x" * (MAX_STEER_CHARS + 900), ts="92.1"))
    assert len(tk.rows("steer")[0]["text"]) == MAX_STEER_CHARS


def test_long_thread_reply_keeps_the_files_note(tk):
    tk.on_message(msg("tarea", ts="93.1"))
    tk.on_message(msg("y" * (MAX_STEER_CHARS + 900), ts="93.2", thread="93.1", subtype="file_share", files=[png()]))
    s = tk.rows("steer")[0]["text"]
    assert len(s) <= MAX_STEER_CHARS and s.endswith("(las imágenes también).") and "`polar files 93.2`" in s


def test_token_goes_only_to_https_slack_hosts():
    assert files._slack_https("https://files.slack.com/files-pri/T1-F1/download/a.png")
    assert files._slack_https("https://slack.com/x")
    for url in ("http://files.slack.com/a.png", "https://files.slack.com.evil.io/a.png", "https://notslack.com/a",
                "https://evil.io/?x=.slack.com"):
        assert not files._slack_https(url)

    import urllib.request
    handler = files._SlackOnly()
    req = urllib.request.Request("https://files.slack.com/a", headers={"Authorization": "Bearer xoxb-secret"})
    kept = handler.redirect_request(req, None, 302, "Found", {}, "https://files-edge.slack.com/a")
    dropped = handler.redirect_request(req, None, 302, "Found", {}, "http://files.slack.com/a")
    away = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example.com/a")
    assert kept.has_header("Authorization")
    assert not dropped.has_header("Authorization") and not away.has_header("Authorization")
