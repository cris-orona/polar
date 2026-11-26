from polar import blocks, db


def test_reply_blocks(conn, reply_id):
    req = db.get(conn, reply_id)
    b = blocks.request_blocks(req)
    assert b[0]["type"] == "header"
    assert {"type": "markdown", "text": "Hola Ana"} in b
    acts = b[-1]["elements"]
    assert [a["text"]["text"] for a in acts] == ["Enviar", "Editar", "Descartar", "🔁 Pedir cambios"]
    assert acts[0]["style"] == "primary"
    assert all(a["value"] == reply_id for a in acts)


def test_edited_text_is_shown(conn, reply_id):
    db.transition(conn, reply_id, "pending", "shown")
    db.set_final_text(conn, reply_id, "Texto nuevo")
    b = blocks.request_blocks(db.get(conn, reply_id))
    assert any(x["type"] == "markdown" and "Texto nuevo" in x["text"] for x in b)


def test_decision_blocks(conn, decision_id):
    acts = blocks.request_blocks(db.get(conn, decision_id))[-1]["elements"]
    assert [a["text"]["text"] for a in acts] == ["Go", "Esperar", "Sí, y sigue", "🔁 Pedir cambios"]
    assert acts[0]["action_id"] == "polar_choose:go"


def test_long_text_is_clipped():
    assert len(blocks.clip("x" * 5000)) <= blocks.SECTION_LIMIT


def test_resolved_has_no_buttons(conn, reply_id):
    req = db.get(conn, reply_id)
    req.update(status="discarded")
    b = blocks.resolved_blocks(req)
    assert all(x["type"] != "actions" for x in b)
    assert b[-1]["text"]["text"] == "🗑️ Descartado"


def test_result_lines(conn, reply_id, decision_id):
    r = db.get(conn, reply_id)
    r.update(status="sent", decided_at="2026-11-18T21:04:00Z", result={"permalink": "https://l"})
    line = blocks.result_line(r)
    assert line.startswith("✅ Enviado a su canal") and "<https://l|" in line
    d = db.get(conn, decision_id)
    d.update(status="chosen", result={"option": "go"})
    assert blocks.result_line(d) == "👉 Elegiste: Go — ¿Release?"
    r.update(status="failed", result={"error": "channel_not_found"})
    assert "channel_not_found" in blocks.result_line(r)


def test_edit_modal(conn, reply_id):
    m = blocks.edit_modal(db.get(conn, reply_id))
    assert m["private_metadata"] == reply_id
    assert m["blocks"][0]["element"]["initial_value"] == "Hola Ana"
