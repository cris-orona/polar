"""One status message for each owner task, edited in place."""
import pytest

from polar import db
from polar.tasks import NO_REPORT_TEXT
from tests.test_tasks import env, msg, sh, texts, tk  # noqa: F401  (fixtures)


def posts(bot):
    return bot.of("chat_postMessage")


def test_whole_life_is_one_message(tk, sh, bot, tmp_path):
    tk.on_message(msg("haz algo"))
    tk.tick()  # launch: «Recibido» becomes «Abrí»
    t = tk.rows("task")[0]
    sh.live.remove(t["tmux"])
    tk.tick()  # reap: «Terminó»
    assert len(posts(bot)) == 1  # one notification for the whole task
    assert t["id"] and texts(bot) == [NO_REPORT_TEXT]
    ups = [c["text"] for c in bot.of("chat_update")]
    assert ups[0].startswith("▶️ Abrí") and ups[1] == NO_REPORT_TEXT
    assert all(c["blocks"] for c in bot.of("chat_update"))  # explicit blocks: old ones never stay


def test_same_text_makes_no_slack_call(tk, bot):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    n = len(bot.calls)
    assert tk.status_line(tid, tk.get(tid)["status_text"])
    assert len(bot.calls) == n


def test_failed_edit_posts_a_new_message(tk, bot):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    bot.fail = "chat_update"
    assert tk.status_line(tid, "⏳ En cola: 1 antes")
    assert len(posts(bot)) == 2 and posts(bot)[-1]["text"] == "⏳ En cola: 1 antes"
    assert tk.get(tid)["status_ts"] == "100.2"


def test_old_task_without_ts_posts_once(tk, bot):
    tid = tk._insert("task", "queued", "viejo", "viejo", "D1", "5.5", "5.5")
    assert tk.get(tid)["status_ts"] is None
    assert tk.status_line(tid, "⏳ En cola: 0 antes")
    assert tk.status_line(tid, "🅿️ En pausa. Se reanuda cuando llegue su espera.")
    assert len(posts(bot)) == 1 and len(bot.of("chat_update")) == 1
    assert posts(bot)[0]["thread_ts"] == "5.5"


def test_parked_and_queue_use_the_same_message(tk, sh, bot, tmp_path, monkeypatch):
    tk.on_message(msg("uno"))
    tk.tick()
    t = tk.rows("task")[0]
    sh.live.remove(t["tmux"])
    (tmp_path / "parked").mkdir()
    (tmp_path / "parked" / t["tmux"]).write_text("1")
    tk.tick()
    assert len(posts(bot)) == 1 and texts(bot)[0].startswith("🅿️")


def _report(tk, t, title="Informe de prueba"):
    rid = db.create_request(tk.conn, "report", title, draft_text="Resumen", requester="%s task-%s" % (t["session_id"], t["id"]))
    rid = rid["id"] if isinstance(rid, dict) else rid
    return rid


def test_final_text_links_the_report(tk, sh, bot, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_URL", "https://polar.example/")
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    rid = _report(tk, t)
    sh.live.remove(t["tmux"])
    tk.tick()
    assert texts(bot) == ["✅ Terminó: <https://polar.example/informes/%s|Informe de prueba>" % rid]


def test_report_made_before_the_task_is_not_its_report(tk, sh, bot, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_URL", "https://polar.example/")
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    rid = _report(tk, t, "Informe de otra tarea")
    tk.conn.execute("UPDATE requests SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (rid,))
    sh.live.remove(t["tmux"])
    tk.tick()
    assert texts(bot) == [NO_REPORT_TEXT]


def test_tick_fixes_a_line_when_the_report_comes_late(tk, sh, bot, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_URL", "https://polar.example")
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    sh.live.remove(t["tmux"])
    tk.tick()
    assert texts(bot) == [NO_REPORT_TEXT]
    tk.tick()
    assert texts(bot) == [NO_REPORT_TEXT]  # nothing new: no call
    n = len(bot.calls)
    tk.tick()
    assert len(bot.calls) == n
    _report(tk, t, "Ya hay informe")
    tk.tick()
    assert texts(bot)[0].startswith("✅ Terminó: <https://polar.example/informes/") and "Ya hay informe" in texts(bot)[0]
    assert len(posts(bot)) == 1


def test_old_done_task_is_left_alone(tk, sh, bot, monkeypatch):
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    sh.live.remove(t["tmux"])
    tk.tick()
    tk.update(t["id"], finished_at="2020-01-01T00:00:00Z")
    _report(tk, t)
    n = len(bot.calls)
    tk.tick()
    assert len(bot.calls) == n


def test_laptop_task_goes_through_the_same_line(tk, bot):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.claim(tid, "laptop", "5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "p_9")
    tk.notify_claims()
    tk.task_done(tid)
    tk.notify_claims()
    assert len(posts(bot)) == 1 and texts(bot) == [NO_REPORT_TEXT]


def test_schema_adds_the_columns_to_an_old_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,"
                " title TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert {"status_ts", "status_text"} <= cols
