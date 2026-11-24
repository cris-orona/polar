"""The owner's text on a card that names no session starts a task: it is never lost in the router inbox."""
import pytest

from polar import db
from polar.tasks import has_session
from tests.conftest import OWNER

SID = "0b1c2d3e-aaaa-4bbb-8ccc-1234567890ab"
OPTS = [{"key": "ok", "label": "Lo reviso"}, {"key": "ignore", "label": "Ignorar"}]


def card(conn, requester, kind="decision", **kw):
    if kind == "report":
        return db.create_request(conn, "report", "Falla en tu Mac: disco casi lleno", "", requester,
                                 draft_text="Quedan 4 GB libres", expires_hours=72, report={})
    return db.create_request(conn, "decision", "Watchdog: el router parece detenido", "Sin corridas en 40 min",
                             requester, options=OPTS, **kw)


def agent_card(conn, where="server", requester=""):
    return db.create_request(
        conn, "decision", "Flotillera sin usuario del portal", "nadie tiene este mensaje", requester,
        draft_text="Revisa por qué la flota no tiene usuario. https://x.slack.com/p1 de Ben", target_channel="C1",
        options=[{"key": "agent", "label": "Abrir agente"}, {"key": "no", "label": "No"}],
        reply_to_ts="55.5", agent_where=where)


def shown(polar, rid):
    polar.deliver_pending()
    return db.get(polar.conn, rid)


def revise(polar, rid, text):
    view = {"private_metadata": rid, "state": {"values": {"text_block": {"text": {"value": text}}}}}
    return polar.on_revise_submit(OWNER, view)


def reply(req, text, ts):
    return {"type": "message", "channel_type": "im", "channel": req["dm_channel"], "user": OWNER, "text": text,
            "ts": ts, "thread_ts": req["dm_ts"]}


def said(bot, req):
    return [c["text"] for c in bot.of("chat_postMessage") if c.get("thread_ts") == req["dm_ts"]]


@pytest.mark.parametrize("who,expected", [
    (SID + " w4:p33Z", True),
    (SID, True),
    ("prod-op 7cb4d06dcebd " + SID + " task-1", True),
    ("release-desk b-20261120-37", True),
    ("prod-op 7cb4d06dcebd release-desk owner-steps", True),
    ("", False),
    (None, False),
    ("watchdog", False),
    ("disk-cleanup", False),
    ("unknown-session w4:p1", False),
    ("prod-op f79cbb8f5c5a", False),
    ("09299335 task-f35e6e0a", False),
])
def test_has_session(who, expected):
    assert has_session(who) is expected


def test_revise_on_card_with_no_session_starts_one_task(polar, conn, bot):
    rid = card(conn, "watchdog")
    req = shown(polar, rid)
    assert revise(polar, rid, "puedes levantarlo?") is None
    assert db.get(conn, rid)["status"] == "revise"
    tasks = polar.tasks.rows("task")
    assert len(tasks) == 1
    t = tasks[0]
    assert t["status"] == "triage" and t["thread_ts"] == req["dm_ts"] and t["channel"] == req["dm_channel"]
    assert "puedes levantarlo?" in t["text"] and "Watchdog: el router parece detenido" in t["text"]
    assert rid in t["text"] and "«watchdog»" in t["text"] and "Sin corridas en 40 min" in t["text"]
    assert t["title"].startswith("Sobre la tarjeta «Watchdog")
    assert len(said(bot, req)) == 1 and "no tenía sesión" in said(bot, req)[0]
    # A late second submit of the same window changes nothing.
    revise(polar, rid, "puedes levantarlo?")
    assert len(polar.tasks.rows("task")) == 1 and len(said(bot, req)) == 1


def test_revise_on_report_with_no_session(polar, conn, bot):
    rid = card(conn, "unknown-session", kind="report")
    req = shown(polar, rid)
    assert req["status"] == "noted"
    revise(polar, rid, "límpialo con nuestro script")
    t = polar.tasks.rows("task")
    assert len(t) == 1 and "límpialo con nuestro script" in t[0]["text"] and "Quedan 4 GB libres" in t[0]["text"]


@pytest.mark.parametrize("who", [SID + " w4:p1", "release-desk b-20261120-37", "prod-op abc123 " + SID + " task-9"])
def test_revise_on_card_with_a_reader_starts_no_task(polar, conn, bot, who):
    rid = card(conn, who)
    req = shown(polar, rid)
    revise(polar, rid, "cambia X")
    assert db.get(conn, rid)["status"] == "revise"
    assert polar.tasks.rows("task") == [] and said(bot, req) == []


def test_revise_on_agent_card_starts_the_agent_with_the_text(polar, conn, bot):
    rid = agent_card(conn, "laptop")
    req = shown(polar, rid)
    revise(polar, rid, "abre el agente y pregunta a Gina")
    t = polar.tasks.rows("task")
    assert len(t) == 1
    assert t[0]["status"] == "queued" and t[0]["where_hint"] == "laptop"  # no triage: the router found no owner
    assert t[0]["text"].startswith("Revisa por qué la flota no tiene usuario.")
    assert "Instrucción del dueño al abrir el agente: abre el agente y pregunta a Gina" in t[0]["text"]
    assert said(bot, req) == ["🤖 Esta tarjeta no tenía sesión: abro el agente con tu instrucción."]
    polar.on_choose(OWNER, rid, "agent")  # the card is decided: a click starts no second agent
    assert len(polar.tasks.rows("task")) == 1


def test_revise_on_agent_card_made_by_a_router_run_starts_the_agent(polar, conn, bot):
    """The router run is a Claude session, so its id is on the card; that session ended and cannot be woken."""
    rid = agent_card(conn, "server", requester=SID)
    req = shown(polar, rid)
    revise(polar, rid, "ábrelo, pero redirige a Marta a Robot")
    t = polar.tasks.rows("task")
    assert len(t) == 1 and t[0]["status"] == "queued" and t[0]["where_hint"] == "server"
    assert "Instrucción del dueño al abrir el agente: ábrelo, pero redirige a Marta a Robot" in t[0]["text"]
    assert said(bot, req) == ["🤖 Esta tarjeta no tenía sesión: abro el agente con tu instrucción."]


def test_thread_reply_on_agent_card_made_by_a_router_run_starts_the_agent(polar, conn, bot):
    rid = agent_card(conn, "server", requester=SID + " task-router")
    req = shown(polar, rid)
    polar.tasks.on_message(reply(req, "ábrelo y pregunta a Gina", "9100.1"))
    t = polar.tasks.rows("task")
    assert len(t) == 1 and "Instrucción del dueño al abrir el agente: ábrelo y pregunta a Gina" in t[0]["text"]


def test_task_error_does_not_undo_the_revise(polar, conn, bot, monkeypatch):
    rid = card(conn, "")
    shown(polar, rid)

    def boom(*a, **k):
        raise RuntimeError("db is away")
    monkeypatch.setattr(polar.tasks, "orphan_card", boom)
    assert revise(polar, rid, "hazlo") is None
    assert db.get(conn, rid)["status"] == "revise"


def test_thread_reply_on_card_with_no_session_starts_one_task(polar, conn, bot):
    rid = card(conn, "")
    req = shown(polar, rid)
    ev = reply(req, "oye, ¿y esto qué?", "9000.1")
    polar.tasks.on_message(ev)
    polar.tasks.on_message(ev)  # Slack sent the event twice
    tasks = polar.tasks.rows("task")
    assert len(tasks) == 1 and "oye, ¿y esto qué?" in tasks[0]["text"] and tasks[0]["thread_ts"] == req["dm_ts"]
    assert len(said(bot, req)) == 1
    assert db.get(conn, rid)["status"] == "shown"  # the card stays open
    # The next message of the thread goes to that task. It starts no second task.
    polar.tasks.on_message(reply(req, "y otra cosa", "9000.2"))
    assert len(polar.tasks.rows("task")) == 1
    steers = polar.tasks.rows("steer")
    assert len(steers) == 1 and steers[0]["target_task"] == tasks[0]["id"] and steers[0]["text"] == "y otra cosa"


def test_thread_reply_after_revise_goes_to_the_task(polar, conn, bot):
    rid = card(conn, "disk-cleanup")
    req = shown(polar, rid)
    revise(polar, rid, "límpialo")
    polar.tasks.on_message(reply(req, "con el script nuevo", "9000.3"))
    tasks = polar.tasks.rows("task")
    assert len(tasks) == 1
    steers = polar.tasks.rows("steer")
    assert len(steers) == 1 and steers[0]["target_task"] == tasks[0]["id"]


def test_thread_reply_on_agent_card_starts_the_agent(polar, conn, bot):
    rid = agent_card(conn)
    req = shown(polar, rid)
    polar.tasks.on_message(reply(req, "sí, y pregúntale a Gina", "9000.4"))
    t = polar.tasks.rows("task")
    assert len(t) == 1 and t[0]["status"] == "queued"
    assert "Instrucción del dueño al abrir el agente: sí, y pregúntale a Gina" in t[0]["text"]


def test_no_triage_mode_queues_the_task(polar, conn, bot, monkeypatch):
    monkeypatch.setenv("POLAR_TRIAGE", "0")
    rid = card(conn, "")
    shown(polar, rid)
    revise(polar, rid, "hazlo")
    assert polar.tasks.rows("task")[0]["status"] == "queued"
