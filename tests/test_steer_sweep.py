"""An owner message in a thread reaches a session or fails with a visible line."""
import time

import pytest

from polar import db
from polar.tasks import Tasks, real_requester
from tests.conftest import OWNER
from tests.test_tasks import Sh, env, msg, texts  # noqa: F401  (env: autouse fixture)

SID = "11111111-1111-4111-8111-111111111111"
LAP = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class BusSh(Sh):
    """Sh plus a fake `agent-bus deliver`."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bus = (0, "delivered to 11111111 in task-x")

    def __call__(self, argv, timeout=15):
        if len(argv) > 1 and argv[1] == "deliver":
            self.calls.append(argv)
            return self.bus
        return super().__call__(argv, timeout)

    def delivers(self):
        return self.of("deliver")


@pytest.fixture
def sh():
    return BusSh()


@pytest.fixture
def tk(conn, bot, sh):
    return Tasks(conn, bot, OWNER, sh=sh, sleep=lambda s: None)


def age(tk, sid, seconds):
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
    tk.conn.execute("UPDATE tasks SET created_at=?, updated_at=? WHERE id=?", (old, old, sid))


def card(conn, requester, dm_ts):
    rid = db.create_request(conn, "decision", "¿Sí?", "c", requester,
                            options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    conn.execute("UPDATE requests SET dm_ts=? WHERE id=?", (dm_ts, rid))
    return rid


def test_real_requester_unwraps_prod_op():
    assert real_requester("prod-op 7cb4d06dcebd %s p_3" % SID) == SID + " p_3"
    assert real_requester(SID + " p_3") == SID + " p_3"
    assert real_requester("") == ""


def test_case_prod_op_card_reaches_its_session(tk, conn, sh):
    """A prod-op card names «prod-op <op> <session> <pane>»: the steer must name the session, not «prod-op»."""
    card(conn, "prod-op 7cb4d06dcebd %s p_3" % LAP, "77.7")
    tk.on_message(msg("Go, adelante", ts="10.6", thread="77.7"))
    s = tk.rows("steer")[0]
    assert s["requester"] == LAP + " p_3"
    age(tk, s["id"], 120)
    tk.sweep_steers()
    argv = sh.delivers()[0]
    assert argv[2] == LAP and "--place" in argv and "p_3" in argv
    assert tk.rows("steer")[0]["status"] == "delivered"


def test_case_prod_op_card_of_server_task(tk, conn):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.update(tid, session_id=SID)
    card(conn, "prod-op 7cb4d06dcebd %s task-%s" % (SID, tid), "77.7")
    tk.on_message(msg("Go", ts="10.6", thread="77.7"))
    assert tk.rows("steer")[0]["target_task"] == tid


def test_case_parked_server_session_is_resumed_by_the_bus(tk, sh, bot, tmp_path):
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    sh.live.remove(t["tmux"])
    (tmp_path / "parked").mkdir()
    (tmp_path / "parked" / t["tmux"]).write_text("1")
    tk.tick()
    assert tk.get(t["id"])["status"] == "parked"
    sh.bus = (0, "met task:resume-ab of %s; resumed" % t["session_id"][:8])
    tk.on_message(msg("sigue con esto", ts="10.5", thread="10.1"))
    tk.deliver_steers()
    argv = sh.delivers()[0]
    assert argv[2] == t["session_id"] and any(a.startswith("Polar (owner): sigue con esto") for a in argv)
    assert argv[argv.index("--ref") + 1].startswith("polar-steer:")
    assert tk.rows("steer")[0]["status"] == "delivered"
    assert texts(bot)[-1] == "↪️ Enviado a la sesión"


def test_case_laptop_card_steer_waits_for_the_relay_then_goes_by_bus(tk, conn, sh):
    card(conn, LAP + " w1:p3", "77.8")
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    s = tk.rows("steer")[0]
    tk.sweep_steers()  # the relay has had no time yet
    assert sh.delivers() == [] and tk.rows("steer")[0]["status"] == "new"
    age(tk, s["id"], 61)
    tk.sweep_steers()
    assert len(sh.delivers()) == 1 and tk.rows("steer")[0]["status"] == "delivered"
    tk.sweep_steers()
    assert len(sh.delivers()) == 1  # taken once


def test_laptop_task_that_ended_still_gets_its_steer(tk, sh):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.claim(tid, "laptop", "5", LAP, "p_9")
    tk.on_message(msg("sigue", ts="10.2", thread="10.1"))
    tk.task_done(tid)  # the laptop relay lists only running tasks: nobody would take it
    age(tk, tk.rows("steer")[0]["id"], 90)
    tk.sweep_steers()
    argv = sh.delivers()[0]
    assert argv[2] == LAP and "p_9" in argv


@pytest.mark.parametrize("out", ["router inbox: the bus has no pane for 11111111",
                                 "duplicate of 7: router inbox (queued for the server): no live input box"])
def test_router_inbox_is_a_handoff_not_a_loss(tk, conn, sh, bot, out):
    sh.bus = (0, out)
    card(conn, LAP + " w1:p3", "77.8")
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    age(tk, tk.rows("steer")[0]["id"], 90)
    tk.sweep_steers()
    assert tk.rows("steer")[0]["status"] == "delivered"
    assert texts(bot)[-1].startswith("↪️ La sesión no tiene una pantalla abierta. Se lo pasé al router")


@pytest.mark.parametrize("outcome,why", [
    ((0, "something new"), "no encontré dónde escribirle"),
    ((1, ""), "no conoce esa sesión"),
    ((None, ""), "no contestó a tiempo"),
])
def test_failure_is_visible_and_dropped(tk, conn, sh, bot, outcome, why):
    sh.bus = outcome
    card(conn, LAP + " w1:p3", "77.8")
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    age(tk, tk.rows("steer")[0]["id"], 90)
    tk.sweep_steers()
    assert tk.rows("steer")[0]["status"] == "dropped"
    last = texts(bot)[-1]
    assert last.startswith("⚠️ Tu mensaje no llegó a ninguna sesión") and why in last


def test_old_steer_expires_without_a_message(tk, conn, sh, bot):
    card(conn, LAP + " w1:p3", "77.8")
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    n = len(bot.calls)
    age(tk, tk.rows("steer")[0]["id"], 25 * 3600)
    tk.sweep_steers()
    assert tk.rows("steer")[0]["status"] == "expired"
    assert sh.delivers() == [] and len(bot.calls) == n


def test_tab_steer_is_left_to_the_relay(tk, sh):
    tk.on_message(msg("→ 4 hola"))
    age(tk, tk.rows("steer")[0]["id"], 3600)
    tk.sweep_steers()
    assert sh.delivers() == [] and tk.rows("steer")[0]["status"] == "new"


def test_race_relay_wins(tk, conn, sh):
    card(conn, LAP + " w1:p3", "77.8")
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    sid = tk.rows("steer")[0]["id"]
    age(tk, sid, 90)
    assert tk.mark_steer_done(sid)  # `polar steer-done` came first
    tk.sweep_steers()
    assert sh.delivers() == []


def test_race_sweep_wins(tk, conn, sh):
    card(conn, LAP + " w1:p3", "77.8")
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    s = tk.rows("steer")[0]
    age(tk, s["id"], 90)
    # the sweep claimed it and the bus call is still running
    tk.conn.execute("UPDATE tasks SET status='sending' WHERE id=?", (s["id"],))
    assert not tk.mark_steer_done(s["id"])
    assert tk.laptop_steers() == []
    assert not tk.send_via_bus(s, LAP)  # a second claim fails: one delivery only
    assert sh.delivers() == []


def test_claim_that_never_finished_is_reported(tk, conn, bot):
    card(conn, LAP + " w1:p3", "77.8")
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    sid = tk.rows("steer")[0]["id"]
    tk.conn.execute("UPDATE tasks SET status='sending' WHERE id=?", (sid,))
    age(tk, sid, 700)
    tk.sweep_steers()
    assert tk.rows("steer")[0]["status"] == "dropped"
    assert "no pude confirmar" in texts(bot)[-1]


def test_requester_without_a_session_id_is_reported(tk, conn, bot, sh):
    conn.execute("INSERT INTO tasks (id,kind,status,title,text,channel,thread_ts,msg_ts,requester,created_at,updated_at)"
                 " VALUES ('s1','steer','new','t','hola','D1','1.1','1.2','watchdog','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
    age(tk, "s1", 90)
    tk.sweep_steers()
    assert tk.get("s1")["status"] == "dropped" and sh.delivers() == []
    assert "no dice quién la hizo" in texts(bot)[-1]
