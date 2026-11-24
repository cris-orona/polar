import json

import pytest

from polar import blocks, cli, db


def texts(req):
    return [b.get("text") if isinstance(b.get("text"), str) else (b.get("text") or {}).get("text", "")
            for b in blocks.request_blocks(req)]


def test_decision_body_and_links(conn):
    rid = db.create_request(
        conn, "decision", "Aprobar X", "porque sí", "s1",
        options=[{"key": "a", "label": "Sí"}, {"key": "b", "label": "No"}],
        extra={"body": "**Qué:** una propuesta", "links": ["PR=https://github.com/o/r/pull/1"]})
    req = db.get(conn, rid)
    bl = blocks.request_blocks(req)
    flat = json.dumps(bl, ensure_ascii=False)
    assert "📄 Qué es" in flat and "**Qué:** una propuesta" in flat
    assert "<https://github.com/o/r/pull/1|PR>" in flat
    idx = lambda s: next(i for i, b in enumerate(bl) if s in json.dumps(b, ensure_ascii=False))
    assert idx("Por qué") < idx("📄 Qué es") < idx("polar_actions")


def test_questions_body(conn):
    rid = db.create_request(conn, "questions", "Q", "ctx", "s1",
                            options=[{"key": "q1", "text": "¿Sí?", "options": []}],
                            extra={"body": "Detalle largo"})
    flat = json.dumps(blocks.request_blocks(db.get(conn, rid)), ensure_ascii=False)
    assert "📄 Qué es" in flat and "Detalle largo" in flat


def test_no_extra_unchanged(conn):
    rid = db.create_request(conn, "decision", "T", "c", "s1",
                            options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    assert "Qué es" not in json.dumps(blocks.request_blocks(db.get(conn, rid)))


def test_long_body_truncated():
    parts = blocks.body_sections("x" * 20000)
    assert len(parts) == 3 and all(len(p) <= 2800 for p in parts) and parts[-1].endswith("…")
    assert blocks.body_sections("corto") == ["corto"]


def test_bad_link_scheme(conn):
    with pytest.raises(ValueError):
        db.create_request(conn, "decision", "T", "c", "s1",
                          options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                          extra={"links": ["x=javascript:alert(1)"]})


def test_cli_flags_and_replaces(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    body = tmp_path / "b.md"
    body.write_text("Es una propuesta")
    att = tmp_path / "x.pdf"
    att.write_text("pdf")
    base = ["ask", "decision", "--title", "T", "--context", "Necesito tu decisión sobre esto hoy",
            "--option", "a:Sí", "--option", "b:No", "--requester", "sess-1 pane"]
    assert cli.main(base + ["--text-file", str(body), "--link", "L=https://a.b", "--file", str(att)]) == 0
    first = json.loads(capsys.readouterr().out)["id"]
    ex = db.get(db.connect(), first)["extra"]
    assert ex["body"] == "Es una propuesta"
    kept = ex["files"][0]  # a copy in the data folder: the service cannot read ~/Downloads
    assert kept != str(att) and kept.startswith(str(tmp_path / "uploads")) and kept.endswith("/x.pdf")
    assert open(kept).read() == "pdf"
    assert cli.keep_file(kept) == kept  # a file already there is not copied again
    assert cli.main(base + ["--text-file", str(body), "--replaces", first]) == 0
    capsys.readouterr()
    assert db.get(db.connect(), first)["status"] == "replaced"
    assert cli.main(base + ["--link", "L=ftp://a.b"]) == 2
    assert cli.main(base + ["--file", "/no/such"]) == 2
    qf = tmp_path / "q.json"
    qf.write_text(json.dumps([{"key": "k", "text": "¿?", "options": []}]))
    assert cli.main(["ask", "questions", "--title", "T", "--context", "Necesito tus respuestas hoy",
                     "--question-file", str(qf), "--text-file", str(body), "--requester", "sess-1 pane"]) == 0


def test_upload_files_for_decision(conn, polar, bot, tmp_path):
    att = tmp_path / "x.pdf"
    att.write_text("pdf")
    db.create_request(conn, "decision", "T", "c", "s1",
                      options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                      extra={"files": [str(att)]})
    bot.files_upload_v2 = lambda **kw: bot._rec("files_upload_v2", **kw)
    polar.deliver_pending()
    assert bot.of("files_upload_v2")


def test_old_attachment_copies_are_removed_and_other_things_stay(tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    base = tmp_path / "uploads"
    now = 1_800_000_000
    old, new, other = base / ("%d-abc" % (now - 31 * 86400)), base / ("%d-def" % (now - 29 * 86400)), base / "notes"
    for d in (old, new, other):
        d.mkdir(parents=True)
        (d / "f.pdf").write_text("x")
    (base / ("%d-file" % (now - 40 * 86400))).write_text("a plain file, not a copy folder")
    assert cli.prune_uploads(str(base), now=now) == 1
    assert sorted(p.name for p in base.iterdir()) == sorted([new.name, "notes", "%d-file" % (now - 40 * 86400)])
    assert cli.prune_uploads(str(tmp_path / "missing")) == 0


def test_keep_file_prunes_before_it_copies(tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    old = tmp_path / "uploads" / "1000000000-old"
    old.mkdir(parents=True)
    att = tmp_path / "a.pdf"
    att.write_text("x")
    kept = cli.keep_file(str(att))
    assert not old.exists() and open(kept).read() == "x"
