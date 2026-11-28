import json
import time

import pytest

from polar import cli, db
from polar.tasks import Tasks
from tests.conftest import OWNER
from tests.test_tasks import Sh, msg, texts


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("POLAR_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("POLAR_CLAUDE_BIN", "/x/claude")
    monkeypatch.setenv("POLAR_TMUX_BIN", "/x/tmux")
    monkeypatch.setenv("POLAR_CLAUDE_JSON", str(tmp_path / "claude.json"))
    monkeypatch.setenv("POLAR_PARKED_DIR", str(tmp_path / "parked"))
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "0")
    monkeypatch.delenv("POLAR_TRIAGE", raising=False)
    monkeypatch.delenv("POLAR_TRIAGE_TIMEOUT", raising=False)


@pytest.fixture
def sh():
    return Sh()


@pytest.fixture
def tk(conn, bot, sh):
    return Tasks(conn, bot, OWNER, sh=sh, sleep=lambda s: None)


def new_task(tk):
    tk.on_message(msg("Revisa el PR de Acme"))
    return tk.rows("task")[0]["id"]


def test_task_waits_in_triage_and_does_not_launch(tk, sh):
    tid = new_task(tk)
    tk.tick()
    assert tk.get(tid)["status"] == "triage" and sh.of("new-session") == []


def test_none_goes_to_queue_and_launches(tk, sh):
    tid = new_task(tk)
    assert tk.triage_set(tid, None)
    tk.tick()
    assert tk.get(tid)["status"] == "running" and len(sh.of("new-session")) == 1
    assert not tk.triage_set(tid, None)  # only a triage task accepts an answer


def test_low_or_bad_confidence_is_none(tk):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 2", "low", "x")
    assert tk.get(tid)["status"] == "queued"


def test_high_routes_with_button(tk, bot, sh):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3 · Acme", "high", "mismo PR")
    tk.tick()
    t = tk.get(tid)
    assert t["status"] == "routed" and sh.of("new-session") == []
    call = bot.of("chat_update")[-1]  # the «Recibido» message turns into the route line
    assert call["text"] == "➡️ Lo mandé a tab 3 · Acme: mismo PR"
    btn = call["blocks"][1]["elements"][0]
    assert btn["action_id"] == "polar_route_new" and btn["text"]["text"] == "No, sesión nueva"
    assert t["card_ts"]


def test_no_button_puts_back_to_queue(tk, polar_like=None):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3", "high", "r")
    tk.tick()
    assert tk.route_new(tid)
    assert tk.get(tid)["status"] == "queued"


def test_no_button_after_delivery_does_nothing(tk):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3", "high", "r")
    tk.tick()
    assert tk.mark_route_done(tid)
    assert not tk.route_new(tid)
    assert tk.get(tid)["status"] == "routed"


def test_medium_routes_at_once(tk, bot):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3 · Acme", "medium", "parece lo mismo")
    tk.tick()
    t = tk.get(tid)
    assert t["status"] == "routed" and t["route_done"] == 0
    assert not any("¿Es de" in x for x in texts(bot))
    call = [c for c in bot.of("chat_postMessage") + bot.of("chat_update") if "coincidencia probable" in c["text"]][-1]
    assert call["text"] == "➡️ Lo mandé a tab 3 · Acme (coincidencia probable): parece lo mismo"
    assert "polar_route_new" in str(call["blocks"])


def test_legacy_confirm_row_route_yes(tk, bot):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3 · Acme", "medium", "parece lo mismo")
    tk.update(tid, status="confirm")  # a row from an older version
    tk.tick()
    assert tk.get(tid)["status"] == "confirm"
    assert tk.route_yes(tid)
    assert tk.get(tid)["status"] == "routed"
    assert bot.of("chat_update")


def test_legacy_confirm_row_new_session(tk):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3", "medium", "r")
    tk.update(tid, status="confirm")
    tk.tick()
    assert tk.route_new(tid) and tk.get(tid)["status"] == "queued"


def test_triage_timeout_default_and_notice(tk, bot, conn):
    assert tk.triage_timeout() == 300
    tid = new_task(tk)
    tk.tick()
    assert tk.get(tid)["status"] == "triage"
    conn.execute("UPDATE tasks SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (tid,))
    tk.expire_triage()
    assert tk.get(tid)["status"] == "queued"
    assert "Sin respuesta de la laptop; abro sesión nueva." in texts(bot)


def test_confirm_timeout(tk, bot, conn):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3", "medium", "r")
    tk.update(tid, status="confirm")
    tk.tick()
    conn.execute("UPDATE tasks SET updated_at='2020-01-01T00:00:00Z' WHERE id=?", (tid,))
    tk.expire_triage()
    assert tk.get(tid)["status"] == "queued"


def test_pick_items_go_through_triage(tk, bot):
    tk.on_message(msg("- a\n- b\n- c"))
    tk.conn.execute("UPDATE picks SET status='open'")
    pid = tk.conn.execute("SELECT id FROM picks").fetchone()["id"]
    ids = tk.start_pick(pid, [0, 2])
    assert [tk.get(i)["status"] for i in ids] == ["triage", "triage"]
    assert len({tk.get(i)["thread_ts"] for i in ids}) == 2  # each item has its own thread
    assert all("Reviso si ya hay una sesión" in t for t in texts(bot)[-2:])


def test_triage_off(tk, monkeypatch):
    monkeypatch.setenv("POLAR_TRIAGE", "0")
    assert tk.get(new_task(tk))["status"] == "queued"


def test_cli_roundtrip(capsys, monkeypatch, tmp_path, tk):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))
    tid = new_task(tk)  # the tk fixture uses the same data dir database? use cli db
    conn = db.connect()
    t = Tasks(conn, None, OWNER)
    tid = t._insert("task", "triage", "T", "texto", "D1", "9.9", "9.9")

    def run(*a):
        code = cli.main(list(a))
        return code, [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]

    _, out = run("triage", "--json")
    assert [x["id"] for x in out[0]] == [tid] and out[0][0]["text"] == "texto"
    code, _ = run("triage-set", tid, "--match", "S1")
    assert code == 2  # needs label and confidence
    code, out = run("triage-set", tid, "--match", "S1", "--label", "tab 1 · x", "--confidence", "high",
                    "--reason", "mismo tema")
    assert code == 0 and out[0]["set"]
    assert run("triage-set", tid, "--none")[0] == 1  # already answered
    t2 = Tasks(conn, type("B", (), {"chat_postMessage": lambda s, **k: {"ts": "1.1"}})(), OWNER)
    t2.process_triage()
    _, out = run("routes")
    assert out[0][0]["match_session"] == "S1" and out[0][0]["text"] == "texto" and out[0][0]["thread_ts"] == "9.9"
    assert run("route-done", tid)[1][0]["delivered"] is True
    assert run("routes")[1][0] == []
    assert run("route-done", tid)[0] == 1


def test_buttons_work_for_owner_only(polar, bot):
    polar.tasks.sh = Sh()
    polar.tasks.on_message(msg("tarea"))
    tid = polar.tasks.rows("task")[0]["id"]
    polar.tasks.triage_set(tid, "S1", "tab 1", "medium", "r")
    polar.tasks.update(tid, status="confirm")
    polar.tasks.tick()
    polar.on_route_yes("U9", tid)
    assert polar.tasks.get(tid)["status"] == "confirm"
    polar.on_route_yes(OWNER, tid)
    assert polar.tasks.get(tid)["status"] == "routed"


def test_route_failed_opens_new_session(tk, bot):
    tid = new_task(tk)
    tk.triage_set(tid, "S1", "tab 3", "high", "r")
    tk.tick()
    assert tk.route_failed(tid)
    tk.tick()
    assert tk.get(tid)["status"] in ("queued", "running")
    assert any("No pude entregarlo a tab 3" in t for t in texts(bot)) or bot.of("chat_update")


def test_where_server_skips_grace_and_laptop_keeps_it(tk, sh, monkeypatch):
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "120")
    a = new_task(tk)
    assert tk.triage_set(a, None, where="server")
    assert tk.get(a)["where_hint"] == "server"
    tk.tick()
    assert tk.get(a)["status"] == "running" and len(sh.of("new-session")) == 1


def test_where_laptop_keeps_grace(tk, sh, monkeypatch):
    monkeypatch.setenv("POLAR_LAPTOP_GRACE", "120")
    a = new_task(tk)
    assert tk.triage_set(a, None, where="laptop")
    assert tk.get(a)["where_hint"] == "laptop"
    tk.tick()
    assert tk.get(a)["status"] == "queued" and sh.of("new-session") == []


def test_where_bad_value_refused(tk):
    a = new_task(tk)
    assert not tk.triage_set(a, None, where="moon")


def test_cli_where_and_queue_json(bot, sh, capsys):
    tk = Tasks(db.connect(), bot, OWNER, sh=sh)
    a = new_task(tk)
    assert cli.main(["triage-set", a, "--none", "--where", "server"]) == 0
    capsys.readouterr()
    assert cli.main(["queue", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == a and out[0]["where_hint"] == "server"


def test_plan_item_where_validated():
    from polar.tasks import validate_plan
    good = {"groups": [{"key": "now", "items": [{"i": 0, "title": "t", "where": "server"}]}]}
    assert validate_plan(good, 1)["groups"][0]["items"][0]["where"] == "server"
    bad = {"groups": [{"key": "now", "items": [{"i": 0, "title": "t", "where": "x"}]}]}
    with pytest.raises(ValueError):
        validate_plan(bad, 1)
