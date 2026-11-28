"""The web action `respond`: one «Responder» answer on any card, off until POLAR_RESPOND=on."""
import json
import threading

import pytest

from polar import db, webapi
from tests.test_webapi import TOKEN, QS, act, call, shown

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv("POLAR_RESPOND", "on")
    monkeypatch.setenv("POLAR_WEB_ALLOW", "send,prod")


@pytest.fixture
def web(polar):
    server = webapi.WebServer(0, polar, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def decision(conn, requester="s"):
    return db.create_request(conn, "decision", "¿Qué hacemos?", "c", requester,
                             options=[{"key": "a", "label": "Opción A"}, {"key": "b", "label": "Opción B"}])


def test_off_by_default(web, polar, conn, monkeypatch):
    monkeypatch.delenv("POLAR_RESPOND")
    rid = decision(conn)
    shown(polar, rid)
    assert call(web, "GET", "/health")[1]["allow"] == ["send", "prod"]
    code, out = act(web, rid, "respond", option="a")
    assert code == 403 and out["error"] == "La caja Responder está apagada."
    assert db.get(conn, rid)["status"] == "shown"


def test_health_lists_respond_when_on(web):
    assert call(web, "GET", "/health")[1]["allow"] == ["send", "prod", "respond"]


def test_nothing_given_is_400(web, polar, conn):
    rid = decision(conn)
    shown(polar, rid)
    assert act(web, rid, "respond")[0] == 400
    assert act(web, rid, "respond", note="  ")[0] == 400
    assert act(web, rid, "respond", note="x" * (db.MAX_REPLY_CHARS + 1))[0] == 400
    assert act(web, rid, "respond", option="a", answers={"x": "y"})[0] == 400
    assert db.get(conn, rid)["status"] == "shown"


def test_decision_option_with_note(web, polar, conn):
    rid = decision(conn)
    shown(polar, rid)
    assert act(web, rid, "respond", option="zzz")[0] == 400
    assert act(web, rid, "respond", indexes=[0])[0] == 400
    assert act(web, rid, "respond", option="b", note="con cuidado") == (200, {"ok": True, "status": "chosen"})
    res = db.get(conn, rid)["result"]
    assert res == {"option": "b", "src": "web", "respond_summary": "eligió 2) Opción B",
                   "respond_note": "con cuidado"}


def test_decision_option_without_note_and_continue(web, polar, conn):
    rid = decision(conn)
    shown(polar, rid)
    assert act(web, rid, "respond", option="a", and_continue=True)[0] == 200
    req = db.get(conn, rid)
    assert req["result"]["respond_summary"] == "eligió 1) Opción A"
    assert "respond_note" not in req["result"]


def test_decision_note_only_is_revise(web, polar, conn):
    rid = decision(conn)
    shown(polar, rid)
    assert act(web, rid, "respond", note="Falta C") == (200, {"ok": True, "status": "revise"})
    assert db.get(conn, rid)["result"] == {"instructions": "Falta C", "src": "web",
                                            "respond_summary": "pidió cambios", "respond_note": "Falta C"}


def test_prod_op_gate_kept(web, polar, conn, monkeypatch):
    monkeypatch.setenv("POLAR_WEB_ALLOW", "")
    rid = decision(conn, "prod-op abc")
    shown(polar, rid)
    assert act(web, rid, "respond", option="a")[0] == 403
    assert db.get(conn, rid)["status"] == "shown"


def test_questions_answers_with_note(web, polar, conn):
    rid = db.create_request(conn, "questions", "Q", "c", "s", options=QS)
    shown(polar, rid)
    assert act(web, rid, "respond", answers={"alerta": "si"})[0] == 400
    assert act(web, rid, "respond", option="si")[0] == 400
    assert act(web, rid, "respond", answers={"alerta": "si", "umbral": "Cinco"}, note="ojo") == (
        200, {"ok": True, "status": "answered"})
    res = db.get(conn, rid)["result"]
    assert res["note"] == "ojo" and res["respond_note"] == "ojo"
    assert res["respond_summary"] == "eligió 1) Sí, 2) Cinco"
    assert res["answers"]["umbral"]["text"] == "Cinco"


def test_questions_note_only_is_revise(web, polar, conn):
    rid = db.create_request(conn, "questions", "Q", "c", "s", options=QS)
    shown(polar, rid)
    assert act(web, rid, "respond", note="Otra pregunta")[1]["status"] == "revise"


def report(conn, **kw):
    return db.create_request(conn, "report", "R", "", "s1", draft_text="x", expires_hours=72,
                             report=dict(next=["Sigue con X", "Otra", "Tercera"], **kw))


def test_report_indexes_with_note(web, polar, conn):
    rid = report(conn)
    shown(polar, rid)
    assert act(web, rid, "respond", indexes=[5])[0] == 400
    assert act(web, rid, "respond", indexes=[0, 0])[0] == 400
    assert act(web, rid, "respond", indexes=[0], option="ok")[0] == 400
    assert act(web, rid, "respond", indexes=[2, 0], note="rápido") == (200, {"ok": True, "status": "revise"})
    res = db.get(conn, rid)["result"]
    assert res["via"] == "next" and res["indexes"] == [0, 2]
    assert res["respond_summary"] == "eligió 1) Sigue con X, 3) Tercera"
    assert res["respond_note"] == "rápido"


def test_report_ok_resume_and_note_only(web, polar, conn):
    rid = report(conn)
    shown(polar, rid)
    assert act(web, rid, "respond", option="resume")[0] == 400  # no resume on this report
    assert act(web, rid, "respond", option="ok", note="gracias") == (200, {"ok": True, "status": "chosen"})
    assert db.get(conn, rid)["result"]["respond_note"] == "gracias"
    rid2 = report(conn)
    shown(polar, rid2)
    assert act(web, rid2, "respond", note="cambia")[1]["status"] == "revise"


def test_reply_only_note(web, polar, conn, reply_id):
    shown(polar, reply_id)
    assert act(web, reply_id, "respond", option="a")[0] == 400
    assert act(web, reply_id, "respond", indexes=[0])[0] == 400
    assert act(web, reply_id, "respond", note="Más corto") == (200, {"ok": True, "status": "revise"})


def test_repeat_is_409(web, polar, conn):
    rid = decision(conn, SID + " pane")
    shown(polar, rid)
    assert act(web, rid, "respond", option="a")[0] == 200
    assert act(web, rid, "respond", option="b")[0] == 409


@pytest.fixture
def fake_bus(tmp_path, monkeypatch):
    out = tmp_path / "bus.json"
    bus = tmp_path / "bus.py"
    bus.write_text("import json, sys\njson.dump({'argv': sys.argv[1:], 'in': sys.stdin.read()}, open(%r, 'w'))\n"
                   "sys.exit(int(open(%r).read() or 0) if False else 0)\n" % (str(out), str(out)))
    monkeypatch.setenv("AGENT_BUS_BIN", str(bus))
    return out


def test_closed_card_note_is_delivered(web, polar, conn, fake_bus):
    rid = decision(conn, SID + " pane-1")
    shown(polar, rid)
    polar.on_choose(polar.owner_id, rid, "a")
    code, out = act(web, rid, "respond", note="una cosa más")
    assert (code, out) == (200, {"ok": True, "status": "Entregado al agente."})
    got = json.loads(fake_bus.read_text())
    argv = got["argv"]
    assert argv[:2] == ["deliver", SID] and "--text-file" in argv and "-" in argv
    assert argv[argv.index("--from") + 1] == "polar"
    assert argv[argv.index("--ref") + 1].startswith("polar:%s:" % rid)
    assert argv[argv.index("--place") + 1] == "pane-1"
    assert got["in"] == ("Polar (respuesta): the owner wrote more about your card %s «¿Qué hacemos?». "
                         "Nota: una cosa más" % rid)
    notes = db.get(conn, rid)["result"]["owner_notes"]
    assert notes[0]["text"] == "una cosa más" and db.get(conn, rid)["result"]["option"] == "a"


def test_closed_card_without_agent(web, polar, conn, fake_bus):
    rid = decision(conn, "sin sesion")
    shown(polar, rid)
    polar.on_choose(polar.owner_id, rid, "a")
    code, out = act(web, rid, "respond", note="hola")
    assert code == 409 and out["error"] == "Esta tarjeta no tiene agente."
    assert not fake_bus.exists()


def test_closed_card_rules(web, polar, conn, fake_bus):
    rid = decision(conn, SID)
    shown(polar, rid)
    polar.on_choose(polar.owner_id, rid, "a")
    assert act(web, rid, "respond")[0] == 400
    assert act(web, rid, "respond", option="b", note="x")[0] == 409
    assert not fake_bus.exists()


def test_closed_card_bus_failure(web, polar, conn, monkeypatch, tmp_path):
    bus = tmp_path / "bad.py"
    bus.write_text("import sys\nsys.exit(3)\n")
    monkeypatch.setenv("AGENT_BUS_BIN", str(bus))
    rid = decision(conn, SID)
    shown(polar, rid)
    polar.on_choose(polar.owner_id, rid, "a")
    code, out = act(web, rid, "respond", note="x")
    assert code == 502 and "owner_notes" not in db.get(conn, rid)["result"]


def _moved_by_someone(polar, monkeypatch):
    def fake(owner, rid, key, **kw):
        db.transition(polar.conn, rid, "shown", "chosen", result_json=json.dumps({"option": "b", "src": "slack"}))
    monkeypatch.setattr(polar, "on_choose", fake)


def test_race_with_note_falls_back_to_delivery(web, polar, conn, fake_bus, monkeypatch):
    rid = decision(conn, SID + " pane")
    shown(polar, rid)
    _moved_by_someone(polar, monkeypatch)
    assert act(web, rid, "respond", option="a", note="ojo") == (200, {"ok": True, "status": "Entregado al agente."})
    assert json.loads(fake_bus.read_text())["in"].endswith("Nota: ojo")
    assert db.get(conn, rid)["result"]["option"] == "b"
    assert db.get(conn, rid)["result"]["owner_notes"][0]["text"] == "ojo"


def test_race_without_note_is_409(web, polar, conn, fake_bus, monkeypatch):
    rid = decision(conn, SID)
    shown(polar, rid)
    _moved_by_someone(polar, monkeypatch)
    code, out = act(web, rid, "respond", option="a")
    assert code == 409 and out["error"].startswith("La tarjeta ya cambió")
    assert not fake_bus.exists()


def test_ref_is_stable_per_note_and_place_is_checked(web, polar, conn, fake_bus):
    import hashlib
    rid = decision(conn, SID + " bad;pane")
    shown(polar, rid)
    polar.on_choose(polar.owner_id, rid, "a")
    assert act(web, rid, "respond", note="igual")[0] == 200
    argv = json.loads(fake_bus.read_text())["argv"]
    assert argv[argv.index("--ref") + 1] == "polar:%s:%s" % (rid, hashlib.sha1(b"igual").hexdigest()[:10])
    assert "--place" not in argv


def test_owner_note_keeps_a_result_that_changed(conn):
    rid = decision(conn)
    conn.execute("UPDATE requests SET result_json=? WHERE id=?", (json.dumps({"option": "a"}), rid))
    db.add_owner_note(conn, rid, "uno")
    conn.execute("UPDATE requests SET result_json=json_set(result_json,'$.x',1) WHERE id=?", (rid,))
    db.add_owner_note(conn, rid, "dos")
    res = db.get(conn, rid)["result"]
    assert res["option"] == "a" and res["x"] == 1 and [n["text"] for n in res["owner_notes"]] == ["uno", "dos"]
