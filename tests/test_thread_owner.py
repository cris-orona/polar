import json

import pytest

from polar.tasks import Tasks
from tests.conftest import OWNER
from tests.test_tasks import Sh, env, msg, texts  # noqa: F401

SID = "e0346636-1111-4111-8111-111111111111"
LINK = "https://example.slack.com/archives/C0ABC123/p1795627306218569"


class OwnSh(Sh):
    def __init__(self):
        super().__init__()
        self.owners = {}     # "C:ts" -> list
        self.rows = {}       # sid -> row
        self.owners_rc = 0

    def __call__(self, argv, timeout=15):
        if len(argv) > 2 and argv[1] == "owners":
            self.calls.append(argv)
            assert timeout <= 20
            return self.owners_rc, json.dumps(self.owners.get(argv[3], []))
        if len(argv) > 2 and argv[1] == "session":
            self.calls.append(argv)
            row = self.rows.get(argv[2])
            return (0, json.dumps(row)) if row else (1, "")
        return super().__call__(argv, timeout)


@pytest.fixture
def sh():
    return OwnSh()


@pytest.fixture
def tk(conn, bot, sh, monkeypatch):
    monkeypatch.setenv("POLAR_TRIAGE", "1")
    return Tasks(conn, bot, OWNER, sh=sh, sleep=lambda s: None)


def test_refs_permalink_and_thread_param():
    refs = Tasks.thread_refs("mira %s y %s?thread_ts=1700000000.000100&cid=C0ABC123 y %s" % (
        LINK, LINK.replace("p1795627306218569", "p1700000001000200"), LINK), "D1", "5.5")
    assert refs == ["C0ABC123:1795627306.218569", "C0ABC123:1700000000.000100", "D1:5.5"]


@pytest.mark.parametrize("state,routed", [("live", True), ("parked", True), ("closed", False)])
def test_owner_session_routes_without_triage(tk, sh, bot, state, routed):
    sh.owners["C0ABC123:1795627306.218569"] = [SID]
    sh.rows[SID] = {"id": SID, "state": state, "title": "ontología"}
    tk.on_message(msg("cómo sigue esto %s" % LINK))
    t = tk.rows("task")[0]
    if routed:
        assert t["status"] == "matched" and t["match_session"] == SID and t["match_reason"] == "dueño del hilo"
        tk.process_triage()
        assert tk.get(t["id"])["status"] == "routed"
        assert "dueño del hilo" in texts(bot)[-1]
    else:
        assert t["status"] == "triage"


def test_bus_failure_falls_back_to_triage(tk, sh):
    sh.owners_rc = 1
    tk.on_message(msg("mira %s" % LINK))
    assert tk.rows("task")[0]["status"] == "triage"


def test_no_link_no_bus_call(tk, sh):
    tk.on_message(msg("hola"))
    assert sh.of("owners") == [] and tk.rows("task")[0]["status"] == "triage"


def test_dm_own_thread_is_checked(tk, sh):
    sh.owners["D1:9.9"] = [{"session": SID}]
    sh.rows[SID] = {"state": "live", "tmux": "w1"}
    tk.on_message(msg("sigue", ts="10.1", thread="10.1"))  # thread == ts: a top-level message with a thread
    t = tk.rows("task")[0]
    assert t["status"] == "triage"
    sh.owners["D1:10.2"] = [SID]
    tk.on_message(msg("otra", ts="10.2", thread="10.2"))
    assert tk.rows("task")[1]["match_session"] == SID
