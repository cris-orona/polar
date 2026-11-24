import importlib.machinery
import importlib.util
import json
import os

import pytest

from polar import blocks, cli, db
from polar.tasks import RESUME_PROMPT, Tasks
from tests.conftest import OWNER

SID = "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"


def make(conn, **rep):
    return db.create_request(conn, "report", "Hecho X", "", "s1", draft_text="**Hecho:** listo",
                             expires_hours=72, report=rep)


def actions(req):
    return [b for b in blocks.request_blocks(req) if b.get("block_id") == "polar_actions"][0]["elements"]


def test_create_and_validation(conn):
    rid = make(conn, links=["PR=https://github.com/o/r/pull/1"], files=["/a"], resume=SID)
    req = db.get(conn, rid)
    assert req["kind"] == "report" and req["options"] == []
    assert req["report"]["links"] == [{"label": "PR", "url": "https://github.com/o/r/pull/1"}]
    assert req["report"]["resume"] == SID
    for bad in (["x=javascript:alert(1)"], ["sin-igual"], ["=https://a.b"], ["a=https://x"] * 6):
        with pytest.raises(ValueError):
            make(conn, links=bad)
    with pytest.raises(ValueError):
        make(conn, resume="no es id")
    with pytest.raises(ValueError):
        db.create_request(conn, "report", "t", draft_text=" ", report={})


def test_cli_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    f = tmp_path / "r.md"
    f.write_text("hola")
    att = tmp_path / "x.pdf"
    att.write_text("pdf")
    assert cli.main(["report", "--title", "T", "--text-file", str(f), "--link", "L=https://a.b",
                     "--file", str(att), "--resume", SID]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    req = db.get(db.connect(), rid)
    kept = req["report"]["files"]
    assert len(kept) == 1 and kept[0].startswith(str(tmp_path / "uploads")) and open(kept[0]).read() == "pdf"
    assert req["expires_at"] > req["created_at"]
    assert cli.main(["report", "--title", "T", "--text-file", str(f), "--file", "/no/such"]) == 2
    assert cli.main(["report", "--title", "T", "--text-file", str(f), "--link", "mal"]) == 2


def test_render_without_resume(conn):
    req = db.get(conn, make(conn, links=["Polar=https://slack.com"]))
    bl = blocks.request_blocks(req)
    assert bl[0]["text"]["text"] == "📋 Hecho X"
    assert {"type": "markdown", "text": "**Hecho:** listo"} in bl
    assert any("Sesión" in e["text"] for b in bl if b["type"] == "context" for e in b["elements"])
    els = actions(req)
    ids = [e["action_id"] for e in els]
    assert ids == ["polar_link:0", "polar_revise"]
    assert els[0]["url"] == "https://slack.com"
    assert "polar_choose:resume" not in ids


def test_render_with_resume(conn):
    els = actions(db.get(conn, make(conn, resume=SID)))
    r = [e for e in els if e["action_id"] == "polar_choose:resume"][0]
    assert r["style"] == "primary" and "Reanudar" in r["text"]["text"]


def test_ok_action(conn, polar, bot):
    rid = make(conn)
    polar.deliver_pending()
    polar.on_choose(OWNER, rid, "ok")
    req = db.get(conn, rid)
    assert req["status"] == "chosen" and req["result"]["option"] == "ok"
    assert blocks.result_line(req) == "📋 Visto — Hecho X"
    assert "actions" not in [b["type"] for b in bot.of("chat_update")[-1]["blocks"]]


def test_resume_action_calls_launcher(conn, polar, monkeypatch):
    calls = []
    monkeypatch.setattr(Tasks, "resume_session", lambda self, sid: calls.append(sid) or True)
    rid = make(conn, resume=SID)
    polar.deliver_pending()
    polar.on_choose(OWNER, rid, "resume")
    polar.on_choose(OWNER, rid, "resume")  # second click: nothing
    assert calls == [SID] and db.get(conn, rid)["result"]["option"] == "resume"
    rid2 = make(conn)  # no --resume: the key is refused
    polar.deliver_pending()
    polar.on_choose(OWNER, rid2, "resume")
    assert db.get(conn, rid2)["status"] == "noted" and calls == [SID]


def test_revise_allowed(conn, polar, bot):
    rid = make(conn)
    polar.deliver_pending()
    polar.on_revise_click(OWNER, rid, "trig")
    assert bot.of("views_open")


def test_noted_after_delivery(conn, polar, bot):
    rid = make(conn, resume=SID)
    polar.deliver_pending()
    assert db.get(conn, rid)["status"] == "noted"
    assert rid not in [r["id"] for r in db.list_requests(conn, "open")]
    assert rid in [r["id"] for r in db.list_requests(conn, "noted")]
    sent = bot.of("chat_postMessage")[-1]["blocks"]
    assert "Visto" not in json.dumps(sent, ensure_ascii=False)
    ids = [e["action_id"] for b in sent if b["type"] == "actions" for e in b["elements"]]
    assert ids == ["polar_choose:resume", "polar_revise"]


def test_refresh_noted_keeps_buttons(conn, polar, bot):
    rid = make(conn)
    polar.deliver_pending()
    polar.refresh_dm(rid)
    assert any(b["type"] == "actions" for b in bot.of("chat_update")[-1]["blocks"])
    assert db.get(conn, rid)["dm_done"] == 0


def test_revise_from_noted(conn, polar, bot):
    rid = make(conn)
    polar.deliver_pending()
    polar.on_revise_click(OWNER, rid, "trig")
    assert bot.of("views_open")
    view = {"private_metadata": rid, "state": {"values": {"text_block": {"text": {"value": "cambia X"}}}}}
    assert polar.on_revise_submit(OWNER, view) is None
    req = db.get(conn, rid)
    assert req["status"] == "revise" and req["result"] == {"instructions": "cambia X"}


def test_fold_after_24h(conn, polar, bot):
    rid = make(conn)
    polar.deliver_pending()
    assert db.fold_reports(conn) == []
    later = "2999-01-01T00:00:00Z"
    assert db.fold_reports(conn, later) == [rid]
    req = db.get(conn, rid)
    assert req["status"] == "filed" and req["decided_at"] == later and req["dm_done"] == 0
    assert blocks.result_line(req) == "📋 Informe — Hecho X"
    n = len(bot.of("chat_update"))
    polar.sweep()
    polar.sweep()
    assert len(bot.of("chat_update")) == n + 1
    last = bot.of("chat_update")[-1]["blocks"]
    assert len(last) == 1 and last[0]["text"]["text"] == "📋 Informe — Hecho X"
    assert db.get(conn, rid)["dm_done"] == 1


def test_fold_env_override(conn, polar, monkeypatch):
    rid = make(conn)
    polar.deliver_pending()
    conn.execute("UPDATE requests SET created_at=? WHERE id=?", ("2026-11-20T10:00:00Z", rid))
    assert db.fold_reports(conn, "2026-11-20T12:00:00Z") == []
    monkeypatch.setenv("POLAR_REPORT_FOLD_HOURS", "1")
    assert db.fold_reports(conn, "2026-11-20T12:00:00Z") == [rid]


def test_sweep_folds(conn, polar):
    rid = make(conn)
    polar.deliver_pending()
    conn.execute("UPDATE requests SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (rid,))
    polar.sweep()
    assert db.get(conn, rid)["status"] == "filed"


def test_recover_migrates_shown_report(conn, polar, bot):
    rid = make(conn)
    polar.deliver_pending()
    conn.execute("UPDATE requests SET status='shown' WHERE id=?", (rid,))
    polar.recover()
    assert db.get(conn, rid)["status"] == "noted"
    last = bot.of("chat_update")[-1]["blocks"]
    assert "Visto" not in json.dumps(last, ensure_ascii=False)


def test_withdraw_and_replace_noted(conn, polar):
    a, b = make(conn), make(conn)
    polar.deliver_pending()
    assert db.withdraw(conn, a) and db.get(conn, a)["status"] == "discarded"
    assert db.mark_replaced(conn, b) and db.get(conn, b)["status"] == "replaced"


class Sh:
    def __init__(self, live=()):
        self.calls, self.live = [], list(live)

    def __call__(self, argv, timeout=15):
        self.calls.append(argv)
        if argv[1] == "list-sessions":
            return (0, "\n".join(self.live)) if self.live else (1, "")
        return 0, ""


def test_resume_session_launch(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("POLAR_CLAUDE_BIN", "/bin/claude")
    monkeypatch.setenv("POLAR_CLAUDE_JSON", str(tmp_path / "c.json"))
    sh = Sh()
    assert Tasks(conn, None, OWNER, sh=sh).resume_session(SID)
    new = [c for c in sh.calls if c[1] == "new-session"][0]
    assert new[new.index("-s") + 1] == "task-0b1c2d3e"
    assert new[new.index("-c") + 1] == str(tmp_path / "work")
    assert "--resume " + SID in new[-1] and "--permission-mode auto" in new[-1]
    assert "Retoma donde quedaste" in new[-1] and RESUME_PROMPT.split(".")[0] in new[-1]
    sh2 = Sh(live=["task-0b1c2d3e"])
    assert not Tasks(conn, None, OWNER, sh=sh2).resume_session(SID)
    assert not [c for c in sh2.calls if c[1] == "new-session"]


class ScopeErr(Exception):
    response = {"error": "missing_scope"}


def test_files_uploaded(conn, polar, bot, tmp_path):
    f = tmp_path / "a.pdf"
    f.write_text("x")
    bot.files_upload_v2 = lambda **kw: bot._rec("files_upload_v2", **kw)
    make(conn, files=[str(f)])
    polar.deliver_pending()
    up = bot.of("files_upload_v2")[0]
    assert up["channel"] == "D1" and up["thread_ts"] == "100.1" and up["file"] == str(f)


def test_missing_scope_notice(conn, polar, bot, tmp_path):
    def boom(**kw):
        raise ScopeErr("xoxb-secret")
    bot.files_upload_v2 = boom
    rid = make(conn, files=["/tmp/informe.pdf"])
    assert polar.deliver_pending() == 1
    assert db.get(conn, rid)["status"] == "noted"
    msgs = bot.of("chat_postMessage")
    assert msgs[-1]["thread_ts"] == "100.1"
    assert msgs[-1]["text"] == "No pude adjuntar informe.pdf: falta el permiso files:write en la app Polar."


# -- suggested follow-ups (--next) ------------------------------------------------------
def test_cli_next(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path))
    f = tmp_path / "r.md"
    f.write_text("hola")
    base = ["report", "--title", "T", "--text-file", str(f)]
    assert cli.main(base + ["--next", "  Sigue con la ñ  ", "--next", "Abre el PR"]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    assert db.get(db.connect(), rid)["report"]["next"] == ["Sigue con la ñ", "Abre el PR"]
    assert cli.main(base + ["--next", " "]) == 2
    assert cli.main(base + ["--next", "x" * 71]) == 2
    assert cli.main(base + ["--next", "x" * 70]) == 0
    assert cli.main(base + ["--next", "a", "--next", "b", "--next", "c", "--next", "d"]) == 2


def test_next_buttons_render(conn):
    req = db.get(conn, make(conn, resume=SID, links=["PR=https://a.b"], next=["Sigue con X", "y" * 70]))
    els = actions(req)
    ids = [e["action_id"] for e in els]
    assert ids == ["polar_choose:resume", "polar_link:0", "polar_next_go", "polar_revise"]
    assert els[2]["text"]["text"] == "▶️ Empezar marcados"
    sel = [b for b in blocks.request_blocks(req) if b.get("block_id") == "polar_nextsel:" + req["id"]][0]
    opts = sel["elements"][0]["options"]
    assert [o["value"] for o in opts] == ["0", "1"] and len(opts[1]["text"]["text"]) <= 75


def test_one_followup_keeps_button_three_get_checkboxes(conn):
    one = db.get(conn, make(conn, next=["Solo uno"]))
    els = actions(one)
    assert [e["action_id"] for e in els] == ["polar_next:0", "polar_revise"]
    assert not any(b.get("block_id", "").startswith("polar_nextsel") for b in blocks.request_blocks(one))
    three = db.get(conn, make(conn, next=["a", "b", "c" * 70]))
    bl = blocks.request_blocks(three)
    sel = [b for b in bl if b.get("block_id") == "polar_nextsel:" + three["id"]]
    assert len(sel) == 1 and sel[0]["elements"][0]["type"] == "checkboxes"
    assert len(sel[0]["elements"][0]["options"]) == 3
    ids = [e["action_id"] for e in actions(three)]
    assert "polar_next_go" in ids and not any(i.startswith("polar_next:") for i in ids)


def noted(polar, conn, **kw):
    rid = make(conn, next=["Sigue con X", "Otra"], **kw)
    polar.deliver_pending()
    return rid


def test_on_next_from_noted_and_shown(polar, conn, bot):
    rid = noted(polar, conn)
    assert db.get(conn, rid)["status"] == "noted"
    polar.on_next(OWNER, rid, 1)
    r = db.get(conn, rid)
    assert r["status"] == "revise" and r["result"] == {"instructions": "Otra", "via": "next", "indexes": [1], "steps": ["Otra"]} \
        and r["decided_at"]
    assert bot.of("chat_update")[-1]["blocks"][0]["text"]["text"] == "➡️ Seguimiento: Otra"
    pub = cli._public(r)
    assert pub["instructions"] == "Otra" and pub["via"] == "next"
    rid2 = noted(polar, conn)
    assert db.transition(conn, rid2, "noted", "shown")
    polar.on_next(OWNER, rid2, 0)
    assert db.get(conn, rid2)["result"]["instructions"] == "Sigue con X"


def test_on_next_second_tap_and_guards(polar, conn, bot, monkeypatch):
    rid = noted(polar, conn)
    calls = []
    monkeypatch.setattr(polar, "_orphan", lambda *a: calls.append(a))
    polar.on_next("U9", rid, 0)
    polar.on_next(OWNER, rid, 5)
    polar.on_next(OWNER, rid, -1)
    assert db.get(conn, rid)["status"] == "noted" and not calls
    polar.on_next(OWNER, rid, 0)
    polar.on_next(OWNER, rid, 1)  # second tap: no-op
    assert db.get(conn, rid)["result"]["instructions"] == "Sigue con X"
    assert len(calls) == 1 and calls[0] == (rid, "Sigue con X")


def test_on_next_orphan_starts_task(polar, conn, monkeypatch):
    rid = noted(polar, conn)
    seen = []
    monkeypatch.setattr(polar.tasks, "orphan_card", lambda req, text: seen.append(text) or False)
    polar.on_next(OWNER, rid, 0)
    assert seen == ["Sigue con X"]


def three(polar, conn):
    rid = make(conn, next=["Uno", "Dos", "Tres"])
    polar.deliver_pending()
    return rid


def test_on_next_indexes(polar, conn, bot):
    rid = three(polar, conn)
    polar.on_next(OWNER, rid, indexes=[2, 0])
    r = db.get(conn, rid)
    assert r["status"] == "revise"
    assert r["result"] == {"instructions": "1) Uno 2) Tres", "via": "next", "indexes": [0, 2],
                           "steps": ["Uno", "Tres"]}
    assert bot.of("chat_update")[-1]["blocks"][0]["text"]["text"] == "➡️ Seguimientos: Uno · Tres"
    pub = cli._public(r)
    assert pub["steps"] == ["Uno", "Tres"] and pub["indexes"] == [0, 2]


@pytest.mark.parametrize("bad", [[], [0, 0], [3], [-1], ["0"], [True], [0.0], [0, 1, 2, 0], "0", 0])
def test_on_next_bad_indexes(polar, conn, bad):
    rid = three(polar, conn)
    polar.on_next(OWNER, rid, indexes=bad)
    assert db.get(conn, rid)["status"] == "noted"


def test_on_next_bool_index(polar, conn):
    rid = three(polar, conn)
    polar.on_next(OWNER, rid, True)
    assert db.get(conn, rid)["status"] == "noted"


def state_for(rid, marked):
    return {"values": {"polar_nextsel:" + rid: {"polar_next_check": {
        "type": "checkboxes", "selected_options": [{"value": str(i)} for i in marked]}}}}


def test_next_go_handler(polar, conn, bot):
    rid = three(polar, conn)
    polar.on_next_go(OWNER, rid, state_for(rid, []))
    assert db.get(conn, rid)["status"] == "noted"
    assert [m["text"] for m in bot.of("chat_postEphemeral")] == ["Marca al menos un seguimiento."]
    polar.on_next_go(OWNER, rid, state_for(rid, [1, 2]))
    r = db.get(conn, rid)
    assert r["status"] == "revise" and r["result"]["instructions"] == "1) Dos 2) Tres"


def test_next_go_stale_and_toggle(polar, conn, bot):
    rid = three(polar, conn)
    polar.on_next(OWNER, rid, 0)
    polar.on_next_go(OWNER, rid, state_for(rid, [1]))
    assert db.get(conn, rid)["result"]["steps"] == ["Uno"]
    assert [m["text"] for m in bot.of("chat_postEphemeral")] == ["Esta tarjeta ya estaba pedida de cambios. No hice nada."] \
        or "ya estaba" in bot.of("chat_postEphemeral")[-1]["text"]
    # the toggle handler only acks
    import re
    from polar import app as app_mod
    src = open(app_mod.__file__).read()
    assert re.search(r'def _next_check\(ack\):\n\s+ack\(\)', src)
