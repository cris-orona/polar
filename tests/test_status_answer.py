import json

import pytest

from polar import db, tasks
from polar.core import Polar
from polar.tasks import Tasks, is_status_question
from tests.conftest import OWNER
from tests.test_tasks import Sh, env, msg, texts  # noqa: F401


class SaSh(Sh):
    def __init__(self):
        super().__init__()
        self.sa = (0, json.dumps({"ok": True, "answer_md": "**Va bien.** Falta el PR.", "sources": ["pr:a#1"]}))

    def __call__(self, argv, timeout=15):
        if argv[0].endswith("status-answer"):
            self.calls.append(argv)
            assert timeout == 120 and argv[1:3] == ["--json", "--"]
            return self.sa
        return super().__call__(argv, timeout)


@pytest.fixture
def sh():
    return SaSh()


@pytest.fixture
def tk(conn, bot, sh, monkeypatch):
    monkeypatch.setenv("POLAR_TRIAGE", "1")
    t = Tasks(conn, bot, OWNER, sh=sh, sleep=lambda s: None)
    t.pending = []
    t.spawn = t.pending.append
    return t


@pytest.mark.parametrize("text,ok", [
    ("¿cómo vamos con el speed up del CI?", True), ("en qué quedó la migración", True),
    ("Qué pasó con el PR 12", True), ("status del deploy", True), ("avance de Logros", True),
    ("how is the CI work", True), ("status of the release", True), ("arregla el CI", False),
    ("cómo va " + "x" * 300, False), ("", False)])
def test_pattern(text, ok):
    assert is_status_question(text) is ok


def test_answered(tk, sh, bot, conn):
    tk.on_message(msg("¿cómo vamos con el CI?"))
    t = tk.rows("task")[0]
    assert t["status"] == "answering" and len(tk.pending) == 1
    tk.process_triage()
    tk.pending[0]()
    assert tk.get(t["id"])["status"] == "done"
    assert not any("Va bien" in x for x in texts(bot))  # dm mode: the card shows in the thread, no second post
    card = [r for r in db.list_requests(conn, "all") if r["requester"] == "status-answer"][0]
    assert card["kind"] == "report" and card["silent"] == 0 and card["report"]["next"] == ["Pregúntale a la sesión"]
    assert "Fuentes: pr:a#1" in card["draft_text"]


@pytest.mark.parametrize("out", [(3, ""), (None, ""), (1, "boom"), (0, "no json")])
def test_fallback_to_triage_without_message(tk, sh, bot, out):
    sh.sa = out
    tk.on_message(msg("cómo va el CI"))
    n = len(bot.calls)
    tk.pending[0]()
    assert tk.rows("task")[0]["status"] == "triage"
    assert len(bot.calls) == n


def test_switch_off(tk, monkeypatch):
    monkeypatch.setenv("POLAR_STATUS_ANSWER", "off")
    tk.on_message(msg("cómo va el CI"))
    assert tk.rows("task")[0]["status"] == "triage" and tk.pending == []


def test_stuck_answering_expires(tk):
    tk.on_message(msg("cómo va el CI"))
    tk.conn.execute("UPDATE tasks SET created_at='2020-01-01T00:00:00Z'")
    tk.expire_triage()
    assert tk.rows("task")[0]["status"] in ("triage", "queued")


def test_follow_up_opens_normal_routing(conn, bot, user, tk):
    polar = Polar(conn, bot, user, OWNER)
    polar.tasks = tk
    tk.on_message(msg("cómo va el CI"))
    tk.pending[0]()
    card = [r for r in db.list_requests(conn, "all") if r["requester"] == "status-answer"][0]
    polar.deliver_pending()
    polar.on_next(OWNER, card["id"], 0)
    new = [t for t in tk.rows("task") if t["status"] == "triage"]
    assert len(new) == 1 and "Pregúntale a la sesión" in new[0]["text"] and "cómo va el CI" in new[0]["text"]


def test_web_mode_posts_the_answer_in_the_thread(tk, bot, monkeypatch):
    monkeypatch.setenv("POLAR_DELIVERY", "web")
    tk.on_message(msg("cómo va el CI"))
    tk.pending[0]()
    assert any("Va bien" in x for x in texts(bot))
