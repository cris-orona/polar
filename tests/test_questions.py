import json

import pytest

from polar import blocks, cli, db
from tests.conftest import OWNER

STRANGER = "U999"
QS = [
    {"key": "alerta", "text": "¿Finanzas recibe alerta?",
     "options": [{"key": "si", "label": "Sí"}, {"key": "no", "label": "No"}], "recommended": "si"},
    {"key": "umbral", "text": "¿Qué umbral?"},
    {"key": "dato", "text": "¿Es correcto?", "options": [{"key": "ok", "label": "Correcto"}]},
]


@pytest.fixture
def qid(conn):
    return db.create_request(conn, "questions", "Alertas", "contexto", "s", options=QS)


def shown(polar, rid):
    polar.deliver_pending()
    return db.get(polar.conn, rid)


def card(bot):
    return bot.of("chat_update")[-1]["blocks"]


# -- CLI validation
def run_cli(tmp_path, monkeypatch, data, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "d"))
    f = tmp_path / "q.json"
    f.write_text(data if isinstance(data, str) else json.dumps(data))
    code = cli.main(["ask", "questions", "--title", "T", "--question-file", str(f), "--requester", "sess-1 pane"])
    return code, capsys.readouterr().out


def test_cli_ok_and_expiry_7_days(tmp_path, monkeypatch, capsys):
    code, out = run_cli(tmp_path, monkeypatch, QS, capsys)
    assert code == 0
    req = db.get(db.connect(tmp_path / "d" / "polar.db"), json.loads(out)["id"])
    assert req["kind"] == "questions" and len(req["options"]) == 3
    created = db.datetime.strptime(req["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    expires = db.datetime.strptime(req["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert (expires - created).days == 7


def test_cli_bad_json(tmp_path, monkeypatch, capsys):
    code, out = run_cli(tmp_path, monkeypatch, "{nope", capsys)
    assert code == 2 and "bad question JSON" in out


def test_cli_recommended_not_in_options(tmp_path, monkeypatch, capsys):
    bad = [{"key": "a", "text": "x", "options": [{"key": "si", "label": "Sí"}], "recommended": "no"}]
    code, out = run_cli(tmp_path, monkeypatch, bad, capsys)
    assert code == 2 and "recommended" in out


def test_cli_too_many_and_zero_questions(tmp_path, monkeypatch, capsys):
    six = [{"key": f"k{i}", "text": "x"} for i in range(6)]
    assert run_cli(tmp_path, monkeypatch, six, capsys)[0] == 2
    assert run_cli(tmp_path, monkeypatch, [], capsys)[0] == 2


def test_validate_more_than_4_options():
    opts = [{"key": f"o{i}", "label": "x"} for i in range(5)]
    with pytest.raises(ValueError):
        db.validate_questions([{"key": "a", "text": "x", "options": opts}])


# -- blocks
def test_blocks_unanswered(conn, qid):
    req = db.get(conn, qid)
    bl = blocks.request_blocks(req)
    assert bl[0]["text"]["text"] == "📝 Preguntas · Alertas"
    row = next(b for b in bl if b.get("block_id") == "polar_qrow_alerta")
    si, no, other = row["elements"]
    assert si["text"]["text"] == "Sí (recomendado)" and si["style"] == "primary"
    assert "style" not in no and other["text"]["text"] == "Otra…"
    free = next(b for b in bl if b.get("block_id") == "polar_qrow_umbral")
    assert [e["text"]["text"] for e in free["elements"]] == ["Otra…"]
    bottom = [e["text"]["text"] for e in bl[-1]["elements"]]
    assert bottom == ["✅ Aceptar lo recomendado", "📝 Agregar nota", "Enviar respuestas", "🔁 Pedir cambios"]


def test_blocks_answered_row_and_shrink(conn, qid):
    db.set_result(conn, qid, {"answers": {"umbral": {"option": None, "text": "5 mil"}}})
    db.transition(conn, qid, "pending", "shown")
    db.set_result(conn, qid, {"answers": {"umbral": {"option": None, "text": "5 mil"}}})
    req = db.get(conn, qid)
    row = next(b for b in blocks.request_blocks(req) if b.get("block_id") == "polar_q_umbral")
    assert "2." in row["text"]["text"] and "→ *5 mil*" in row["text"]["text"]
    assert row["accessory"]["text"]["text"] == "Cambiar"
    assert blocks.missing(req) == [1, 3]
    req["status"] = "answered"
    assert blocks.result_line(req) == "📝 Respondiste 3 preguntas · Alertas"


# -- core flow
def test_flow_button_modal_accept_note_send(polar, bot, qid):
    shown(polar, qid)
    polar.on_q_answer(OWNER, qid, "alerta", "no")
    assert db.get(polar.conn, qid)["result"]["answers"]["alerta"] == {"option": "no", "text": "No"}
    polar.on_q_other_click(OWNER, qid, "umbral", "trig")
    view = bot.of("views_open")[0]["view"]
    assert view["callback_id"] == "polar_q_submit"
    sub = {"private_metadata": view["private_metadata"],
           "state": {"values": {"text_block": {"text": {"value": " 5 mil pesos "}}}}}
    assert polar.on_q_submit(OWNER, sub) is None
    a = db.get(polar.conn, qid)["result"]["answers"]["umbral"]
    assert a == {"option": None, "text": "5 mil pesos"}
    # send refused: question 3 has no answer
    polar.on_q_send(OWNER, qid)
    assert db.get(polar.conn, qid)["status"] == "shown"
    assert "Faltan: 3" in json.dumps(card(bot), ensure_ascii=False)
    # note
    polar.on_q_note_click(OWNER, qid, "t2")
    meta = bot.of("views_open")[1]["view"]["private_metadata"]
    polar.on_q_submit(OWNER, {"private_metadata": meta,
                              "state": {"values": {"text_block": {"text": {"value": "ojo con X"}}}}})
    polar.on_q_answer(OWNER, qid, "dato", "ok")
    polar.on_q_send(OWNER, qid)
    req = db.get(polar.conn, qid)
    assert req["status"] == "answered" and req["decided_at"]
    assert req["result"]["note"] == "ojo con X"
    assert set(req["result"]["answers"]) == {"alerta", "umbral", "dato"}
    assert card(bot)[-1]["text"]["text"] == "📝 Respondiste 3 preguntas · Alertas"
    assert polar.deliver_pending() == 0
    # a final request does not change
    polar.on_q_reset(OWNER, qid, "alerta")
    assert "alerta" in db.get(polar.conn, qid)["result"]["answers"]


def test_accept_recommended_and_reset(polar, bot, qid):
    shown(polar, qid)
    polar.on_q_accept(OWNER, qid)
    ans = db.get(polar.conn, qid)["result"]["answers"]
    assert ans == {"alerta": {"option": "si", "text": "Sí"}}
    assert "Aceptar lo recomendado" not in json.dumps(card(bot), ensure_ascii=False)
    polar.on_q_reset(OWNER, qid, "alerta")
    assert db.get(polar.conn, qid)["result"].get("answers") == {}


def test_stranger_ignored(polar, bot, qid):
    shown(polar, qid)
    n = len(bot.of("chat_update"))
    polar.on_q_answer(STRANGER, qid, "alerta", "si")
    polar.on_q_accept(STRANGER, qid)
    polar.on_q_send(STRANGER, qid)
    polar.on_q_other_click(STRANGER, qid, "umbral", "t")
    polar.on_q_note_click(STRANGER, qid, "t")
    polar.on_q_reset(STRANGER, qid, "alerta")
    meta = json.dumps({"id": qid, "q": "umbral"})
    assert polar.on_q_submit(STRANGER, {"private_metadata": meta, "state": {"values": {
        "text_block": {"text": {"value": "hack"}}}}}) is None
    assert db.get(polar.conn, qid)["result"] == {}
    assert len(bot.of("chat_update")) == n and bot.of("views_open") == []


def test_empty_answer_refused_and_expiry(polar, qid):
    shown(polar, qid)
    meta = json.dumps({"id": qid, "q": "umbral"})
    err = polar.on_q_submit(OWNER, {"private_metadata": meta, "state": {"values": {
        "text_block": {"text": {"value": "  "}}}}})
    assert err["response_action"] == "errors"
    db.expire_due(polar.conn, "2999-01-01T00:00:00Z")
    assert db.get(polar.conn, qid)["status"] == "expired"


def test_status_output_has_answers(polar, qid, capsys):
    shown(polar, qid)
    polar.on_q_answer(OWNER, qid, "alerta", "si")
    pub = cli._public(db.get(polar.conn, qid))
    assert pub["answers"]["alerta"]["option"] == "si" and pub["questions"][0]["key"] == "alerta"
