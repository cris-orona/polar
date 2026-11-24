import json

import pytest

from polar import cli, db
from polar.tasks import Tasks, validate_plan
from tests.conftest import OWNER
from tests.test_tasks import Sh, msg
from tests.test_triage import env  # noqa: F401  (autouse env)


@pytest.fixture
def tk(conn, bot):
    return Tasks(conn, bot, OWNER, sh=Sh(), sleep=lambda s: None)


def plan(n=3, **over):
    p = {"summary": "Primero uno.", "groups": [
        {"key": "now", "items": [{"i": 0, "title": "Uno", "reason": "dinero", "checked": True}]},
        {"key": "running", "items": [{"i": 1, "title": "Dos", "reason": "ya va", "session": "sess-9",
                                      "label": "tab 4 · Dos", "checked": False}]},
        {"key": "drop", "items": [{"i": 2, "title": "Tres", "reason": "duplicado de uno"}]}]}
    p.update(over)
    return p


def make(tk):
    tk.on_message(msg("- uno\n- dos\n- tres"))
    return tk.conn.execute("SELECT id FROM picks").fetchone()["id"]


def test_picks_lists_planning(tk, capsys):
    pid = make(tk)
    out = tk.planning_picks()
    assert out[0]["id"] == pid and out[0]["items"] == ["uno", "dos", "tres"] and out[0]["source_ts"] == "10.1"


def test_plan_card_updates_placeholder(tk, bot):
    pid = make(tk)
    tk.plan_set(pid, plan())
    tk.process_plans()
    upd = bot.of("chat_update")[-1]
    assert upd["ts"] == "100.1"
    titles = [b["text"]["text"] for b in upd["blocks"] if b["type"] == "section"]
    assert "Recomiendo empezar" in titles[1] and "Ya en marcha" in titles[2] and "Descartar" in titles[3]
    assert not any("Después" in t for t in titles)
    boxes = [e for b in upd["blocks"] if b["type"] == "actions" for e in b["elements"] if e["type"] == "checkboxes"]
    assert [[o["value"] for o in e["options"]] for e in boxes] == [["0"], ["1"], ["2"]]
    assert [len(e.get("initial_options", [])) for e in boxes] == [1, 0, 0]
    btn = [e["text"]["text"] for e in upd["blocks"][-1]["elements"]]
    assert btn == ["Arrancar las marcadas", "Ver lista original"]
    assert tk.get_pick(pid)["status"] == "planned"


def test_plan_validation():
    with pytest.raises(ValueError, match="unknown group key"):
        validate_plan({"groups": [{"key": "soon", "items": []}]}, 1)
    with pytest.raises(ValueError, match="missing"):
        validate_plan({"groups": [{"key": "now", "items": [{"i": 0, "title": "a"}]}]}, 2)
    with pytest.raises(ValueError, match="more than once"):
        validate_plan({"groups": [{"key": "now", "items": [{"i": 0, "title": "a"}, {"i": 0, "title": "b"}]}]}, 1)
    with pytest.raises(ValueError, match="not in 0..0"):
        validate_plan({"groups": [{"key": "now", "items": [{"i": 3, "title": "a"}]}]}, 1)
    with pytest.raises(ValueError, match="unknown item field"):
        validate_plan({"groups": [{"key": "now", "items": [{"i": 0, "title": "a", "x": 1}]}]}, 1)
    ok = validate_plan({"groups": [{"key": "now", "items": [{"i": 0, "title": "t" * 90, "reason": "r" * 200}]}]}, 1)
    it = ok["groups"][0]["items"][0]
    assert len(it["title"]) == 60 and len(it["reason"]) == 140


def test_plan_many_items_split(tk, bot):
    tk.on_message(msg("\n".join("- t%d" % i for i in range(25))))
    pid = tk.conn.execute("SELECT id FROM picks").fetchone()["id"]
    tk.plan_set(pid, {"groups": [{"key": "later", "items": [{"i": i, "title": "t%d" % i} for i in range(25)]}]})
    tk.process_plans()
    bl = bot.of("chat_update")[-1]["blocks"]
    boxes = [e for b in bl if b["type"] == "actions" for e in b["elements"] if e["type"] == "checkboxes"]
    assert [len(e["options"]) for e in boxes] == [10, 10, 5]
    assert len({e["action_id"] for e in boxes}) == 3


def test_plan_set_on_open_pick_posts_new_card(tk, bot):
    pid = make(tk)
    tk.conn.execute("UPDATE picks SET status='open'")
    tk.plan_set(pid, plan())
    tk.process_plans()
    new = bot.of("chat_postMessage")[-1]
    assert new["thread_ts"] == "10.1" and new["blocks"]
    old = bot.of("chat_update")[-1]
    assert old["ts"] == "100.1" and "Reemplazada por el plan" in json.dumps(old["blocks"], ensure_ascii=False)
    assert tk.get_pick(pid)["status"] == "planned"


def test_go_with_session_skips_triage(polar, bot, conn):
    polar.on_message(msg("- uno\n- dos\n- tres"))
    pid = conn.execute("SELECT id FROM picks").fetchone()["id"]
    polar.tasks.plan_set(pid, plan())
    polar.tasks.process_plans()
    state = {"values": {"pick_0": {"polar_pick_check:0": {"selected_options": [{"value": "0"}]}},
                        "pick_1": {"polar_pick_check:1": {"selected_options": [{"value": "1"}]}}}}
    polar.on_pick_go(OWNER, pid, state, "D1", "100.1")
    tasks = polar.tasks.rows("task")
    by = {t["text"]: t for t in tasks}
    assert by["uno"]["status"] == "triage"
    assert by["dos"]["status"] == "matched" and by["dos"]["match_session"] == "sess-9"
    polar.tasks.process_triage()
    t = polar.tasks.get(by["dos"]["id"])
    assert t["status"] == "routed" and t["match_conf"] == "high" and t["route_done"] == 0


def test_original_list(polar, bot, conn):
    polar.on_message(msg("- uno\n- dos\n- tres"))
    pid = conn.execute("SELECT id FROM picks").fetchone()["id"]
    polar.on_pick_orig(OWNER, pid)
    last = bot.of("chat_postMessage")[-1]
    assert last["thread_ts"] == "10.1" and "1. uno" in last["text"] and "3. tres" in last["text"]


def test_cli_picks_and_plan_set(capsys, monkeypatch, tmp_path, io=None):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "d"))
    t = Tasks(db.connect(), None, OWNER)
    t.conn.execute("INSERT INTO picks (id, channel, source_ts, items_json, status, created_at) VALUES"
                   " ('p1','D1','1.1',?, 'planning', ?)", (json.dumps(["a", "b"]), db.now_iso()))
    assert cli.main(["picks", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "p1"
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"groups": [{"key": "now", "items": [{"i": 0, "title": "a"}]}]}))
    assert cli.main(["plan-set", "p1", "--file", str(f)]) == 2  # item 1 missing
    assert "missing from the plan" in capsys.readouterr().out
    f.write_text(json.dumps({"groups": [{"key": "now", "items": [{"i": 0, "title": "a"}, {"i": 1, "title": "b"}]}]}))
    assert cli.main(["plan-set", "p1", "--file", str(f)]) == 0
    assert t.get_pick("p1")["status"] == "plan_ready"
