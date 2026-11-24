"""Socket Mode app: `polar serve` and `polar doctor`."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from typing import Any, Dict, Optional

from polar import alarm, db
from polar.core import Polar, error_code
from polar.tokens import TOKENS, get_token

log = logging.getLogger("polar")
TICK_SECONDS = 5


def owner_id() -> Optional[str]:
    return (os.environ.get("POLAR_OWNER_ID") or "U0000000000").strip()


def register(app: Any, polar: Polar) -> None:
    """Wire the Bolt handlers to the logic. Each handler acks first."""

    def uid(body: Dict[str, Any]) -> Optional[str]:
        return (body.get("user") or {}).get("id")

    def rid(body: Dict[str, Any]) -> str:
        return body["actions"][0]["value"]

    @app.action("polar_send")
    def _send(ack, body):
        ack()
        polar.on_send(uid(body), rid(body))

    @app.action("polar_discard")
    def _discard(ack, body):
        ack()
        polar.on_discard(uid(body), rid(body))

    @app.action("polar_revise")
    def _revise(ack, body):
        ack()
        polar.on_revise_click(uid(body), rid(body), body["trigger_id"])

    @app.action("polar_reopen")
    def _reopen(ack, body):
        ack()
        polar.on_reopen(uid(body), rid(body))

    @app.action("polar_edit")
    def _edit(ack, body):
        ack()
        polar.on_edit_click(uid(body), rid(body), body["trigger_id"])

    @app.action(re.compile(r"^polar_choose:"))
    def _choose(ack, body):
        ack()
        key = body["actions"][0]["action_id"].split(":", 1)[1]
        polar.on_choose(uid(body), rid(body), key)

    @app.action(re.compile(r"^polar_link:"))
    def _link(ack):
        ack()  # a URL button opens the page; Slack still sends the click

    @app.action(re.compile(r"^polar_next:"))
    def _next(ack, body):
        ack()
        polar.on_next(uid(body), rid(body), int(body["actions"][0]["action_id"].split(":", 1)[1]))

    @app.action("polar_next_check")
    def _next_check(ack):
        ack()  # a toggle needs no redraw; the state is read when the start button is pressed

    @app.action("polar_next_go")
    def _next_go(ack, body):
        ack()
        polar.on_next_go(uid(body), rid(body), body.get("state") or {})

    @app.action(re.compile(r"^polar_qa:"))
    def _qa(ack, body):
        ack()
        _, qkey, okey = body["actions"][0]["action_id"].split(":", 2)
        polar.on_q_answer(uid(body), rid(body), qkey, okey)

    def split_value(body: Dict[str, Any]):
        return rid(body).split(":", 1)

    @app.action("polar_qreset")
    def _qreset(ack, body):
        ack()
        r, q = split_value(body)
        polar.on_q_reset(uid(body), r, q)

    @app.action("polar_qother")
    def _qother(ack, body):
        ack()
        r, q = split_value(body)
        polar.on_q_other_click(uid(body), r, q, body["trigger_id"])

    @app.action("polar_qnote")
    def _qnote(ack, body):
        ack()
        polar.on_q_note_click(uid(body), rid(body), body["trigger_id"])

    @app.action("polar_qaccept")
    def _qaccept(ack, body):
        ack()
        polar.on_q_accept(uid(body), rid(body))

    @app.action("polar_qsend")
    def _qsend(ack, body):
        ack()
        polar.on_q_send(uid(body), rid(body))

    @app.action("polar_group_go")
    def _group_go(ack, body):
        ack()
        polar.on_group_go(uid(body), rid(body))

    @app.event("message")
    def _message(event):
        polar.on_message(event)

    @app.event("app_mention")
    def _mention(event):
        polar.on_mention(event)

    @app.action(re.compile(r"^polar_pick_check:"))
    def _pick_check(ack):
        ack()  # the state is read when the button is clicked

    @app.action("polar_pick_go")
    def _pick_go(ack, body):
        ack()
        polar.on_pick_go(uid(body), rid(body), body.get("state") or {},
                         (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id"),
                         (body.get("message") or {}).get("ts") or (body.get("container") or {}).get("message_ts"))

    @app.action("polar_pick_orig")
    def _pick_orig(ack, body):
        ack()
        polar.on_pick_orig(uid(body), rid(body))

    @app.action("polar_route_new")
    def _route_new(ack, body):
        ack()
        polar.on_route_new(uid(body), rid(body))

    @app.action("polar_route_yes")
    def _route_yes(ack, body):
        ack()
        polar.on_route_yes(uid(body), rid(body))

    @app.view("polar_q_submit")
    def _q_submit(ack, body, view):
        err = polar.on_q_submit(uid(body), view)
        ack(err) if err else ack()

    @app.view("polar_revise_submit")
    def _revise_submit(ack, body, view):
        err = polar.on_revise_submit(uid(body), view)
        ack(err) if err else ack()

    @app.view("polar_edit_submit")
    def _edit_submit(ack, body, view):
        err = polar.on_edit_submit(uid(body), view)
        ack(err) if err else ack()


def build(conn=None) -> Polar:
    from slack_bolt import App
    from slack_sdk import WebClient

    bot, user = get_token("bot"), get_token("user")
    if not (bot and user and get_token("app")):
        raise RuntimeError("missing tokens: run `polar doctor`")
    app = App(token=bot)
    polar = Polar(conn or db.connect(), app.client, WebClient(token=user), owner_id())
    register(app, polar)
    polar.app = app  # type: ignore[attr-defined]
    return polar


def serve() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    try:
        polar = build()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    polar.recover()
    stop = threading.Event()

    burst = alarm.Burst(3, 300)

    def loop() -> None:
        while not stop.wait(TICK_SECONDS):
            try:
                polar.tick()
            except Exception as exc:  # keep the loop alive
                log.error("tick failed: %s", error_code(exc))
                if burst.hit():  # 3 failed ticks in 5 minutes: not a blip
                    alarm.alarm("polar:tick", "Polar: el ciclo de entrega falla", "tick failed: %s" % error_code(exc))

    threading.Thread(target=loop, daemon=True).start()
    from polar import webapi
    webapi.start(polar)  # a busy port or a bad setting only turns it off
    log.info("polar serving for owner %s", polar.owner_id)
    SocketModeHandler(polar.app, get_token("app")).start()  # type: ignore[attr-defined]
    return 0


def doctor() -> int:
    """Say which tokens exist and who they belong to. Print no token."""
    from slack_sdk import WebClient

    ok = True
    for name in TOKENS:
        token = get_token(name)
        if not token:
            print(f"{name}: token no encontrado")
            ok = False
            continue
        try:
            if name == "app":
                WebClient(token=token).api_call("apps.connections.open")
                print("app: token existe, Socket Mode responde")
            else:
                r = WebClient(token=token).auth_test()
                print(f"{name}: existe · equipo {r.get('team')} · usuario {r.get('user')}")
        except Exception as exc:
            print(f"{name}: existe, pero Slack lo rechazó ({error_code(exc)})")
            ok = False
    print(f"POLAR_OWNER_ID: {owner_id()}")
    return 0 if ok else 1
