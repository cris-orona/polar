import json

import os
import pytest
import time

from polar import blocks, cli, db
from polar.tasks import Tasks, clip_title, parse_free_gb, split_items
from tests.conftest import OWNER

VM = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              %d.
Pages active:                            272424.
Pages inactive:                          %d.
Pages speculative:                       %d.
"""
GB_PAGES = 65536  # 1 GB of 16 KB pages


class Sh:
    """A fake for Tasks.sh: records every call and answers by the command name."""

    def __init__(self, free_gb=8, live=()):
        self.calls = []
        self.live = list(live)
        self.bus_live = None  # tmux names the bus has as live
        self.born = {}        # tmux name -> epoch of its start (default: long ago)
        self.free_gb = free_gb
        self.new_rc = 0
        self.send_rc = 0

    def __call__(self, argv, timeout=15):
        self.calls.append(argv)
        if argv[0].endswith("vm_stat"):
            return 0, VM % (int(self.free_gb * GB_PAGES), 0, 0)
        sub = argv[1]
        if sub == "sessions":  # agent-bus sessions --json: one row for each name in bus_live; None: no answer
            if self.bus_live is None:
                return 1, ""
            return 0, "\n".join(json.dumps({"tmux": n, "state": "live"}) for n in self.bus_live) + \
                "\n" + json.dumps({"tmux": "task-old", "state": "closed"})
        if sub == "list-sessions":
            if not self.live:
                return 1, ""
            if "session_created" in argv[-1]:
                return 0, "\n".join("%s %d" % (n, self.born.get(n, 1)) for n in self.live)
            return 0, "\n".join(self.live)
        if sub == "new-session":
            if self.new_rc == 0:
                self.live.append(argv[argv.index("-s") + 1])
            return self.new_rc, ""
        if sub == "send-keys":
            return self.send_rc, ""
        return 0, ""

    def of(self, sub):
        return [c for c in self.calls if len(c) > 1 and c[1] == sub]


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("POLAR_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("POLAR_CLAUDE_BIN", "/x/claude")
    monkeypatch.setenv("POLAR_TMUX_BIN", "/x/tmux")
    monkeypatch.setenv("POLAR_CLAUDE_JSON", str(tmp_path / "claude.json"))
    monkeypatch.setenv("POLAR_PARKED_DIR", str(tmp_path / "parked"))
    monkeypatch.delenv("POLAR_TASK_MAX", raising=False)
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "0")  # the grace tests set it
    monkeypatch.setenv("POLAR_TRIAGE", "0")  # the old tests expect a queued task; triage tests turn it on


@pytest.fixture
def sh():
    return Sh()


@pytest.fixture
def tk(conn, bot, sh):
    return Tasks(conn, bot, OWNER, sh=sh, sleep=lambda s: None)


def msg(text, ts="10.1", thread=None, **kw):
    ev = {"type": "message", "channel_type": "im", "channel": "D1", "user": OWNER, "text": text, "ts": ts}
    if thread:
        ev["thread_ts"] = thread
    ev.update(kw)
    return ev


def texts(bot):
    """What the owner reads now: each posted message, with its last edit applied."""
    edits = {c["ts"]: c["text"] for c in bot.of("chat_update")}
    return [edits.get("100.%d" % (i + 1), c["text"]) for i, c in enumerate(bot.of("chat_postMessage"))]


# -- parsing ------------------------------------------------------------------
def test_title_clip():
    assert clip_title("hola\nmundo") == "hola"
    assert len(clip_title("x" * 100)) == 60


def test_split_items():
    assert split_items("- a\n- b\n- c") == ["a", "b", "c"]
    assert split_items("1. uno\n2) dos\n\n3. tres") == ["uno", "dos", "tres"]
    assert split_items("a\nb\nc") == ["a", "b", "c"]
    assert split_items("a\nb") == []
    assert split_items("a\nb\n" + "x" * 400) == []


def test_free_memory():
    assert parse_free_gb(VM % (2 * GB_PAGES, GB_PAGES, GB_PAGES)) == pytest.approx(4.0)
    assert parse_free_gb("nothing") is None


# -- events -------------------------------------------------------------------
def test_top_level_message_is_a_task(tk, bot, conn):
    tk.on_message(msg("Revisa el cobro de Acme\ncon detalle"))
    t = tk.rows("task")[0]
    assert t["status"] == "queued" and t["title"] == "Revisa el cobro de Acme"
    assert t["text"].endswith("con detalle") and t["thread_ts"] == "10.1" and t["msg_ts"] == "10.1"
    call = bot.of("chat_postMessage")[0]
    assert call["text"] == "🧭 Recibido. Abro una sesión…" and call["thread_ts"] == "10.1"


def test_ignored_messages(tk, bot):
    tk.on_message(msg("hola", bot_id="B1"))
    tk.on_message(msg("hola", subtype="message_changed"))
    tk.on_message(msg("hola", user="U9"))
    tk.on_message(msg("   "))
    tk.on_message(msg("hola", channel_type="channel"))
    assert tk.rows("task") == [] and bot.calls == []


def test_duplicate_event(tk, bot):
    tk.on_message(msg("hola"))
    tk.on_message(msg("hola"))
    assert len(tk.rows("task")) == 1 and len(bot.of("chat_postMessage")) == 1


def plain(tk):
    """Make the pick fall back to the plain card (no plan came)."""
    tk.conn.execute("UPDATE picks SET created_at='2020-01-01T00:00:00Z'")
    tk.expire_plans()


def test_list_makes_a_planning_pick(tk, bot, conn):
    tk.on_message(msg("- uno\n- dos\n- tres"))
    assert tk.rows("task") == []
    call = bot.of("chat_postMessage")[0]
    assert call["thread_ts"] == "10.1" and "Recibí 3 tareas" in call["text"] and not call.get("blocks")
    row = conn.execute("SELECT status, card_ts FROM picks").fetchone()
    assert (row["status"], row["card_ts"]) == ("planning", "100.1")


def test_plain_card_after_timeout(tk, bot, conn):
    tk.on_message(msg("- uno\n- dos\n- tres"))
    tk.expire_plans()  # not yet
    assert conn.execute("SELECT status FROM picks").fetchone()["status"] == "planning"
    plain(tk)
    assert conn.execute("SELECT status FROM picks").fetchone()["status"] == "open"
    call = bot.of("chat_update")[0]
    assert call["ts"] == "100.1" and "Encontré 3 tareas" in call["text"]
    assert "No llegó el plan" in json.dumps(call["blocks"], ensure_ascii=False)
    kinds = [(b["type"], [e["type"] for e in b.get("elements", [])]) for b in call["blocks"]]
    assert ("actions", ["checkboxes"]) in kinds and ("actions", ["button"]) in kinds
    assert call["blocks"][-1]["elements"][0]["text"]["text"] == "Arrancar las marcadas"


def test_pick_splits_over_ten(tk, bot):
    tk.on_message(msg("\n".join("- t%d" % i for i in range(25))))
    plain(tk)
    bl = bot.of("chat_update")[0]["blocks"]
    boxes = [e for b in bl if b["type"] == "actions" for e in b["elements"] if e["type"] == "checkboxes"]
    assert [len(e["options"]) for e in boxes] == [10, 10, 5]
    assert len({e["action_id"] for e in boxes}) == 3


def state(*values):
    return {"values": {"pick_0": {"polar_pick_check:0": {
        "selected_options": [{"value": str(v)} for v in values]}}}}


def test_pick_go_starts_checked(polar, bot, conn):
    polar.on_message(msg("- uno\n- dos\n- tres"))
    plain(polar.tasks)
    pid = conn.execute("SELECT id FROM picks").fetchone()["id"]
    polar.on_pick_go(OWNER, pid, state(0, 2), "D1", "100.1")
    tasks = polar.tasks.rows("task")
    assert [t["text"] for t in tasks] == ["uno", "tres"]
    assert all(t["thread_ts"] and t["queue_notified"] == 0 for t in tasks)
    assert tasks[0]["thread_ts"] != tasks[1]["thread_ts"]
    upd = bot.of("chat_update")[-1]
    assert "Arranqué 2 de 3" in upd["blocks"][0]["text"]["text"]
    polar.on_pick_go(OWNER, pid, state(1), "D1", "100.1")  # a second click does nothing
    assert len(polar.tasks.rows("task")) == 2


def test_pick_go_none_checked_or_stranger(polar, bot, conn):
    polar.on_message(msg("- uno\n- dos\n- tres"))
    plain(polar.tasks)
    pid = conn.execute("SELECT id FROM picks").fetchone()["id"]
    polar.on_pick_go("U9", pid, state(0), "D1", "100.1")
    assert polar.tasks.rows("task") == []
    polar.on_pick_go(OWNER, pid, state(), "D1", "100.1")
    assert polar.tasks.rows("task") == []
    assert "Marca al menos" in json.dumps(bot.of("chat_update")[-1]["blocks"], ensure_ascii=False)
    assert conn.execute("SELECT status FROM picks").fetchone()["status"] == "open"


def test_picked_indexes():
    assert blocks.picked_indexes({"values": {"a": {"x": {}}, "b": {"polar_pick_check:1": {
        "selected_options": [{"value": "11"}, {"value": "3"}]}}}}) == [3, 11]


# -- steering -----------------------------------------------------------------
def test_arrow_steer_for_tab(tk):
    tk.on_message(msg("→ 3 sigue con el PR"))
    tk.on_message(msg("-> 12: otra cosa", ts="10.2"))
    tk.on_message(msg("→ 4", ts="10.3"))
    s = tk.rows("steer")
    assert [(x["target_tab"], x["text"]) for x in s] == [(3, "sigue con el PR"), (12, "otra cosa")]
    assert tk.rows("task") == []


def test_thread_reply_steers_task(tk, sh, bot):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.on_message(msg("mejor así", ts="10.5", thread="10.1"))
    s = tk.rows("steer")[0]
    assert s["target_task"] == tid and s["text"] == "mejor así"
    # not running yet: it waits
    tk.deliver_steers()
    assert tk.rows("steer")[0]["status"] == "new"


def test_thread_reply_under_card_of_server_task(tk, conn):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.update(tid, session_id="11111111-1111-4111-8111-111111111111")
    rid = db.create_request(conn, "decision", "¿Sí?", "c", "11111111-1111-4111-8111-111111111111 task-" + tid,
                            options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    conn.execute("UPDATE requests SET dm_ts='77.7' WHERE id=?", (rid,))
    tk.on_message(msg("sí, adelante", ts="10.6", thread="77.7"))
    assert tk.rows("steer")[0]["target_task"] == tid


def test_thread_reply_under_laptop_card(tk, conn):
    rid = db.create_request(conn, "decision", "¿Sí?", "c", "99999999-9999-4999-8999-999999999999 w1:p3",
                            options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    conn.execute("UPDATE requests SET dm_ts='77.8' WHERE id=?", (rid,))
    tk.on_message(msg("sí", ts="10.7", thread="77.8"))
    tk.on_message(msg("sin dueño", ts="10.8", thread="1.1"))
    s = tk.rows("steer")
    assert len(s) == 1 and s[0]["requester"] == "99999999-9999-4999-8999-999999999999 w1:p3" and s[0]["target_task"] is None


# -- launcher -----------------------------------------------------------------
def test_launch_starts_tmux(tk, sh, bot, tmp_path):
    tk.on_message(msg("haz algo\ncon calma"))
    tk.tick()
    t = tk.rows("task")[0]
    assert t["status"] == "running" and t["tmux"] == "task-" + t["id"] and t["session_id"]
    new = sh.of("new-session")[0]
    assert new[:6] == ["/x/tmux", "new-session", "-d", "-s", t["tmux"], "-c"]
    assert new[6] == str(tmp_path / "work") and (tmp_path / "work").is_dir()
    cmd = new[7]
    assert "--session-id " + t["session_id"] in cmd and "$(cat " in cmd and "dangerously" not in cmd
    assert "--model opus " in cmd
    brief = (tmp_path / "data/briefs" / (t["tmux"] + ".txt")).read_text()
    assert "Owner task from Polar (task %s)" % t["id"] in brief and "haz algo\ncon calma" in brief
    assert "--thread 10.1" in brief and "--channel D0000000000" in brief
    last = texts(bot)[-1]
    assert last.startswith('▶️ Abrí "haz algo" en el servidor (task-') and "pestaña ☁️" in last


def test_launch_trusts_work_folder(tk, tmp_path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": False}}, "keep": 1}))
    tk.on_message(msg("x"))
    tk.tick()
    data = json.loads(cj.read_text())
    proj = data["projects"][str(tmp_path / "work")]
    assert proj["hasTrustDialogAccepted"] is True and data["keep"] == 1 and "/other" in data["projects"]


def test_queue_when_full(tk, sh, bot, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_MAX", "2")
    sh.live = ["task-aaa", "task-bbb", "release-desk"]
    tk.on_message(msg("uno", ts="1.1"))
    tk.tick()
    tk.tick()
    assert tk.rows("task")[0]["status"] == "queued" and sh.of("new-session") == []
    waits = [t for t in texts(bot) if t.startswith("⏳")]
    assert waits == ["⏳ En cola: 2 antes"]  # once, and release-desk does not count


def test_a_session_the_bus_does_not_have_as_live_holds_no_slot(tk, sh, monkeypatch):
    # 2026-11-23: three resumes stopped at «trust this folder?» and held 3 of 10 slots for 5 hours
    monkeypatch.setenv("POLAR_TASK_MAX", "2")
    sh.live = ["task-aaa", "task-wdead"]
    sh.bus_live = ["task-aaa", "release-desk"]
    tk.on_message(msg("uno", ts="1.1"))
    tk.tick()
    assert tk.rows("task")[0]["status"] == "running"


def test_a_new_session_holds_its_slot_before_the_bus_knows_it(tk, sh, bot, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_MAX", "2")
    sh.live = ["task-aaa", "task-new"]
    sh.bus_live = ["task-aaa"]
    sh.born = {"task-new": time.time() - 30}
    tk.on_message(msg("uno", ts="1.1"))
    tk.tick()
    assert tk.rows("task")[0]["status"] == "queued" and sh.of("new-session") == []
    assert [t for t in texts(bot) if t.startswith("⏳")] == ["⏳ En cola: 2 antes"]


def test_every_session_holds_a_slot_when_the_bus_gives_no_answer(tk, sh, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_MAX", "2")
    sh.live = ["task-aaa", "task-wdead"]
    tk.on_message(msg("uno", ts="1.1"))
    tk.tick()
    assert tk.rows("task")[0]["status"] == "queued"
    assert sh.of("sessions") != []  # it asked the bus


def test_no_bus_call_below_the_limit(tk, sh, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_MAX", "3")
    sh.live = ["task-aaa", "task-bbb"]
    tk.on_message(msg("uno", ts="1.1"))
    tk.tick()
    assert tk.rows("task")[0]["status"] == "running" and sh.of("sessions") == []


def test_queue_when_low_memory(tk, sh, bot):
    sh.free_gb = 1.0
    tk.on_message(msg("uno"))
    tk.tick()
    assert tk.rows("task")[0]["status"] == "queued"
    sh.free_gb = 3.0
    tk.tick()
    assert tk.rows("task")[0]["status"] == "running"


def test_overflow_flag_follows_server_block(tk, sh):
    tk.on_message(msg("uno"))
    sh.free_gb = 1.0
    tk.tick()
    t = tk.queue_list()[0]
    assert t["overflow"] is True  # the server could not start it: the laptop may
    tk.update(t["id"], status="triage")
    tk.update(t["id"], status="queued", queue_notified=0)  # a re-queue starts clean
    assert tk.queue_list()[0]["overflow"] is False


def test_launch_loses_to_laptop_claim(tk, sh, bot):
    tk.on_message(msg("uno"))
    t = tk.rows("task")[0]
    assert tk.claim(t["id"], "laptop", "5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert tk.launch(t) is False  # the row read before the claim must not start a second session
    assert sh.of("new-session") == [] and tk.rows("task")[0]["host"] == "laptop"


def test_one_launch_each_tick_and_max(tk, sh, monkeypatch):
    monkeypatch.setenv("POLAR_TASK_MAX", "2")
    tk.on_message(msg("uno", ts="1.1"))
    tk.on_message(msg("dos", ts="1.2"))
    tk.on_message(msg("tres", ts="1.3"))
    tk.tick()
    assert [t["status"] for t in tk.rows("task")] == ["running", "queued", "queued"]
    tk.tick()
    tk.tick()
    assert [t["status"] for t in tk.rows("task")] == ["running", "running", "queued"]
    monkeypatch.setenv("POLAR_TASK_MAX", "3")
    tk.tick()
    assert tk.rows("task")[2]["status"] == "running"


def test_launch_failure(tk, sh, bot):
    sh.new_rc = 1
    tk.on_message(msg("uno"))
    tk.tick()
    assert tk.rows("task")[0]["status"] == "failed"
    assert any("No pude abrir" in t for t in texts(bot))


def test_tmux_unavailable_keeps_queue(tk, sh):
    tk.on_message(msg("uno"))
    sh.__class__.__call__  # noqa
    tk.sh = lambda argv, timeout=15: (None, "")
    tk.tick()
    assert tk.rows("task")[0]["status"] == "queued"


def test_reap_done_and_parked(tk, sh, bot, tmp_path):
    tk.on_message(msg("uno", ts="1.1"))
    tk.on_message(msg("dos", ts="1.2"))
    tk.tick()
    tk.tick()
    a, b = tk.rows("task")
    sh.live.remove(a["tmux"])
    sh.live.remove(b["tmux"])
    (tmp_path / "parked").mkdir()
    (tmp_path / "parked" / b["tmux"]).write_text("1")
    tk.tick()
    a, b = tk.rows("task")
    assert (a["status"], b["status"]) == ("done", "parked")
    assert "✅ Terminó · sin informe todavía" in texts(bot) and any(t.startswith("🅿️") for t in texts(bot))
    assert a["finished_at"]


def test_steer_running_task(tk, sh, bot):
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    before = t["updated_at"]
    tk.on_message(msg("mejor\nasí", ts="10.5", thread="10.1"))
    tk.tick()
    keys = sh.of("send-keys")
    assert keys[0][-5:] == ["-t", "=" + t["tmux"] + ":", "-l", "--", "mejor así"][-5:]
    assert keys[1][-1] == "Enter"
    assert tk.rows("steer")[0]["status"] == "delivered"
    assert bot.of("chat_postMessage")[-1]["text"] == "↪️ Enviado a la sesión"
    assert bot.of("chat_postMessage")[-1]["thread_ts"] == "10.1"
    assert tk.get(t["id"])["updated_at"] >= before


def test_steer_finished_task_opens_a_new_task(tk, sh, bot):
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    sh.live.clear()
    tk.on_message(msg("más", ts="10.5", thread="10.1"))
    tk.reap()
    tk.deliver_steers()  # a finished server session gets it through agent-bus (see test_steer_sweep.py)
    assert len(sh.of("deliver")) == 1
    assert tk.rows("steer")[0]["status"] == "rescued"  # the fake bus gave no outcome: the text is not lost
    new = tk.rows("task")[-1]
    assert new["id"] != t["id"] and new["thread_ts"] == t["thread_ts"]
    assert "más" in new["text"] and t["title"] in new["text"]
    assert "abre una tarea nueva" in texts(bot)[-1]


def test_laptop_steer_delivered_notice(tk, bot, conn, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    tk.on_message(msg("→ 3 hola"))
    assert cli.main(["steers"]) == 0  # the CLI uses its own connection
    assert tk.rows("steer")[0]["status"] == "new"
    tk.deliver_steers()  # a laptop steer is left alone
    assert tk.rows("steer")[0]["status"] == "new"
    sid = tk.rows("steer")[0]["id"]
    assert tk.mark_steer_done(sid) and not tk.mark_steer_done(sid)
    tk.tick()
    assert texts(bot)[-1] == "↪️ Enviado a la pestaña 3"
    tk.tick()
    assert texts(bot).count("↪️ Enviado a la pestaña 3") == 1


def test_card_outcome_typed_into_session(tk, sh, conn):
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    rid = db.create_request(conn, "decision", "¿Sí?", "c", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa task-" + t["id"],
                            options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    db.transition(conn, rid, "pending", "shown")
    tk.notify_outcomes()
    assert sh.of("send-keys") == []
    db.transition(conn, rid, "shown", "chosen", result_json=json.dumps({"option": "a"}))
    tk.notify_outcomes()
    tk.notify_outcomes()
    sent = [c for c in sh.of("send-keys") if "-l" in c]
    assert len(sent) == 1 and "eligió «a»" in sent[0][-1] and rid in sent[0][-1]


# -- CLI ----------------------------------------------------------------------
def test_cli_tasks_json(tk, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    c = db.connect()
    t = Tasks(c, None, OWNER)
    tid = t.create_task("D1", "1.1", "1.1", "haz algo")
    t.update(tid, status="running", tmux="task-" + tid, session_id="S1")
    assert cli.main(["tasks", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == tid and out[0]["tmux"] == "task-" + tid and out[0]["session_id"] == "S1"
    assert set(out[0]) >= {"id", "title", "status", "tmux", "session_id", "thread_ts", "created_at"}
    assert cli.main(["steer-done", "nope"]) == 1


def test_event_never_arriving_is_fine(polar, bot):
    polar.tick()  # no tasks, no events: only the first catch-up read, nothing is posted
    assert [c[0] for c in bot.calls] == ["conversations_history"]


# -- laptop placement ---------------------------------------------------------
def test_grace_blocks_server_launch(tk, sh, bot, monkeypatch):
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "120")
    tk.on_message(msg("haz algo"))
    tk.conn.execute("UPDATE tasks SET where_hint='laptop' WHERE kind='task'")
    tk.tick()
    assert sh.of("new-session") == []
    assert tk.rows("task")[0]["status"] == "queued"
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "0")
    tk.tick()
    assert len(sh.of("new-session")) == 1
    assert tk.rows("task")[0]["host"] == "server"


def test_default_is_server_at_once(tk, sh, bot, monkeypatch):
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "120")
    tk.on_message(msg("haz algo"))
    tk.tick()
    assert tk.rows("task")[0]["where_hint"] == "server"
    assert len(sh.of("new-session")) == 1


def test_queue_lists_queued(bot, sh, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    tk = Tasks(db.connect(), bot, OWNER, sh=sh)
    tk.on_message(msg("haz algo"))
    assert cli.main(["queue", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["text"] == "haz algo" and out[0]["thread_ts"] == "10.1"


def test_queue_marks_overflow_after_server_block(bot, sh, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    tk = Tasks(db.connect(), bot, OWNER, sh=sh)
    tk.on_message(msg("haz algo"))
    assert cli.main(["queue", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["overflow"] is False
    tk.update(tk.rows("task")[0]["id"], queue_notified=1)  # the server launcher could not start it
    assert cli.main(["queue", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["overflow"] is True


def test_claim_is_atomic_and_posts(tk, sh, bot):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    assert tk.claim(tid, "laptop", "5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "p_9")
    assert not tk.claim(tid, "laptop", "5", "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "p_9")
    tk.notify_claims()
    tk.notify_claims()
    t = tk.rows("task")[0]
    assert (t["status"], t["host"], t["session_id"]) == ("running", "laptop", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert texts(bot)[-1] == '▶️ Abrí "haz algo" en la laptop (pestaña 5).'
    tk.tick()  # no launch, no reap, no tmux steering
    assert sh.of("new-session") == []
    assert tk.rows("task")[0]["status"] == "running"


def test_claim_fails_after_server_launch(tk, sh):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.tick()
    assert not tk.claim(tid, "laptop", "5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def test_laptop_task_steer_goes_to_laptop(tk, sh, bot):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.claim(tid, "laptop", "5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "p_9")
    tk.on_message(msg("sigue", ts="10.2", thread="10.1"))
    tk.deliver_steers()
    assert sh.of("send-keys") == []
    assert tk.rows("steer")[0]["status"] == "new"
    st = tk.laptop_steers()
    assert len(st) == 1 and st[0]["requester"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb p_9"


def test_card_reply_from_laptop_task_steers_it(tk, conn):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    tk.claim(tid, "laptop", "5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "p_9")
    rid = db.create_request(conn, "decision", "¿Sí?", "c", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb p_9",
                            options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    conn.execute("UPDATE requests SET dm_ts='20.1' WHERE id=?", (rid,))
    tk.on_message(msg("sí", ts="20.2", thread="20.1"))
    assert tk.laptop_steers()[0]["requester"] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb p_9"


def test_task_done_once(tk, bot):
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    assert not tk.task_done(tid)  # not running yet
    tk.claim(tid, "laptop", "5", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert tk.task_done(tid) and not tk.task_done(tid)
    assert tk.rows("task")[0]["status"] == "done"
    tk.tick()
    assert texts(bot)[-1] == "✅ Terminó · sin informe todavía"


def test_cli_claim_exit_code(bot, sh, monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    tk = Tasks(db.connect(), bot, OWNER, sh=sh)
    tk.on_message(msg("haz algo"))
    tid = tk.rows("task")[0]["id"]
    assert cli.main(["claim", tid, "--host", "laptop", "--tab", "5", "--session", "S"]) == 0
    assert cli.main(["claim", tid, "--host", "laptop", "--tab", "5", "--session", "S"]) == 1
    assert cli.main(["task-done", tid]) == 0


def test_forwarded_text_reads_shared_attachment():
    from polar.tasks import forwarded_text
    ev = {"text": "", "attachments": [{"is_share": True, "author_name": "Sam Rivera", "channel_name": "ops",
                                       "text": "me ayudas con la conciliación", "from_url": "https://x.slack.com/archives/C1/p1"}]}
    out = forwarded_text(ev)
    assert "Sam Rivera" in out and "#ops" in out and "conciliación" in out and "https://x.slack.com" in out
    assert forwarded_text({"attachments": [{"title": "link preview", "text": "x"}]}) == ""


def test_handoff_is_laptop_only(env, tk):
    from polar import cli
    tid = tk.create_handoff("Revisar plantilla", "Mira el estado de penalidades_semana en respond.io",
                            "abc task-12345678")
    row = tk.conn.execute("SELECT status, where_hint, text, requester, channel FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "queued" and row["where_hint"] == "laptop-only"
    assert "herdr-deliver --tmux task-12345678" in row["text"] and row["requester"] == "abc task-12345678"
    tk.launch_queued()  # the server never starts a handoff
    assert tk.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"] == "queued"
    assert any(q["id"] == tid for q in tk.queue_list())



def test_handoff_result_goes_by_session_id(env, tk):
    """The result goes to the session id with agent-bus deliver, not to the tmux name: agent-bus resumes a parked
    or closed session as task-w<sid8>, so a «herdr-deliver --tmux task-<task id>» reached nobody (2026-11-22)."""
    sid = "13a148ef-0000-4000-8000-000000000001"
    tid = tk.create_handoff("Leer Drive", "Lista la carpeta", sid + " task-ce04b969")
    text = tk.conn.execute("SELECT text FROM tasks WHERE id=?", (tid,)).fetchone()["text"]
    assert "agent-bus deliver " + sid + " --ref handoff:" in text
    assert "--place task-ce04b969" in text and "--text-file - <<'RESULTADO'" in text
    assert "herdr-deliver" not in text


def test_handoff_refs_differ(env, tk):
    sid = "13a148ef-0000-4000-8000-000000000001"
    refs = set()
    for title in ("uno", "dos"):
        tid = tk.create_handoff(title, "paso " + title, sid + " task-ce04b969")
        text = tk.conn.execute("SELECT text FROM tasks WHERE id=?", (tid,)).fetchone()["text"]
        refs.add(text.split("--ref ")[1].split()[0])
    assert len(refs) == 2  # a second handoff of the same session is not a duplicate delivery


def test_handoff_fallback_printf_keeps_result(env, tk):
    tid = tk.create_handoff("Revisar", "Mira algo", "abc task-12345678")
    text = tk.conn.execute("SELECT text FROM tasks WHERE id=?", (tid,)).fetchone()["text"]
    assert "printf '%s' '<resultado>'" in text  # '%%s' printed a literal %s and dropped the result


# -- watchdog over coord-tick ---------------------------------------------------------------------------
def _posts(bot):
    return [kw["text"] for m, kw in bot.calls if m == "chat_postMessage"]


def test_watch_coord_quiet_without_heartbeat(tk, bot, monkeypatch, tmp_path):
    tk._coord_checked = 0.0
    monkeypatch.setenv("POLAR_COORD_HEARTBEAT", str(tmp_path / "nope"))
    tk.watch_coord(now=1e9)
    assert _posts(bot) == []


def test_watch_coord_alarm_once_then_recovered(tk, bot, monkeypatch, tmp_path):
    tk._coord_checked = 0.0  # past the start grace
    hb = tmp_path / "heartbeat"
    hb.write_text("")
    os.utime(hb, (1000, 1000))
    monkeypatch.setenv("POLAR_COORD_HEARTBEAT", str(hb))
    tk.watch_coord(now=1000 + 60)           # fresh: quiet
    assert _posts(bot) == []
    tk.watch_coord(now=1000 + 20 * 60)      # 20 min stale: one alarm to the owner DM
    posts = _posts(bot)
    assert len(posts) == 1 and "coord-tick lleva 20 min" in posts[0]
    assert [kw["channel"] for m, kw in bot.calls if m == "chat_postMessage"] == ["D0000000000"]
    tk.watch_coord(now=1000 + 30 * 60)      # still stale, inside 2 h: no repeat
    assert len(_posts(bot)) == 1
    tk.watch_coord(now=1000 + 30 * 60 + 100)  # inside the 5 min check gap anyway
    assert len(_posts(bot)) == 1
    tk.watch_coord(now=1000 + 20 * 60 + 2 * 3600)  # 2 h later: repeat
    assert len(_posts(bot)) == 2
    os.utime(hb, (20000, 20000))
    tk.watch_coord(now=20000 + 60)           # beats again: one recovered DM, then quiet
    assert "volvió" in _posts(bot)[-1] and len(_posts(bot)) == 3
    tk.watch_coord(now=20000 + 600)
    assert len(_posts(bot)) == 3


def test_watch_coord_waits_after_start(tk, bot, monkeypatch, tmp_path):
    hb = tmp_path / "heartbeat"
    hb.write_text("")
    os.utime(hb, (1000, 1000))  # stale from before a reboot
    monkeypatch.setenv("POLAR_COORD_HEARTBEAT", str(hb))
    tk.watch_coord()  # right after start: no look yet
    assert _posts(bot) == []


def _running_with_card(tk, conn, status, result=None, kind="decision"):
    tk.on_message(msg("haz algo"))
    tk.tick()
    t = tk.rows("task")[0]
    rid = db.create_request(conn, "decision", "¿Sí?", "c", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa task-" + t["id"],
                            options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    if kind == "report":
        conn.execute("UPDATE requests SET kind='report' WHERE id=?", (rid,))
    db.transition(conn, rid, "pending", "shown")
    db.transition(conn, rid, "shown", status, result_json=json.dumps(result or {}))
    return rid


def test_outcome_not_noted_when_typing_fails(tk, sh, conn):
    rid = _running_with_card(tk, conn, "chosen", {"option": "a"})
    sh.send_rc = 1
    tk.notify_outcomes()
    assert conn.execute("SELECT 1 FROM task_notes WHERE request_id=?", (rid,)).fetchone() is None
    sh.send_rc = 0
    tk.notify_outcomes()
    tk.notify_outcomes()
    sent = [c for c in sh.of("send-keys") if "-l" in c and rid in c[-1]]
    assert len(sent) == 2  # one failed try, one that worked
    assert conn.execute("SELECT count(*) FROM task_notes WHERE request_id=?", (rid,)).fetchone()[0] == 1


def test_outcome_report_chosen_not_typed(tk, sh, conn):
    rid = _running_with_card(tk, conn, "chosen", {"option": "ok"}, kind="report")
    tk.notify_outcomes()
    assert [c for c in sh.of("send-keys") if "-l" in c] == []
    assert conn.execute("SELECT 1 FROM task_notes WHERE request_id=?", (rid,)).fetchone()


def test_fable_at_its_limit_starts_on_opus(monkeypatch):
    """A [difícil] task starts on opus while POLAR_MODEL_FULL_CMD (plus «fable») exits 0. Unset: no check."""
    from polar import tasks as T
    monkeypatch.delenv("AGENT_TASK_MODEL", raising=False)
    monkeypatch.delenv("POLAR_MODEL_FULL_CMD", raising=False)
    seen = []
    rc = {"v": 0}

    def fake(argv, **k):
        seen.append(argv)
        return type("R", (), {"returncode": rc["v"]})()
    monkeypatch.setattr(T.subprocess, "run", fake)
    assert T.model_flag("[difícil] algo") == "--model fable "      # unset: no-op, no call
    assert seen == []
    monkeypatch.setenv("POLAR_MODEL_FULL_CMD", "my-tool model-full")
    assert T.model_flag("[difícil] algo") == "--model opus "
    assert seen[-1] == ["my-tool", "model-full", "fable"]
    rc["v"] = 1
    assert T.model_flag("[difícil] algo") == "--model fable "

    def boom(*a, **k):
        raise OSError("no tool")
    monkeypatch.setattr(T.subprocess, "run", boom)
    assert T.model_flag("[difícil] algo") == "--model fable "
    assert T.model_flag("normal") == "--model opus "


def test_brief_extra_rules_and_server_name(monkeypatch):
    from polar import tasks as T
    monkeypatch.delenv("POLAR_TASK_RULES", raising=False)
    assert T._extra_rules() == "."
    monkeypatch.setenv("POLAR_TASK_RULES", "; use the tool X.")
    assert T._extra_rules() == "; use the tool X."
    monkeypatch.setenv("POLAR_TASK_RULES", "Use Y.")
    assert T._extra_rules() == ". Use Y."
