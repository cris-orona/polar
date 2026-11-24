from polar import db
from polar.tasks import OWNER_DM, Tasks
from tests.conftest import OWNER

NOW = 1000.0


def m(ts, text="hola", **kw):
    d = {"type": "message", "user": OWNER, "text": text, "ts": ts}
    d.update(kw)
    return d


def texts(bot):
    return [c["text"] for c in bot.of("chat_postMessage")]


def test_missed_message_becomes_task(polar, bot, conn):
    bot.history_pages = [[m("900.1", "haz algo")]]
    assert polar.tasks.catch_up(now=NOW) == 1
    t = polar.tasks.rows("task")[0]
    assert t["text"] == "haz algo" and t["msg_ts"] == "900.1"
    assert "Recibido" in texts(bot)[-1]
    assert float(db.get_meta(conn, "dm_caught")) == NOW - 20
    assert bot.of("conversations_history")[0]["channel"] == OWNER_DM


def test_first_round_reads_the_last_hour(polar, bot):
    polar.tasks.catch_up(now=NOW)
    assert float(bot.of("conversations_history")[0]["oldest"]) == NOW - 3600


def test_taken_message_changes_nothing(polar, bot, conn):
    polar.tasks.on_message(dict(m("900.1", "haz algo"), channel=OWNER_DM, channel_type="im"))
    posts = len(bot.of("chat_postMessage"))
    bot.history_pages = [[m("900.1", "haz algo")]]
    assert polar.tasks.catch_up(now=NOW) == 0
    assert len(polar.tasks.rows("task")) == 1 and len(bot.of("chat_postMessage")) == posts


def test_bot_and_other_user_ignored(polar, bot):
    bot.history_pages = [[m("900.1", bot_id="B1"), m("900.2", user="U9")]]
    assert polar.tasks.catch_up(now=NOW) == 0
    assert polar.tasks.rows("task") == []


def test_settle_time(polar, bot, conn):
    bot.history_pages = [[m("985.0", "reciente")]]
    assert polar.tasks.catch_up(now=NOW) == 0
    assert polar.tasks.rows("task") == []
    assert polar.tasks.catch_up(now=NOW + 30) == 1


def test_live_message_does_not_hide_an_older_lost_one(polar, bot, conn):
    """A lost message, then a live one: the next round still takes the lost one."""
    tk = polar.tasks
    tk.catch_up(now=NOW - 300)  # an earlier round: read up to NOW - 320
    tk.on_message(dict(m("950.5", "llegó en vivo"), channel=OWNER_DM, channel_type="im"))
    bot.history_pages = [[m("950.5", "llegó en vivo"), m("900.1", "se perdió")]]
    assert tk.catch_up(now=NOW) == 1
    assert sorted(t["msg_ts"] for t in tk.rows("task")) == ["900.1", "950.5"]
    assert float(bot.of("conversations_history")[-1]["oldest"]) == NOW - 320


def test_marks_move_only_after_a_good_round(polar, bot, conn):
    tk = polar.tasks
    tk.catch_up(now=NOW, threads=False)
    assert float(db.get_meta(conn, "dm_caught")) == NOW - 20
    assert db.get_meta(conn, "dm_caught_threads") is None  # threads were not read

    def boom(**kw):
        raise RuntimeError("x")
    bot.conversations_history = boom
    tk.catch_up(now=NOW + 500)
    assert float(db.get_meta(conn, "dm_caught")) == NOW - 20  # the failed round moved nothing


def test_thread_mark_is_its_own(polar, bot, conn):
    """Top-level rounds run more often. They must not move the mark of the thread reads."""
    tk = polar.tasks
    tid = tk.create_task(OWNER_DM, "800.0", "800.0", "tarea")
    tk.update(tid, status="running", tmux="task-x", host="server")
    tk.catch_up(now=NOW - 600)                    # reads threads up to NOW - 620
    tk.catch_up(now=NOW - 100, threads=False)     # top level only
    bot.replies = {"800.0": [m("800.0"), m("%.1f" % (NOW - 400), "respuesta perdida", thread_ts="800.0")]}
    assert tk.catch_up(now=NOW) == 1
    assert float(bot.of("conversations_replies")[-1]["oldest"]) == NOW - 620


def test_thread_reply_under_running_task_is_steer(polar, bot):
    tk = polar.tasks
    tid = tk.create_task(OWNER_DM, "800.1", "800.1", "tarea")
    tk.update(tid, status="running")
    bot.replies = {"800.1": [m("800.1", "tarea"), m("900.5", "mejor así", thread_ts="800.1")]}
    assert tk.catch_up(now=NOW) == 1
    s = tk.rows("steer")[0]
    assert s["target_task"] == tid and s["text"] == "mejor así" and s["msg_ts"] == "900.5"
    assert tk.catch_up(now=NOW) == 0
    assert len(tk.rows("steer")) == 1


def test_threads_off(polar, bot):
    tk = polar.tasks
    tid = tk.create_task(OWNER_DM, "800.1", "800.1", "tarea")
    tk.update(tid, status="running")
    tk.catch_up(now=NOW, threads=False)
    assert bot.of("conversations_replies") == []


def test_slack_error_does_not_raise(polar, bot):
    bot.fail = "conversations_history"
    assert polar.tasks.catch_up(now=NOW) == 0


def test_paging(polar, bot):
    bot.history_pages = [[m("900.1", "a")], [m("900.2", "b")], [m("900.3", "c")], [m("900.4", "d")]]
    assert polar.tasks.catch_up(now=NOW, threads=False) == 3
    assert len(bot.of("conversations_history")) == 3


def test_tick_schedule(polar, bot):
    tk = polar.tasks
    tk.catch_up_due(now=NOW)
    n = len(bot.of("conversations_history"))
    assert n == 1
    tk.catch_up_due(now=NOW + 100)
    assert len(bot.of("conversations_history")) == n
    tk.catch_up_due(now=NOW + 301)
    assert len(bot.of("conversations_history")) == n + 1
    tid = tk.create_task(OWNER_DM, "800.1", "800.1", "tarea")
    tk.update(tid, status="running")
    tk.catch_up_due(now=NOW + 602)
    assert bot.of("conversations_replies") == []
    tk.catch_up_due(now=NOW + 901)
    assert len(bot.of("conversations_replies")) == 1


def test_one_bad_thread_does_not_stop_the_round(polar, bot, conn):
    tk = polar.tasks
    for parent in ("800.0", "810.0"):
        tid = tk.create_task(OWNER_DM, parent, parent, "tarea " + parent)
        tk.update(tid, status="running", tmux="task-" + parent[:3], host="server")
    conn.execute("UPDATE tasks SET created_at='2026-01-01T00:00:0' || (CASE thread_ts WHEN '810.0' THEN '2Z' ELSE '1Z' END)")

    class Gone(Exception):
        response = {"error": "thread_not_found"}
    good = [m("800.0"), m("905.5", "respuesta", thread_ts="800.0")]

    def replies(**kw):
        bot._rec("conversations_replies", **kw)
        if kw["ts"] == "810.0":
            raise Gone()
        return {"messages": good}
    bot.conversations_replies = replies
    assert tk.catch_up(now=NOW) == 1  # the thread after the bad one was still read
    assert float(db.get_meta(conn, "dm_caught_threads")) == NOW - 20


def test_first_read_is_capped_at_one_day(polar, bot, conn):
    db.set_meta(conn, "dm_caught", "%.6f" % (NOW - 10 * 86400))
    polar.tasks.catch_up(now=NOW, threads=False)
    assert float(bot.of("conversations_history")[-1]["oldest"]) == NOW - 86400
