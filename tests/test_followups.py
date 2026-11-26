"""Scheduled follow-ups: `polar report --revisar`, the «Revisar después» select, the web actions."""
import json
import os
import stat
import sys
import threading

import pytest

from polar import blocks, cli, db, followups, webapi
from tests.conftest import OWNER
from tests.test_webapi import act, call, shown, TOKEN

@pytest.fixture(autouse=True)
def _org(monkeypatch):
    monkeypatch.setenv("POLAR_GITHUB_ORG", "example-org")


SID = "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"
FAKE = r'''#!%s
import json, os, sys
d = os.environ["FAKE_BUS_DIR"]
argv = sys.argv[1:]
open(os.path.join(d, "calls.log"), "a").write(json.dumps(argv, ensure_ascii=False) + "\n")
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else ""
def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False)); sys.exit(code)
cmd = argv[1]
if cmd == "add":
    when = opt("--when")
    if "xyz" in when:
        out({"error": "No entiendo la fecha «xyz»."}, 2)
    n = 0
    if "--dry-run" not in argv:
        p = os.path.join(d, "count")
        n = int(open(p).read()) + 1 if os.path.exists(p) else 1
        if str(n) == os.environ.get("FAKE_BUS_FAIL_AT"):
            out({"error": "Esta sesión ya tiene 3 revisiones programadas (máximo 3). Cancela una en «Programados»."}, 2)
        open(p, "w").write(str(n))
    phrase = "cuando " + when[5:] + " salga a producción" if when.startswith("prod:") else "el " + when
    res = {"session": opt("--session"), "kind": "time", "key": "k", "label": when, "phrase": phrase,
           "by": opt("--by"), "text": opt("--text"), "card": opt("--card") or None, "title": opt("--title"),
           "duplicate": False}
    if n:
        res["id"] = n
    out(res)
if cmd == "cancel":
    state = os.environ.get("FAKE_BUS_CANCEL", "cancelled")
    out({"id": int(argv[2]), "cancelled": state == "cancelled", "state": state})
if cmd == "set-card":
    out({"id": int(argv[2]), "card": argv[3]})
out({"error": "?"}, 2)
'''


@pytest.fixture
def fakebus(tmp_path, monkeypatch):
    script = tmp_path / "agent-bus"
    script.write_text(FAKE % sys.executable)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "busstate"
    state.mkdir()
    monkeypatch.setenv("AGENT_BUS_BIN", str(script))
    monkeypatch.setenv("FAKE_BUS_DIR", str(state))
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))

    class Bus:
        def calls(self):
            p = state / "calls.log"
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return Bus()


def report(conn, links=None, requester=SID + " pane", followups_=None, **kw):
    return db.create_request(conn, "report", "Hecho X", "", requester, draft_text="listo", expires_hours=72,
                             report={"links": links or []}, followups=followups_, **kw)


def texts(bl):
    return [b["text"]["text"] for b in bl if b.get("type") == "section"]


def md(path, text="hola"):
    path.write_text(text)
    return str(path)


def run_cli(tmp_path, capsys, *extra, requester=SID + " pane"):
    f = md(tmp_path / "r.md")
    code = cli.main(["report", "--title", "T", "--text-file", f, "--requester", requester, *extra])
    return code, json.loads(capsys.readouterr().out)


# -- polar report --revisar
def test_revisar_happy_path(fakebus, tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys, "--revisar", "30-nov 10:00|Mide Polar", "--revisar", "prod:app-repo#870|Mira Pendientes")
    assert code == 0 and out["followups"] == [1, 2]
    req = db.get(db.connect(), out["id"])
    assert [(f["id"], f["phrase"], f["text"], f["by"], f["state"]) for f in req["followups"]] == [
        (1, "el 30-nov 10:00", "Mide Polar", "session", "open"),
        (2, "cuando app-repo#870 salga a producción", "Mira Pendientes", "session", "open")]
    calls = fakebus.calls()
    assert [c[1] + (":dry" if "--dry-run" in c else "") for c in calls] == [
        "add:dry", "add:dry", "add", "add", "set-card", "set-card"]
    real = calls[2]
    assert real[real.index("--by") + 1] == "session" and real[real.index("--session") + 1] == SID
    assert real[real.index("--title") + 1] == "T" and "--card" not in real
    assert calls[4] == ["followup", "set-card", "1", out["id"]]
    lines = [b["text"]["text"] for b in blocks.followup_blocks(req)]
    assert lines[0] == "🗓 Revisaré el 30-nov 10:00: Mide Polar"
    assert [b["accessory"]["text"]["text"] for b in blocks.followup_blocks(req)] == ["Cancelar", "Cancelar"]


def test_revisar_refused_before_the_card(fakebus, tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys, "--revisar", "30-nov 10:00|ok", "--revisar", "xyz|mal")
    assert code == 2 and out == {"error": "No entiendo la fecha «xyz»."}
    assert all("--dry-run" in c for c in fakebus.calls())  # nothing was added
    assert db.list_requests(db.connect()) == []


def test_revisar_needs_a_session(fakebus, tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys, "--revisar", "30-nov 10:00|ok", requester="watchdog")
    assert code == 2 and out == {"error": "Falta la sesión."}
    assert fakebus.calls() == []
    code, out = run_cli(tmp_path, capsys, "--revisar", "sin barra")
    assert code == 2 and "--revisar" in out["error"]
    code, out = run_cli(tmp_path, capsys, *["--revisar", "+2h|x"] * 4)
    assert code == 2 and "at most 3" in out["error"]


def test_revisar_cancels_when_an_add_fails(fakebus, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BUS_FAIL_AT", "2")
    code, out = run_cli(tmp_path, capsys, "--revisar", "+2h|a", "--revisar", "+3h|b")
    assert code == 2 and "máximo 3" in out["error"]
    assert ["followup", "cancel", "1", "--by", "session"] in fakebus.calls()
    assert db.list_requests(db.connect()) == []


def test_revisar_cancels_when_the_card_fails(fakebus, tmp_path, capsys, monkeypatch):
    def boom(*a, **k):
        raise ValueError("card refused")
    monkeypatch.setattr(db, "create_request", boom)
    code, out = run_cli(tmp_path, capsys, "--revisar", "+2h|a")
    assert code == 2 and out == {"error": "card refused"}
    assert ["followup", "cancel", "1", "--by", "session"] in fakebus.calls()


def test_report_without_revisar_does_not_call_the_bus(fakebus, tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys)
    assert code == 0 and "followups" not in out and fakebus.calls() == []


def test_migration_adds_the_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,"
                " created_at TEXT NOT NULL, title TEXT NOT NULL, expires_at TEXT NOT NULL)")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert "followups_json" in {r["name"] for r in conn.execute("PRAGMA table_info(requests)").fetchall()}


# -- the select: who sees it
def has_select(req):
    return any(b.get("elements") and b["elements"][0].get("action_id") == "polar_later"
               for b in blocks.request_blocks(req) + blocks.resolved_blocks(req))


def shown_blocks(req):
    live = req["status"] in db.OPEN_STATUSES or req["status"] == "noted"
    return blocks.request_blocks(req) if live else blocks.resolved_blocks(req)


def select_of(req):
    for b in shown_blocks(req):
        if b.get("block_id", "").startswith("polar_later_"):
            return b["elements"][0]
    return None


def test_select_visibility(conn):
    rid = report(conn)
    assert select_of(db.get(conn, rid))
    # a decision that waits: no; answered (chosen): yes
    did = db.create_request(conn, "decision", "¿Go?", "x", SID + " p", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    conn.execute("UPDATE requests SET status='shown' WHERE id=?", (did,))
    assert select_of(db.get(conn, did)) is None
    conn.execute("UPDATE requests SET status='chosen' WHERE id=?", (did,))
    assert select_of(db.get(conn, did))
    # questions: waiting no; answered yes
    qid = db.create_request(conn, "questions", "Q", "x", SID + " p", options=[{"key": "k", "text": "¿?"}])
    conn.execute("UPDATE requests SET status='shown' WHERE id=?", (qid,))
    assert select_of(db.get(conn, qid)) is None
    conn.execute("UPDATE requests SET status='answered' WHERE id=?", (qid,))
    assert select_of(db.get(conn, qid))
    # a sent reply and an email never
    rep = db.create_request(conn, "reply", "R", "x", SID + " p", draft_text="hi", target_channel="C1")
    conn.execute("UPDATE requests SET status='sent' WHERE id=?", (rep,))
    assert select_of(db.get(conn, rep)) is None
    # prod-op, no session id, a name that is no UUID
    pid = db.create_request(conn, "decision", "Go", "x", "prod-op " + SID, options=[{"key": "go", "label": "Go"}, {"key": "no", "label": "No"}])
    conn.execute("UPDATE requests SET status='chosen' WHERE id=?", (pid,))
    assert select_of(db.get(conn, pid)) is None
    assert select_of(db.get(conn, report(conn, requester=""))) is None
    assert select_of(db.get(conn, report(conn, requester="watchdog"))) is None
    assert select_of(db.get(conn, report(conn, requester="not-a-uuid pane"))) is None


def test_prod_option_needs_a_pr_of_the_org(conn):
    def labels(links):
        return [o["text"]["text"] for o in select_of(db.get(conn, report(conn, links=links)))["options"]]
    base = ["Mañana 9:00", "En 3 días", "El lunes 9:00"]
    assert labels([]) == base
    assert labels(["PR=https://github.com/other-org/x/pull/3"]) == base
    assert labels(["Doc=https://github.com/example-org/x/issues/3"]) == base
    assert labels(["PR=https://github.com/Example-ORG/app-repo/pull/870"]) == base + ["Cuando salga a producción"]
    req = db.get(conn, report(conn, links=["A=https://github.com/o/x/pull/1", "B=https://github.com/example-org/r1/pull/5",
                                           "C=https://github.com/example-org/r2/pull/6"]))
    assert followups.later_spec(req, "prod") == "prod:r1#5"
    with pytest.raises(followups.FollowupError):
        followups.later_spec(db.get(conn, report(conn)), "prod")
    # a decision takes the PR from its extra links
    did = db.create_request(conn, "decision", "¿Go?", "x", SID + " p", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
                            extra={"links": ["PR=https://github.com/example-org/r3/pull/9"]})
    conn.execute("UPDATE requests SET status='chosen' WHERE id=?", (did,))
    assert followups.pr_of(db.get(conn, did)) == "r3#9"


def test_any_org_counts_when_no_org_is_set(conn, monkeypatch):
    monkeypatch.delenv("POLAR_GITHUB_ORG")
    req = db.get(conn, report(conn, links=["PR=https://github.com/other-org/x/pull/3"]))
    assert followups.pr_of(req) == "x#3"


# -- the Slack handlers
def test_slack_pick_and_cancel(polar, conn, fakebus):
    rid = report(conn, links=["PR=https://github.com/example-org/r1/pull/5"])
    shown(polar, rid)
    polar.on_later(OWNER, "%s:prod" % rid)
    call_ = [c for c in fakebus.calls() if c[1] == "add"][0]
    assert call_[call_.index("--by") + 1] == "owner" and call_[call_.index("--card") + 1] == rid
    assert call_[call_.index("--when") + 1] == "prod:r1#5" and call_[call_.index("--session") + 1] == SID
    req = db.get(conn, rid)
    assert req["followups"][0]["by"] == "owner"
    last = polar.bot.of("chat_update")[-1]["blocks"]
    assert any(t.startswith("🗓 Revisión programada cuando r1#5") for t in texts(last))
    polar.on_fu_cancel(OWNER, "%s:1" % rid)
    assert db.get(conn, rid)["followups"][0]["state"] == "cancelled"
    sec = [b for b in polar.bot.of("chat_update")[-1]["blocks"] if b["type"] == "section"][-1]
    assert "~" in sec["text"]["text"] and sec["text"]["text"].endswith("cancelada") and "accessory" not in sec


def test_slack_bus_error_goes_to_the_owner(polar, conn, fakebus, monkeypatch):
    monkeypatch.setenv("FAKE_BUS_FAIL_AT", "1")
    rid = report(conn)
    shown(polar, rid)
    polar.on_later(OWNER, "%s:manana" % rid)
    msg = polar.bot.of("chat_postEphemeral")[-1]
    assert "máximo 3" in msg["text"] and msg["user"] == OWNER
    assert db.get(conn, rid)["followups"] == []
    polar.on_later("someone-else", "%s:manana" % rid)  # not the owner: nothing
    assert len([c for c in fakebus.calls() if c[1] == "add"]) == 1


def test_cancel_too_late_turns_to_delivered(polar, conn, fakebus, monkeypatch):
    monkeypatch.setenv("FAKE_BUS_CANCEL", "delivered")
    rid = report(conn, followups_=[{"id": 7, "phrase": "el 30-nov 10:00", "text": "Mide", "by": "session", "state": "open"}])
    shown(polar, rid)
    polar.on_fu_cancel(OWNER, "%s:7" % rid)
    assert db.get(conn, rid)["followups"][0]["state"] == "delivered"
    blocks_ = polar.bot.of("chat_update")[-1]["blocks"]
    assert any(t.startswith("✅ entregada") for t in texts(blocks_))


def test_decided_card_keeps_lines_and_select(polar, conn, fakebus):
    did = db.create_request(conn, "decision", "¿Go?", "x", SID + " p", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    shown(polar, did)
    polar.on_choose(OWNER, did, "a")
    polar.on_later(OWNER, "%s:3d" % did)
    last = polar.bot.of("chat_update")[-1]["blocks"]
    assert last[0]["text"]["text"].startswith("👉 Elegiste")
    assert last[1]["text"]["text"] == "🗓 Revisión programada el +3d 9:00"
    assert last[-1]["elements"][0]["action_id"] == "polar_later"


# -- the web API
def test_web_later_and_cancel_without_409(web, polar, conn, fakebus):
    rid = report(conn, links=["PR=https://github.com/example-org/r1/pull/5"])
    shown(polar, rid)
    code, out = act(web, rid, "later", when="lunes")
    assert code == 200 and out["ok"] and out["followup"]["id"] == 1 and out["followup"]["phrase"] == "el lunes 9:00"
    assert db.get(conn, rid)["status"] == "noted"  # the status did not change, and the call still worked
    code, out = act(web, rid, "later", when="prod")
    assert code == 200 and out["followup"]["id"] == 2
    code, out = act(web, rid, "fu_cancel", fu=1)
    assert code == 200 and out == {"ok": True, "followup": {"id": 1, "cancelled": True, "state": "cancelled"}}
    assert [f["state"] for f in db.get(conn, rid)["followups"]] == ["cancelled", "open"]


def test_web_error_codes(web, polar, conn, fakebus, monkeypatch):
    rid = report(conn)
    shown(polar, rid)
    assert act(web, rid, "later", when="nunca")[0] == 400
    assert act(web, rid, "later")[0] == 400
    assert act(web, rid, "later", when="prod")[0] == 409  # no PR on the card
    assert act(web, rid, "fu_cancel", fu="x")[0] == 400
    assert act(web, rid, "fu_cancel", fu=99)[0] == 409
    monkeypatch.setenv("FAKE_BUS_FAIL_AT", "1")
    code, out = act(web, rid, "later", when="manana")
    assert code == 422 and "máximo 3" in out["error"] and out["ok"] is False
    # cards that do not allow it
    did = db.create_request(conn, "decision", "¿Go?", "x", SID + " p", options=[{"key": "a", "label": "A"}, {"key": "b", "label": "B"}])
    shown(polar, did)
    code, out = act(web, did, "later", when="manana")
    assert code == 409 and out["status"] == "shown"
    nos = report(conn, requester="watchdog")
    shown(polar, nos)
    assert act(web, nos, "later", when="manana")[0] == 409


@pytest.fixture
def web(polar, monkeypatch):
    server = webapi.WebServer(0, polar, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_revisar_rollback_keeps_an_older_duplicate(fakebus, tmp_path, capsys, monkeypatch):
    """A duplicate is an older follow-up that another card may show: a failed add must not cancel it."""
    seq = iter([{"id": 7, "phrase": "el 30-nov 10:00", "text": "a", "by": "session", "duplicate": True},
                {"error": "Esta sesión ya tiene 3 revisiones programadas (máximo 3)."}])

    def fake_add(spec, session, by, title, text="", card="", dry_run=False):
        return {"phrase": "x", "duplicate": False} if dry_run else next(seq)
    monkeypatch.setattr(followups, "add", fake_add)
    code, out = run_cli(tmp_path, capsys, "--revisar", "30-nov 10:00|a", "--revisar", "+3h|b")
    assert code == 2 and "máximo 3" in out["error"]
    assert not [c for c in fakebus.calls() if c[1] == "cancel"]


def test_later_on_old_reports_but_not_replaced(fakebus):
    conn = db.connect()
    rid = report(conn)
    for status, ok in (("pending", True), ("noted", True), ("filed", True), ("chosen", True), ("replaced", False)):
        conn.execute("UPDATE requests SET status=? WHERE id=?", (status, rid))
        assert followups.can_later(db.get(conn, rid)) is ok, status
