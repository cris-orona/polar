"""POLAR_TASK_NOTICES: no Slack notice for each task (default off in web delivery)."""
import pytest

from polar import db, webapi
from polar.core import Polar
from polar.tasks import BRIEF, NO_REPORT_TEXT, OWNER_DM, fill_thread
from tests.conftest import OWNER
from polar.tasks import Tasks
from tests.test_status_answer import SaSh
from tests.test_tasks import env, msg, sh, texts, tk  # noqa: F401  (fixtures)


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setenv("POLAR_TASK_NOTICES", "off")


@pytest.fixture
def satk(conn, bot, monkeypatch):
    """Tasks with a fake status-answer and the answer worker held back."""
    monkeypatch.setenv("POLAR_TRIAGE", "1")
    t = Tasks(conn, bot, OWNER, sh=SaSh(), sleep=lambda s: None)
    t.pending = []
    t.spawn = t.pending.append
    return t


def posts(bot):
    return bot.of("chat_postMessage")


def test_default_off_in_web_delivery(tk, monkeypatch):
    monkeypatch.delenv("POLAR_TASK_NOTICES", raising=False)
    monkeypatch.setenv("POLAR_DELIVERY", "web")
    assert tk.notices_on() is False


def test_default_on_without_delivery_env(tk, monkeypatch):
    monkeypatch.delenv("POLAR_TASK_NOTICES", raising=False)
    monkeypatch.delenv("POLAR_DELIVERY", raising=False)
    assert tk.notices_on() is True
    monkeypatch.setenv("POLAR_DELIVERY", "dm")
    assert tk.notices_on() is True


def test_explicit_value_wins(tk, monkeypatch):
    monkeypatch.setenv("POLAR_DELIVERY", "web")
    monkeypatch.setenv("POLAR_TASK_NOTICES", "on")
    assert tk.notices_on() is True
    monkeypatch.setenv("POLAR_DELIVERY", "dm")
    monkeypatch.setenv("POLAR_TASK_NOTICES", "off")
    assert tk.notices_on() is False


def test_mando_task_posts_nothing(polar, tk, bot, conn, quiet):
    polar.tasks = tk
    code, out = webapi.new_task(polar, {"action": "task", "text": "Revisa el drift"})
    assert code == 200 and out == {"ok": True, "status": "queued", "task": out["task"]}
    assert posts(bot) == []
    t = tk.get(out["task"])
    assert not t["thread_ts"] and t["msg_ts"].startswith("web:") and len(t["msg_ts"]) > 10
    code2, out2 = webapi.new_task(polar, {"action": "task", "text": "Revisa el drift"})
    assert code2 == 200 and tk.get(out2["task"])["msg_ts"] != t["msg_ts"]


def test_mando_task_loud_when_on(polar, tk, bot, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_NOTICES", "on")
    polar.tasks = tk
    webapi.new_task(polar, {"action": "task", "text": "Revisa el drift"})
    assert texts(bot)[0].startswith("🖥️ Desde Mando") and "Recibido desde Mando" in texts(bot)[1]


def test_status_line_stores_text_but_posts_nothing(tk, bot, quiet):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    assert tk.status_line(tid, "⏳ En cola: 1 antes") is True
    assert posts(bot) == [] and not bot.of("chat_update")
    assert tk.get(tid)["status_text"] == "⏳ En cola: 1 antes" and tk.get(tid)["status_ts"] is None


def test_finish_posts_no_done_line(tk, sh, bot, quiet):
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    sh.live.remove(t["tmux"])
    tk.tick()
    assert tk.get(t["id"])["status"] == "done"
    assert posts(bot) == [] and not bot.of("chat_update")
    assert tk.get(t["id"])["status_text"] == NO_REPORT_TEXT


def test_mention_posts_no_head_and_keeps_link(tk, bot, quiet):
    tk.on_mention({"type": "app_mention", "user": OWNER, "channel": "C1", "ts": "40.1",
                   "thread_ts": "39.0", "text": "<@UBOT> revisa esto"})
    t = tk.rows("task")[0]
    assert posts(bot) == []
    assert not t["thread_ts"] and t["msg_ts"] == "40.1"
    assert "https://example.slack.com/archives/C1/p401" in t["text"]


def test_mention_loud_when_on(tk, bot, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_NOTICES", "on")
    tk.on_mention({"type": "app_mention", "user": OWNER, "channel": "C1", "ts": "40.1", "text": "<@UBOT> revisa"})
    assert texts(bot)[0].startswith("🧭 Me etiquetaste") and tk.rows("task")[0]["thread_ts"] == "100.1"


def test_confirm_card_still_posts_top_level(tk, bot, quiet, monkeypatch):
    monkeypatch.setenv("POLAR_TRIAGE", "1")
    tid = tk.create_task(OWNER_DM, "", "web:abc", "algo", intake=True)
    tk.triage_set(tid, "S1", "tab 3", "medium", "parece lo mismo")
    tk.tick()
    call = posts(bot)[-1]
    assert call["text"].startswith("¿Es de tab 3") and "thread_ts" not in call
    assert tk.get(tid)["status"] == "confirm" and tk.get(tid)["card_ts"]


def test_error_notice_still_posts(tk, bot, quiet):
    tid = tk.create_task(OWNER_DM, "", "web:abc", "algo")
    t = tk.get(tid)
    tk.claude_bin = lambda: None
    assert tk.launch(t) is False
    assert posts(bot)[-1]["text"].startswith("⚠️ No encuentro claude") and "thread_ts" not in posts(bot)[-1]


def test_brief_without_thread_has_no_thread_flag():
    out = fill_thread(BRIEF, "", id="x1", text="hola", channel="D1", session="s",
                      polar_bin="polar", where="a server", extra_rules="")
    assert "--thread" not in out and "polar report --channel D1" in out
    out2 = fill_thread(BRIEF, "5.5", id="x1", text="hola", channel="D1", session="s",
                      polar_bin="polar", where="a server", extra_rules="")
    assert "--thread 5.5 --channel D1" in out2


def test_launch_brief_of_threadless_task(tk, sh, tmp_path, quiet):
    tid = tk.create_task(OWNER_DM, "", "web:abc", "algo")
    tk.tick()
    assert tk.get(tid)["status"] == "running"
    brief = (tmp_path / "data" / "briefs" / ("task-" + tid + ".txt")).read_text()
    assert "--thread" not in brief and "None" not in brief.split("polar report")[1][:60]


def test_status_question_answer_in_thread_without_done_line(satk, bot, quiet, monkeypatch):
    monkeypatch.setenv("POLAR_DELIVERY", "web")
    satk.on_message(msg("cómo va el CI"))
    satk.pending[0]()
    got = texts(bot)
    assert any("Va bien" in x for x in got)
    assert not any("Te respondí" in x for x in got) and not any(x.startswith("🧭") for x in got)


def test_status_question_without_thread_posts_nothing(satk, bot, quiet, monkeypatch):
    monkeypatch.setenv("POLAR_DELIVERY", "web")
    tid = satk.create_task(OWNER_DM, "", "web:abc", "cómo va el CI", intake=True)
    satk.pending[0]()
    assert satk.get(tid)["status"] == "done" and posts(bot) == []
    assert [r for r in db.list_requests(satk.conn, "all") if r["requester"] == "status-answer"]
