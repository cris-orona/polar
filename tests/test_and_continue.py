from polar import blocks, cli, db
from tests.conftest import OWNER
from tests.test_core import shown
from tests.test_webapi import act, web  # noqa: F401


def _buttons(req):
    return [e["action_id"] for e in blocks.request_blocks(req)[-1]["elements"]]


def test_button_on_decision_bound_to_first_option(polar, decision_id):
    req = db.get(polar.conn, decision_id)
    assert "polar_choose_go:go" in _buttons(req)
    btn = [e for e in blocks.request_blocks(req)[-1]["elements"] if e["action_id"].startswith("polar_choose_go")][0]
    assert btn["text"]["text"] == "Sí, y sigue"


def test_no_button_for_prod_op_or_other_kinds(polar, conn):
    rid = db.create_request(conn, "decision", "Go?", "x", "prod-op abc123",
                            options=[{"key": "go", "label": "Go"}, {"key": "no", "label": "No"}])
    assert not any(a.startswith("polar_choose_go") for a in _buttons(db.get(conn, rid)))
    rid2 = db.create_request(conn, "report", "r", "x", "s1", draft_text="body")
    assert not any(a.startswith("polar_choose_go") for a in _buttons(db.get(conn, rid2)))


def test_choose_with_and_continue(polar, bot, decision_id):
    shown(polar, decision_id)
    polar.on_choose(OWNER, decision_id, "go", and_continue=True)
    req = db.get(polar.conn, decision_id)
    assert req["status"] == "chosen" and req["and_continue"] == 1 and req["result"] == {"option": "go"}
    assert bot.of("chat_update")[-1]["blocks"][-1]["text"]["text"] == "👉 Elegiste «Go» · y sigue"
    assert cli._public(req)["and_continue"] == 1


def test_plain_choose_keeps_flag_off(polar, decision_id):
    shown(polar, decision_id)
    polar.on_choose(OWNER, decision_id, "go")
    req = db.get(polar.conn, decision_id)
    assert req["and_continue"] == 0 and cli._public(req)["and_continue"] == 0


def test_prod_op_ignores_flag(polar, conn):
    rid = db.create_request(conn, "decision", "Go?", "x", "prod-op abc123",
                            options=[{"key": "go", "label": "Go"}, {"key": "no", "label": "No"}])
    shown(polar, rid)
    polar.on_choose(OWNER, rid, "go", and_continue=True)
    assert db.get(conn, rid)["and_continue"] == 0


def test_web_choose_flag(web, polar, decision_id):  # noqa: F811
    shown(polar, decision_id)
    assert act(web, decision_id, "choose", option="go", and_continue=True)[0] == 200
    assert db.get(polar.conn, decision_id)["and_continue"] == 1
