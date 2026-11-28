import json
import time

import pytest

from polar import cli, db
from polar.tasks import FOLLOW_BUS_EDITS_MAX, Tasks, bus_state
from tests.conftest import OWNER
from tests.test_tasks import Sh, env, msg  # noqa: F401


class BusSh(Sh):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.sessions_rc = 0

    def __call__(self, argv, timeout=15):
        if len(argv) > 1 and argv[1] == "sessions":
            self.calls.append(argv)
            if self.sessions_rc:
                return self.sessions_rc, ""
            return 0, "\n".join(json.dumps(r) for r in self.rows)
        return super().__call__(argv, timeout)


@pytest.fixture
def sh():
    return BusSh()


@pytest.fixture
def tk(conn, bot, sh, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_NOTICES", "on")
    return Tasks(conn, bot, OWNER, sh=sh, sleep=lambda s: None)


def row(sid, state="live", host="server", tmux=None, **kw):
    r = {"session_id": sid, "host": host, "tmux": tmux, "state": state}
    if state == "closed":
        r["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    r.update(kw)
    return r


_n = [0]


def mk(tk, status="running", sid="s1", ts="50.1", **kw):
    _n[0] += 1
    tid = tk._insert("task", status, "t", "texto", "D1", "1.%d" % _n[0], "1.%d" % _n[0], **kw)
    tk.update(tid, session_id=sid, status_ts=ts, status_text="x" if ts else None)
    old(tk, tid)
    return tid


def old(tk, tid, secs=3600):
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs))
    tk.conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (at, tid))


def test_bus_state_derived():
    assert bus_state({"parked_at": "x", "ended_at": "y"}) == "parked"
    assert bus_state({"ended_at": "y"}) == "closed"
    assert bus_state({}) == "live"
    assert bus_state({"state": "closed", "parked_at": "x"}) == "closed"


def test_closed_edits_in_place(tk, sh, bot):
    tid = mk(tk)
    sh.rows = [row("s1", "closed")]
    tk.follow_bus(force=True)
    t = tk.get(tid)
    assert t["status"] == "done" and t["finished_at"]
    assert len(bot.of("chat_update")) == 1 and not bot.of("chat_postMessage")
    assert "Terminó" in bot.of("chat_update")[0]["text"]


def test_closed_without_status_ts_posts_nothing(tk, sh, bot):
    tid = mk(tk, ts=None)
    sh.rows = [row("s1", "closed")]
    tk.follow_bus(force=True)
    t = tk.get(tid)
    assert t["status"] == "done" and "Terminó" in t["status_text"]
    assert not bot.of("chat_update") and not bot.of("chat_postMessage")


def test_routed_done_no_slack(tk, sh, bot):
    tid = tk._insert("task", "routed", "t", "x", "D1", "1.1", "1.1")
    tk.update(tid, route_done=1, match_session="m1", status_ts="9.9")
    old(tk, tid)
    sh.rows = [row("m1", "closed")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "done"
    assert not bot.calls


def test_routed_not_delivered_left_alone(tk, sh):
    tid = tk._insert("task", "routed", "t", "x", "D1", "1.1", "1.1")
    tk.update(tid, match_session="m1")
    old(tk, tid)
    sh.rows = [row("m1", "closed")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "routed"


def test_running_to_parked(tk, sh, bot):
    tid = mk(tk)
    sh.rows = [row("s1", "parked")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "parked"
    assert bot.of("chat_update")[0]["text"].startswith("🅿️")


def test_parked_to_running_with_successor(tk, sh, bot):
    tid = mk(tk, status="parked")
    sh.rows = [row("s1", "closed", tmux="task-old"),
               row("s2", "live", tmux="task-w12345678", handoff_from="s1")]
    sh.live = ["task-w12345678"]
    tk.follow_bus(force=True)
    t = tk.get(tid)
    assert (t["status"], t["tmux"], t["session_id"]) == ("running", "task-w12345678", "s2")
    assert bot.of("chat_update")[0]["text"] == "▶️ Corre otra vez."


def test_live_on_other_host_unchanged(tk, sh):
    tid = mk(tk, status="parked")
    sh.rows = [row("s1", "closed"), row("s2", "live", host="local", tmux="tab1", handoff_from="s1")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "parked"


def test_handoff_chain_closed(tk, sh):
    tid = mk(tk, status="parked")
    sh.rows = [row("s1", "closed"), row("s2", "closed", handoff_from="s1", last_seen="2"),
               row("s3", "closed", handoff_from="s2", last_seen="3")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "done"


def test_cycle_does_not_hang(tk, sh):
    mk(tk, status="parked")
    sh.rows = [row("s1", "parked", handoff_from="s2"), row("s2", "parked", handoff_from="s1")]
    tk.follow_bus(force=True)


def test_unknown_session_left_alone(tk, sh):
    tid = mk(tk, sid="zzz")
    sh.rows = [row("s1", "closed")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "running"


def test_laptop_grace(tk, sh, bot):
    tid = mk(tk)
    tk.update(tid, host="laptop")
    old(tk, tid)
    now = time.time()
    iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - s))
    sh.rows = [row("s1", "closed", host="local", ended_at=iso(60))]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "running"
    sh.rows = [row("s1", "closed", host="local", ended_at=iso(1200))]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "done"
    sh.rows = [row("s1", "parked", host="local")]
    t2 = mk(tk, sid="s1")
    tk.update(t2, host="laptop")
    old(tk, t2)
    tk.follow_bus(force=True)
    assert tk.get(t2)["status"] == "running"


def test_edit_cap(tk, sh, bot):
    ids = [mk(tk, sid="s%d" % i, ts="5.%d" % i) for i in range(FOLLOW_BUS_EDITS_MAX + 3)]
    quiet = mk(tk, sid="q", ts=None)
    sh.rows = [row("s%d" % i, "closed") for i in range(len(ids))] + [row("q", "closed")]
    tk.follow_bus(force=True)
    assert len(bot.of("chat_update")) == FOLLOW_BUS_EDITS_MAX
    assert tk.get(quiet)["status"] == "done"
    assert [tk.get(i)["status"] for i in ids].count("running") == 3
    tk.follow_bus(force=True)
    assert [tk.get(i)["status"] for i in ids].count("running") == 0


def test_bus_failure_and_empty_change_nothing(tk, sh, bot):
    tid = mk(tk)
    sh.sessions_rc = 1
    tk.follow_bus(force=True)
    sh.sessions_rc = 0
    sh.rows = []
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "running" and not bot.of("chat_update")


def test_gate(tk, sh):
    mk(tk)
    sh.rows = [row("s1", "live", tmux="task-x")]
    tk.follow_bus()
    tk.follow_bus()
    assert len(sh.of("sessions")) == 1


def test_dry_run_changes_nothing(conn, sh, bot, capsys, monkeypatch):
    t0 = Tasks(conn, None, "", sh=sh)
    tid = mk(t0)
    sh.rows = [row("s1", "closed")]
    out = t0.follow_bus(force=True, dry_run=True)
    assert out == [{"task": tid, "old": "running", "new": "done", "bus": "closed", "edit": False}]
    assert t0.get(tid)["status"] == "running"
    t0.follow_bus(force=True)  # None client: no crash
    assert t0.get(tid)["status"] == "done"


def test_cli_registered():
    p = cli.build_parser()
    assert p.parse_args(["follow-bus", "--dry-run"]).dry_run is True


def test_thread_owner_row_without_state(tk, sh, monkeypatch):
    class S(BusSh):
        def __call__(self, argv, timeout=15):
            if argv[1] == "owners":
                return 0, json.dumps(["abcd1234"])
            if argv[1] == "session":
                return 0, json.dumps({"session_id": "abcd1234", "title": "T", "parked_at": "x"})
            return super().__call__(argv, timeout)
    tk.sh = S()
    assert tk.thread_owner(["D1:1.1"]) == ("abcd1234", "T")


def test_reap_asks_bus(tk, sh, bot, tmp_path):
    tk.on_message(msg("uno", ts="1.1"))
    tk.tick()
    t = tk.rows("task")[0]
    sid = t["session_id"]
    n0 = len(sh.of("sessions"))
    sh.live.remove(t["tmux"])
    sh.rows = [row(sid, "closed", tmux=t["tmux"]), row("n2", "live", tmux="task-wabcdef12", handoff_from=sid)]
    sh.live.append("task-wabcdef12")
    tk.reap()
    t = tk.get(t["id"])
    assert (t["status"], t["tmux"], t["session_id"]) == ("running", "task-wabcdef12", "n2")
    sh.live.remove("task-wabcdef12")
    sh.rows = [row("n2", "parked")]
    (tmp_path / "parked").mkdir(exist_ok=True)
    (tmp_path / "parked" / "task-wabcdef12").write_text("1")
    tk.reap()
    assert tk.get(t["id"])["status"] == "parked" and not (tmp_path / "parked" / "task-wabcdef12").exists()
    assert len(sh.of("sessions")) > n0


def test_reap_bus_closed_beats_marker_and_failure_uses_marker(tk, sh, tmp_path):
    tk.on_message(msg("uno", ts="1.1"))
    tk.on_message(msg("dos", ts="1.2"))
    tk.tick()
    tk.tick()
    a, b = tk.rows("task")
    sh.live.clear()
    sh.live.append("task-other")
    (tmp_path / "parked").mkdir()
    (tmp_path / "parked" / a["tmux"]).write_text("1")
    (tmp_path / "parked" / b["tmux"]).write_text("1")
    sh.rows = [row(a["session_id"], "closed")]
    tk.reap()
    assert tk.get(a["id"])["status"] == "done"
    assert tk.get(b["id"])["status"] == "parked"  # unknown session: marker rule


def test_close_grace(tk, sh):
    tid = mk(tk)
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20))
    sh.rows = [row("s1", "closed", ended_at=fresh)]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "running"  # an end of 20 s ago may still get a handoff successor


def test_revive_done_newest_only(tk, sh, bot):
    a = mk(tk, status="done", sid="s1", ts="7.1")
    b = mk(tk, status="done", sid="s1", ts="7.2")
    now = db.now_iso()
    tk.conn.execute("UPDATE tasks SET finished_at=?", (now,))
    sh.rows = [row("s1", "live", tmux="task-w1111111")]
    sh.live = ["task-w1111111"]
    tk.follow_bus(force=True)
    assert tk.get(a)["status"] == "done"
    t = tk.get(b)
    assert (t["status"], t["tmux"], t["session_id"]) == ("running", "task-w1111111", "s1")
    assert bot.of("chat_update")[0]["text"] == "▶️ Corre otra vez."


def test_done_old_not_revived(tk, sh):
    a = mk(tk, status="done", sid="s1")
    tk.conn.execute("UPDATE tasks SET finished_at='2020-01-01T00:00:00Z'")
    sh.rows = [row("s1", "live", tmux="task-w1111111")]
    tk.follow_bus(force=True)
    assert tk.get(a)["status"] == "done"


def test_recent_row_skipped(tk, sh):
    tid = mk(tk)
    old(tk, tid, 10)
    sh.rows = [row("s1", "closed")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "running"


def test_compare_and_set(tk, sh, bot):
    tid = mk(tk)
    sh.rows = [row("s1", "closed")]
    orig = tk.follow_plan

    def racing(t, bus, live=None):
        ch = orig(t, bus, live)
        tk.conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (tid,))
        return ch
    tk.follow_plan = racing
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "failed" and not bot.of("chat_update")


def test_resolve_prefers_live_then_parked():
    from polar.tasks import resolve_session
    rows = {r["session_id"]: r for r in [
        row("p"), row("a", "closed", handoff_from="p", last_seen="9"),
        row("b", "parked", handoff_from="p", last_seen="1"),
        row("c", "live", handoff_from="p", last_seen="0")]}
    assert resolve_session(rows, "p")["session_id"] == "c"
    del rows["c"]
    assert resolve_session(rows, "p")["session_id"] == "b"


def test_done_not_revived_when_an_open_task_shows_the_session(tk, sh, bot):
    a = mk(tk, status="done", sid="s1", ts="7.1")
    b = mk(tk, status="done", sid="s2", ts="7.2")
    c = mk(tk, status="parked", sid="s3", ts="7.3")
    tk.conn.execute("UPDATE tasks SET finished_at=? WHERE status='done'", (db.now_iso(),))
    sh.rows = [row("s1", "live", tmux="task-w1111111"),
               row("s2", "closed"), row("s3", "parked"),
               row("s4", "live", tmux="task-w4444444", handoff_from="s2")]
    sh.live = ["task-w1111111", "task-w4444444"]
    tk.conn.execute("UPDATE tasks SET session_id='s1' WHERE id=?", (c,))  # the parked task already shows s1
    old(tk, c)
    tk.follow_bus(force=True)
    assert tk.get(a)["status"] == "done"  # s1 shows on the open task c
    assert (tk.get(b)["status"], tk.get(b)["session_id"]) == ("running", "s4")  # s4 shows nowhere else


def test_stale_live_row_without_tmux_changes_nothing(tk, sh, bot):
    """A session killed without its end hook stays «live» in the bus. Its tmux is gone: no «running»."""
    a = mk(tk, status="done", sid="s1", ts="7.1")
    b = mk(tk, status="parked", sid="s2", ts="7.2")
    tk.conn.execute("UPDATE tasks SET finished_at=? WHERE id=?", (db.now_iso(), a))
    sh.rows = [row("s1", "live", tmux="task-w1111111"), row("s2", "live", tmux="task-w2222222")]
    sh.live = ["task-wother"]
    assert tk.follow_bus(force=True) == []
    assert (tk.get(a)["status"], tk.get(b)["status"]) == ("done", "parked")
    assert not bot.of("chat_update")


def test_revival_sees_the_successor_of_an_open_task(tk, sh, bot):
    """Open task A has s1, which resolves to live s2. Done task B has the older s0, which also ends at s2."""
    a = mk(tk, status="running", sid="s1", ts="7.1", tmux="task-w2222222")
    b = mk(tk, status="done", sid="s0", ts="7.2")
    tk.conn.execute("UPDATE tasks SET finished_at=? WHERE id=?", (db.now_iso(), b))
    sh.rows = [row("s0", "closed"), row("s1", "closed", handoff_from="s0"),
               row("s2", "live", tmux="task-w2222222", handoff_from="s1")]
    sh.live = ["task-w2222222"]
    tk.follow_bus(force=True)
    assert (tk.get(a)["status"], tk.get(a)["session_id"]) == ("running", "s2")
    assert tk.get(b)["status"] == "done"


def test_closed_with_bad_ended_at_still_ends(tk, sh):
    tid = mk(tk)
    sh.rows = [row("s1", "closed", ended_at="not a time")]
    tk.follow_bus(force=True)
    assert tk.get(tid)["status"] == "done"


def test_revival_clears_finished_at(tk, sh):
    tid = mk(tk, status="done", sid="s1")
    tk.conn.execute("UPDATE tasks SET finished_at=? WHERE id=?", (db.now_iso(), tid))
    sh.rows = [row("s1", "live", tmux="task-w1111111")]
    sh.live = ["task-w1111111"]
    tk.follow_bus(force=True)
    assert (tk.get(tid)["status"], tk.get(tid)["finished_at"]) == ("running", None)
