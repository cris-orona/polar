"""The app logic. It takes Slack clients as arguments, so tests can use fakes."""
from __future__ import annotations

import logging
import os
import sqlite3
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from polar import alarm, blocks, db, followups
from polar.tasks import OK_SUBTYPES, OWNER_DM, Tasks, _epoch, card_has_reader

log = logging.getLogger("polar")

FALLBACK_MAX_AGE = 30 * 60  # seconds: how long a thread reply may stand in for a modal that did not open
NOTICE_TAIL = "No pude abrir la ventana. Escribe aquí, en este hilo, "


def _repost_min() -> float:
    """Minutes before a changed «Pendientes» list is posted again at the bottom (POLAR_PENDING_REPOST_MIN)."""
    try:
        return float(os.environ.get("POLAR_PENDING_REPOST_MIN", "30"))
    except ValueError:
        return 30.0


def web_first() -> bool:
    """True when cards go to the web only and Slack sends reminders (POLAR_DELIVERY=web). Default: dm."""
    return (os.environ.get("POLAR_DELIVERY") or "dm").strip().lower() == "web"


def slack_reminders() -> bool:
    """False when POLAR_SLACK_REMINDERS=off: web mode posts no reminder DMs (Mando pushes to the phone instead;
    owner, 2026-11-23: «quita los avisos de Polar en Slack a mi teléfono»). Old reminders are still deleted."""
    return (os.environ.get("POLAR_SLACK_REMINDERS") or "on").strip().lower() not in ("off", "0", "no", "false")


REMINDER_MINUTES = (30, 60, 240)  # age of an open card at each reminder
REMINDER_LATE_MIN = 10            # a reminder later than this shows the real age
REMINDER_MAX_LINES = 15
QUIET_FROM, QUIET_TO = 22, 8      # local hours: no reminder from 22:00 until 08:00


def _reminder_tz() -> Any:
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(os.environ.get("POLAR_TZ") or "UTC")
    except Exception:
        return ZoneInfo("America/Mexico_City")


def _age_label(minutes: float) -> str:
    m = int(minutes)
    if m < 60:
        return "%d min" % m
    if m < 24 * 60:
        return "%d h" % (m // 60)
    return "%d d" % (m // (24 * 60))


def error_code(exc: Exception) -> str:
    """A short Slack error code. Never the exception text, which could hold a token."""
    resp = getattr(exc, "response", None)
    try:
        code = resp["error"] if resp is not None else None
    except (KeyError, TypeError):
        code = None
    return str(code or type(exc).__name__)[:80]


MAX_CARD_FAILS = 5
TOO_LONG = ("msg_blocks_too_long", "msg_too_long")  # Slack: the card does not fit


class _NoOriginal(Exception):
    pass


class _BadCard(Exception):
    """The card could not be built. The cause is in __cause__."""


def _card_error(exc: Exception) -> bool:
    """True when a retry cannot fix the post: a bad card, not a network or auth problem."""
    resp = getattr(exc, "response", None)
    if resp is not None:  # a Slack API error
        code = error_code(exc)
        return code.startswith("invalid") and not code.startswith("invalid_auth") \
            or code in TOO_LONG
    return isinstance(exc, (TypeError, KeyError, ValueError))


def _res(src: Optional[str], **fields: Any) -> str:
    """The stored result of a decision. `src` is «web» when the web API acted; it adds one key and nothing else."""
    if src:
        fields["src"] = src
    return json.dumps(fields, ensure_ascii=False)


class Polar:
    def __init__(self, conn: sqlite3.Connection, bot_client: Any, user_client: Any, owner_id: str,
                 gmail_service: Any = None):
        self.conn = conn
        self.bot = bot_client    # DMs and message updates
        self.user = user_client  # posts as the owner
        self.owner_id = owner_id
        self._gmail = gmail_service  # a Gmail API service; None means "build it from the token file"
        self._card_fails: Dict[str, int] = {}  # failures per request that a retry cannot fix
        self._sync_retry_at: Dict[str, float] = {}  # companion message -> no Slack call before this time
        self._qlock = threading.RLock()  # answers are read, changed and saved in one step
        self.tasks = Tasks(conn, bot_client, owner_id)
        self.tasks.intake = self.on_message  # catch-up goes through the same entry as live events

    # -- owner check -------------------------------------------------------
    def is_owner(self, user_id: Optional[str]) -> bool:
        if user_id == self.owner_id:
            return True
        log.warning("ignored a click from a user who is not the owner: %s", user_id)
        return False

    # -- the message a reply answers ---------------------------------------
    def gmail(self) -> Any:
        if self._gmail is None:
            from polar import gmail
            self._gmail = gmail.service()
        return self._gmail

    def fetch_original_email(self, req: Dict[str, Any]) -> None:
        """Quote the last message of the Gmail thread. On failure, log the code only."""
        from polar import gmail
        found: Dict[str, Any] = {}
        try:
            info = gmail.quote_info(self.gmail(), req["target_thread_ts"])
            found = {k: v for k, v in info.items() if v}
            if "subject" in found:
                found["email_subject"] = found.pop("subject")
        except Exception as exc:
            log.warning("email original fetch failed for %s: %s", req["id"], gmail.error_code(exc))
        db.set_original(self.conn, req["id"], **found)

    def fetch_original(self, req: Dict[str, Any]) -> None:
        """Read the message with the user token and store it. On failure, log the code only."""
        if req["kind"] == "email":
            return self.fetch_original_email(req)
        channel = req["target_channel"]
        thread = req.get("target_thread_ts")
        exact = req.get("reply_to_ts")
        found: Dict[str, Any] = {}
        try:
            if not (thread or exact):
                raise _NoOriginal()  # a new message: there is nothing to quote
            msgs = self.user.conversations_replies(
                channel=channel, ts=thread or exact, limit=200)["messages"]
            if exact:
                pick = next((m for m in msgs if m.get("ts") == exact), None)
            else:
                pick = next((m for m in reversed(msgs)
                             if m.get("user") != self.owner_id and m.get("text")), None)
            if pick:
                found["orig_text"] = " ".join((pick.get("text") or "").split())[:300]
                found["orig_time"] = blocks.mx_hhmm(pick.get("ts"))
                found["orig_name"] = self._display_name(pick)
                try:
                    found["orig_permalink"] = self.user.chat_getPermalink(
                        channel=channel, message_ts=pick["ts"])["permalink"]
                except Exception as exc:
                    log.warning("original permalink failed for %s: %s", req["id"], error_code(exc))
        except _NoOriginal:
            pass
        except Exception as exc:
            log.warning("original fetch failed for %s: %s", req["id"], error_code(exc))
        if not channel.startswith("D"):
            try:
                found["orig_channel_name"] = self.user.conversations_info(
                    channel=channel)["channel"]["name"]
            except Exception as exc:
                log.warning("channel name failed for %s: %s", req["id"], error_code(exc))
        db.set_original(self.conn, req["id"], **found)

    def _display_name(self, msg: Dict[str, Any]) -> Optional[str]:
        uid = msg.get("user")
        if not uid:
            return msg.get("username") or None
        try:
            user = self.user.users_info(user=uid)["user"]
            prof = user.get("profile") or {}
            name = prof.get("display_name") or (user.get("real_name") or "").split(" ")[0]
            return name or user.get("name") or None
        except Exception as exc:
            log.warning("user name failed: %s", error_code(exc))
            return None

    # -- DM delivery -------------------------------------------------------
    def deliver_pending(self) -> int:
        sent = 0
        web = web_first()
        for req in db.list_requests(self.conn, "pending"):
            if req["kind"] in db.REPLY_KINDS or (req["kind"] == "decision" and req.get("agent_where")
                                          and req.get("target_channel") and req.get("reply_to_ts")):
                self.fetch_original(req)
                req = db.get(self.conn, req["id"]) or req
            status = "noted" if req["kind"] == "report" else "shown"  # a report needs no click
            # The web is the channel, or the card is a canary: no DM, so no dm_channel and no dm_ts
            if web or (req.get("extra") or {}).get("canary"):
                if db.transition(self.conn, req["id"], "pending", status):
                    sent += 1
                continue
            try:
                thread = req.get("target_thread_ts") if req["kind"] == "report" else None
                channel, ts = self._fit(req, lambda r: dict(
                    {"text": blocks.fallback_text(r), "blocks": blocks.request_blocks(r)},
                    **({"thread_ts": thread} if thread else {})), self._post_dm)
            except _BadCard as exc:
                self._card_failed(req, exc.__cause__ or exc)
                continue
            except Exception as exc:  # leave it pending: retry on the next tick
                log.error("DM failed for %s: %s", req["id"], error_code(exc))
                if _card_error(exc):
                    self._card_failed(req, exc)
                continue
            self._card_fails.pop(req["id"], None)
            if db.transition(self.conn, req["id"], "pending", status, dm_channel=channel, dm_ts=ts):
                sent += 1
                if req["kind"] == "report":
                    self.upload_report_files(req, channel, ts)
                elif (req.get("extra") or {}).get("files"):
                    self.upload_report_files(dict(req, report=req["extra"]), channel, ts)
        return sent

    def _post_dm(self, **card: Any) -> Any:
        """Post one message in the owner's DM. Return (channel, ts)."""
        channel = self.bot.conversations_open(users=self.owner_id)["channel"]["id"]
        if db.get_meta(self.conn, "owner_dm") != channel:
            db.set_meta(self.conn, "owner_dm", channel)
        return channel, self.bot.chat_postMessage(channel=channel, **card)["ts"]

    def _fit(self, req: Dict[str, Any], build: Any, send: Any) -> Any:
        """Send a card in a size that Slack accepts. `build(req)` returns the arguments of `send`.

        When Slack answers that the card is too long, cut the body one step and send again. Store the
        step that Slack accepted: a later edit of the card starts from it."""
        first = level = blocks.body_level(req)
        while True:
            try:
                card = build(dict(req, compact=level))
            except Exception as exc:
                raise _BadCard() from exc
            try:
                resp = send(**card)
                break
            except Exception as exc:
                step = blocks.next_level(req, level) if error_code(exc) in TOO_LONG else None
                if step is None:
                    raise
                level = step
        if level != first:
            db.set_compact(self.conn, req["id"], level)
            log.warning("card %s was too long for Slack: body cut to %d characters",
                        req["id"], blocks.BODY_LIMITS[level])
        return resp

    def _card_failed(self, req: Dict[str, Any], exc: Exception) -> None:
        """Count a failure that a retry cannot fix. After MAX_CARD_FAILS, stop and tell the owner."""
        code = error_code(exc)
        n = self._card_fails.get(req["id"], 0) + 1
        self._card_fails[req["id"]] = n
        log.error("card %s cannot be shown (%d/%d): %s", req["id"], n, MAX_CARD_FAILS, code)
        if n < MAX_CARD_FAILS:
            return
        self._card_fails.pop(req["id"], None)
        if not db.transition(self.conn, req["id"], "pending", "failed",
                             result_json=json.dumps({"error": code}), decided_at=db.now_iso()):
            return
        try:
            channel = self.bot.conversations_open(users=self.owner_id)["channel"]["id"]
            self.bot.chat_postMessage(
                channel=channel,
                text="⚠️ No pude mostrar la tarjeta «%s» (%s). Avisé a la sesión que la pidió."
                % (blocks.clip(req["title"], 150), code))
        except Exception as exc2:
            log.error("failure notice failed for %s: %s", req["id"], error_code(exc2))

    def upload_report_files(self, req: Dict[str, Any], channel: str, ts: str) -> None:
        """Attach the files of a report card in its thread. A failure never fails the card."""
        if not channel or not ts:  # web mode: no DM, no thread
            return
        for path in (req.get("report") or {}).get("files") or []:
            name = os.path.basename(path)
            try:
                self.bot.files_upload_v2(channel=channel, thread_ts=ts, file=path, title=name)
                continue
            except Exception as exc:
                code = error_code(exc)
                if isinstance(exc, OSError):
                    code = "no existe o no se puede leer"
            log.warning("file upload failed for %s: %s", req["id"], code)
            if code == "missing_scope":
                why = "falta el permiso files:write en la app Polar."
            else:
                why = code + "."
            try:
                self.bot.chat_postMessage(channel=channel, thread_ts=ts,
                                          text=f"No pude adjuntar {name}: {why}")
            except Exception as exc:
                log.error("file notice failed for %s: %s", req["id"], error_code(exc))

    def refresh_dm(self, rid: str, hint: Optional[str] = None) -> None:
        req = db.get(self.conn, rid)
        if not req or not req.get("dm_ts"):
            return
        live = req["status"] in db.OPEN_STATUSES or req["status"] == "noted"

        def build(r: Dict[str, Any]) -> Dict[str, Any]:
            return {"text": blocks.fallback_text(r),
                    "blocks": blocks.request_blocks(r, hint) if live else blocks.resolved_blocks(r)}
        try:
            self._fit(req, build, lambda **card: self.bot.chat_update(
                channel=req["dm_channel"], ts=req["dm_ts"], **card))
        except _BadCard as exc:
            raise (exc.__cause__ or exc)
        except Exception as exc:
            log.error("DM update failed for %s: %s", rid, error_code(exc))
            return
        if not live:
            self.conn.execute("UPDATE requests SET dm_done=1 WHERE id=?", (rid,))

    def sweep(self) -> None:
        """Expire old requests, then bring every finished DM up to date."""
        db.expire_due(self.conn)
        for step in (self.sync_pending, lambda: db.fold_reports(self.conn)):
            try:
                step()
            except Exception as exc:  # a bad round here must not stop the DM refresh or the task loop
                log.error("sweep step failed: %s", error_code(exc))
        rows = self.conn.execute(
            "SELECT id FROM requests WHERE dm_ts IS NOT NULL AND dm_done=0"
            " AND status IN ('sent','chosen','discarded','expired','failed','answered','skipped','revise','replaced','filed')"
        ).fetchall()
        for r in rows:
            self.refresh_dm(r["id"])
        self.sync_prod_group()

    # -- one «Go» for a group of production steps ---------------------------
    GROUP_DEBOUNCE_SECONDS = 20

    def prod_group_cards(self) -> list:
        """The open prod-op steps with exactly the options go and no, oldest first."""
        rows = self.conn.execute(
            "SELECT id FROM requests WHERE status='shown' AND kind='decision' AND requester LIKE 'prod-op %'"
            " AND dm_ts IS NOT NULL AND dm_channel IS NOT NULL ORDER BY created_at, rowid").fetchall()
        out = []
        for r in rows:
            req = db.get(self.conn, r["id"])
            if req and [o["key"] for o in req["options"]] == ["go", "no"]:
                out.append(req)
        return out

    def sync_prod_group(self, now: Optional[datetime] = None) -> None:
        """Keep one companion message in the DM that lists the open production steps."""
        if web_first():  # the web shows the prod-op cards one by one
            return
        with self._qlock:
            try:
                self._sync_prod_group(now)
            except Exception as exc:  # a bad round must not stop the sweep
                log.error("prod group sync failed: %s", error_code(exc))

    def _sync_prod_group(self, now: Optional[datetime]) -> None:
        if self._backing_off("prod_group"):
            return
        now = now or datetime.now(timezone.utc)
        state = db.get_meta(self.conn, "prod_group")
        reqs = self.prod_group_cards()
        if reqs:
            newest = datetime.strptime(reqs[-1]["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if (now - newest).total_seconds() < self.GROUP_DEBOUNCE_SECONDS:
                return
        ids = [r["id"] for r in reqs[:blocks.GROUP_MAX]]
        if len(ids) < 2:
            if state:
                try:
                    self.bot.chat_delete(channel=state["channel"], ts=state["ts"])
                except Exception as exc:
                    log.error("group delete failed: %s", error_code(exc))
                    if error_code(exc) not in self.GONE:
                        self._back_off("prod_group")
                        return
                db.set_meta(self.conn, "prod_group", None)
            return
        if state and state["ids"] == ids:
            return
        if state and set(ids) <= set(state["ids"]):
            try:
                self.bot.chat_update(channel=state["channel"], ts=state["ts"],
                                     text=blocks.prod_group_text(len(ids)),
                                     blocks=blocks.prod_group_blocks(reqs, state["gid"]))
            except Exception as exc:
                code = error_code(exc)
                log.error("group update failed: %s", code)
                if code in self.GONE:
                    db.set_meta(self.conn, "prod_group", None)  # gone: the next round posts a new one
                else:
                    self._back_off("prod_group")
                return
            db.set_meta(self.conn, "prod_group", dict(state, ids=ids))
            return
        gid = uuid.uuid4().hex[:8]
        try:
            channel = self.bot.conversations_open(users=self.owner_id)["channel"]["id"]
            posted = self.bot.chat_postMessage(channel=channel, text=blocks.prod_group_text(len(ids)),
                                               blocks=blocks.prod_group_blocks(reqs, gid))
        except Exception as exc:
            log.error("group post failed: %s", error_code(exc))
            self._back_off("prod_group")
            return
        if state:
            try:
                self.bot.chat_delete(channel=state["channel"], ts=state["ts"])
            except Exception as exc:
                log.error("group delete failed: %s", error_code(exc))
        db.set_meta(self.conn, "prod_group", {"gid": gid, "ts": posted["ts"],
                                              "channel": posted.get("channel") or channel, "ids": ids})

    def on_group_go(self, user_id: str, gid: str) -> None:
        if not self.is_owner(user_id):
            return
        with self._qlock:
            state = db.get_meta(self.conn, "prod_group")
            if not state or state.get("gid") != gid:
                return
            moved = []
            for rid in state["ids"]:
                if db.transition(self.conn, rid, "shown", "chosen",
                                 result_json=json.dumps({"option": "go", "via": "group"}),
                                 decided_at=db.now_iso()):
                    moved.append(rid)
            db.set_meta(self.conn, "prod_group", None)
        # The Slack calls run with the lock free. A card that is not redrawn here is redrawn by the next sweep.
        for rid in moved:
            self.refresh_dm(rid)
        try:
            self.bot.chat_update(channel=state["channel"], ts=state["ts"],
                                 text=blocks.prod_group_done_text(len(moved)),
                                 blocks=blocks.prod_group_done(len(moved)))
        except Exception as exc:
            log.error("group done update failed: %s", error_code(exc))

    def sync_pending(self, now: Optional[str] = None) -> None:
        """Keep one «Pendientes» message that lists the cards the owner passed over."""
        if web_first():  # reminders replace it; remove a list left from dm mode
            old = db.get_meta(self.conn, "pendientes") or {}
            if old.get("ts"):
                self._pending_delete(old.get("channel"), old["ts"])
                db.set_meta(self.conn, "pendientes", None)
            return
        if self._backing_off("pendientes"):
            return
        now = now or db.now_iso()
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now_dt = datetime.strptime(now, fmt)
        reqs = db.passed_over(self.conn, now)
        ids = [r["id"] for r in reqs]
        state = db.get_meta(self.conn, "pendientes") or {}
        ts, channel = state.get("ts"), state.get("channel")
        if not reqs:
            if ts:
                self._pending_delete(channel, ts)
                db.set_meta(self.conn, "pendientes", None)
            return
        view = blocks.pending_blocks(reqs, now_dt)
        text = blocks.pending_text(len(reqs))
        if not ts:
            self._pending_post(view, text, ids, now)
            return
        if ids == state.get("ids"):
            return
        posted = state.get("posted_ids") or []
        if set(ids) <= set(posted):
            if self._pending_update(channel, ts, view, text):
                state["ids"] = ids
                db.set_meta(self.conn, "pendientes", state)
            return
        try:
            posted_at = datetime.strptime(state.get("posted_at") or now, fmt)
        except ValueError:
            posted_at = now_dt
        if (now_dt - posted_at).total_seconds() >= _repost_min() * 60:
            self._pending_delete(channel, ts)
            db.set_meta(self.conn, "pendientes", None)  # the old message is gone, even if delete failed
            self._pending_post(view, text, ids, now)
        elif self._pending_update(channel, ts, view, text):
            state["ids"] = ids
            db.set_meta(self.conn, "pendientes", state)

    # -- companion messages («Pendientes», group Go): no Slack call every 5 s after a failure -------
    SYNC_BACKOFF_SECONDS = 60
    GONE = ("message_not_found", "channel_not_found", "cant_update_message")

    def _backing_off(self, key: str) -> bool:
        return time.time() < self._sync_retry_at.get(key, 0)

    def _back_off(self, key: str) -> None:
        self._sync_retry_at[key] = time.time() + self.SYNC_BACKOFF_SECONDS

    def _pending_post(self, view: Any, text: str, ids: Any, now: str) -> None:
        try:
            channel = self.bot.conversations_open(users=self.owner_id)["channel"]["id"]
            resp = self.bot.chat_postMessage(channel=channel, text=text, blocks=view)
        except Exception as exc:
            log.error("pending post failed: %s", error_code(exc))
            self._back_off("pendientes")
            return
        db.set_meta(self.conn, "pendientes", {
            "ts": resp["ts"], "channel": channel, "ids": list(ids),
            "posted_ids": list(ids), "posted_at": now})

    def _pending_delete(self, channel: Any, ts: Any) -> None:
        try:
            self.bot.chat_delete(channel=channel, ts=ts)
        except Exception as exc:  # most often the message is already gone
            log.warning("pending delete failed: %s", error_code(exc))

    def _pending_update(self, channel: Any, ts: Any, view: Any, text: str) -> bool:
        try:
            self.bot.chat_update(channel=channel, ts=ts, text=text, blocks=view)
            return True
        except Exception as exc:
            code = error_code(exc)
            log.error("pending update failed: %s", code)
            if code in self.GONE:
                db.set_meta(self.conn, "pendientes", None)  # the message is gone: the next round posts a new one
            else:
                self._back_off("pendientes")
            return False

    def tick(self) -> None:
        self.deliver_pending()
        self.sweep()
        try:
            self.sync_reminders()
        except Exception as exc:  # a bad round here must not stop the task loop
            log.error("reminder step failed: %s", error_code(exc))
        self.tasks.tick()

    # -- web mode: Slack only reminds about open action cards ------------------------
    def _card_url(self, rid: str) -> Optional[str]:
        base = (os.environ.get("POLAR_WEB_URL") or "").strip().rstrip("/")
        return "%s/?c=%s" % (base, rid) if base else None

    def _open_action_cards(self) -> List[Dict[str, Any]]:
        marks = ",".join("?" * len(db.OPEN_STATUSES))
        rows = self.conn.execute(
            "SELECT id FROM requests WHERE status IN (%s) AND kind != 'report' ORDER BY created_at, rowid" % marks,
            db.OPEN_STATUSES).fetchall()
        return [r for r in (db.get(self.conn, x["id"]) for x in rows) if r]

    def sync_reminders(self, now: Optional[datetime] = None) -> None:
        """One DM that lists the open cards that crossed a reminder age. Only in web mode.

        State is in meta «reminders»: `sent` (card id -> highest age in minutes already reminded) and `msgs`
        (the reminder messages: channel, ts, card ids). A message is deleted when none of its cards is open."""
        if not web_first() or self._backing_off("reminders"):
            return
        now = now or datetime.now(timezone.utc)
        state = db.get_meta(self.conn, "reminders") or {}
        sent: Dict[str, int] = dict(state.get("sent") or {})
        msgs: List[Dict[str, Any]] = list(state.get("msgs") or [])
        cards = self._open_action_cards()
        open_ids = {c["id"] for c in cards}
        # Delete the reminders whose cards are all closed.
        keep = []
        for m in msgs:
            if any(i in open_ids for i in m.get("ids") or []):
                keep.append(m)
                continue
            try:
                self.bot.chat_delete(channel=m["channel"], ts=m["ts"])
            except Exception as exc:
                code = error_code(exc)
                log.warning("reminder delete failed: %s", code)
                if code not in self.GONE:
                    keep.append(m)
                    self._back_off("reminders")
        msgs = keep
        sent = {k: v for k, v in sent.items() if k in open_ids}
        save = lambda: db.set_meta(self.conn, "reminders", {"sent": sent, "msgs": msgs})  # noqa: E731
        local = now.astimezone(_reminder_tz())
        if not slack_reminders() or local.hour >= QUIET_FROM or local.hour < QUIET_TO:
            save()
            return
        due = []  # (card, age in minutes, threshold)
        for c in cards:
            try:
                created = datetime.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            age = (now - created).total_seconds() / 60
            hit = [t for t in REMINDER_MINUTES if age >= t and t > sent.get(c["id"], 0)]
            if hit:
                due.append((c, age, max(hit)))
        if not due:
            save()
            return
        lines = []
        for c, age, thr in due[:REMINDER_MAX_LINES]:
            label = _age_label(thr if age - thr <= REMINDER_LATE_MIN else age)
            title = blocks.clip(blocks._link_text(" ".join((c["title"] or "").split())), 80)
            url = self._card_url(c["id"])
            lines.append("⏰ %s · %s%s" % (label, title, " → <%s|Abrir>" % url if url else ""))
        if len(due) > REMINDER_MAX_LINES:
            lines.append("… y %d más en Mando" % (len(due) - REMINDER_MAX_LINES))
        if len(due) > 1:
            lines.insert(0, "%d cartas te esperan en Mando" % len(due))
        try:
            channel = self.bot.conversations_open(users=self.owner_id)["channel"]["id"]
            resp = self.bot.chat_postMessage(channel=channel, text="\n".join(lines))
        except Exception as exc:
            log.error("reminder post failed: %s", error_code(exc))
            self._back_off("reminders")
            save()
            return
        for c, _age, thr in due:
            sent[c["id"]] = thr
        msgs.append({"channel": channel, "ts": resp["ts"], "ids": [c["id"] for c, _a, _t in due]})
        save()

    # -- DMs from the owner: tasks, steering, pick card -------------------------
    def on_message(self, event: Dict[str, Any]) -> None:
        try:
            if self._take_fallback(event):
                return
            self.tasks.on_message(event)
        except Exception as exc:  # a bad event must not break the app
            log.error("message event failed: %s", error_code(exc))

    # -- modal that did not open: the thread reply takes its place ------------------
    def _open_modal(self, req: Dict[str, Any], trigger_id: str, view: Dict[str, Any], kind: str,
                    qkey: Optional[str] = None) -> bool:
        """Open a text window. When Slack refuses (expired trigger), ask for the text in the card's thread."""
        try:
            self.bot.views_open(trigger_id=trigger_id, view=view)
            db.set_meta(self.conn, "fallback:" + req["id"], None)  # an earlier failed try no longer waits for text
            return True
        except Exception as exc:
            log.warning("modal did not open for %s (%s): %s", req["id"], kind, error_code(exc))
        db.set_meta(self.conn, "fallback:" + req["id"], {"kind": kind, "q": qkey, "at": db.now_iso()})
        if kind == "revise":
            ask = "qué cambio quieres y lo tomo."
        elif kind == "edit":
            ask = "el texto nuevo de la respuesta y lo guardo."
        elif kind == "qother":
            n = next((i for i, q in enumerate(req.get("options") or [], 1) if q["key"] == qkey), "?")
            ask = "tu respuesta a la pregunta %s y la tomo." % n
        else:
            ask = "tu nota y la guardo."
        self._say_thread(req, NOTICE_TAIL + ask)
        return False

    def _say_thread(self, req: Dict[str, Any], text: str) -> None:
        if not req.get("dm_ts"):  # web mode: the card has no thread
            return
        try:
            self.bot.chat_postMessage(channel=req["dm_channel"], thread_ts=req["dm_ts"], text=text)
        except Exception as exc:
            log.error("thread notice failed for %s: %s", req["id"], error_code(exc))

    def _take_fallback(self, event: Dict[str, Any]) -> bool:
        """The owner answers in a card's thread after its modal did not open: apply the text through the
        modal's own submit method. True when the message was used (or was already used)."""
        ts, thread = event.get("ts"), event.get("thread_ts")
        text = (event.get("text") or "").strip()
        if (event.get("user") != self.owner_id or event.get("bot_id") or event.get("subtype") not in OK_SUBTYPES
                or event.get("channel_type") not in (None, "im") or not ts or not thread or thread == ts
                or not text):
            return False
        req = self.conn.execute("SELECT id FROM requests WHERE dm_ts=?", (thread,)).fetchone()
        if not req:
            return False
        rid = req["id"]
        with self._qlock:  # check, submit and marker in one step: a catch-up replay of the same message waits
            return self._apply_fallback(rid, thread, ts, text)

    def _apply_fallback(self, rid: str, thread: str, ts: str, text: str) -> bool:
        fb = db.get_meta(self.conn, "fallback:" + rid)
        if not isinstance(fb, dict):
            return False
        asked = _epoch(fb.get("at") or "")
        if time.time() - asked >= FALLBACK_MAX_AGE:
            db.set_meta(self.conn, "fallback:" + rid, None)
            return False
        try:
            if float(ts) <= asked:
                return False  # written before Polar asked for the text: it is not the answer
        except (TypeError, ValueError):
            return False
        if self.conn.execute("SELECT 1 FROM tasks WHERE msg_ts=?", (ts,)).fetchone():
            return True  # this message was already used
        card = db.get(self.conn, rid)
        if not card or card["status"] not in ("shown", "noted"):  # noted: a report can get «Pedir cambios»
            db.set_meta(self.conn, "fallback:" + rid, None)  # nothing left to change
            return False
        kind, qkey = fb.get("kind"), fb.get("q")
        before = (card["status"], card.get("result_json"), card.get("final_text"))
        if kind == "revise":
            err = self.on_revise_submit(self.owner_id, blocks.text_view(text, rid))
        elif kind == "edit":
            err = self.on_edit_submit(self.owner_id, blocks.text_view(text, rid))
        elif kind in ("qother", "qnote"):
            meta = json.dumps({"id": rid, "q": qkey if kind == "qother" else None})
            err = self.on_q_submit(self.owner_id, blocks.text_view(text, meta))
        else:
            db.set_meta(self.conn, "fallback:" + rid, None)
            return False
        sid = self.tasks.create_steer(card.get("dm_channel") or OWNER_DM, thread, ts, text)
        if sid:
            self.tasks.update(sid, status="delivered", notified=1)
        if err:
            msg = next(iter((err.get("errors") or {}).values()), "No pude tomarlo.")
            self._say_thread(card, "⚠️ " + msg)
            return True
        db.set_meta(self.conn, "fallback:" + rid, None)
        after = db.get(self.conn, rid) or {}
        changed = (after.get("status"), after.get("result_json"), after.get("final_text")) != before
        self._say_thread(card, "✅ Tomado." if changed else "⚠️ No pude tomarlo: la tarjeta ya cambió.")
        return True

    def on_mention(self, event: Dict[str, Any]) -> None:
        try:
            self.tasks.on_mention(event)
        except Exception as exc:
            log.error("mention event failed: %s", error_code(exc))

    def on_pick_go(self, user_id: str, pick_id: str, state: Dict[str, Any], channel: str, ts: str) -> None:
        if not self.is_owner(user_id):
            return
        pick = self.tasks.get_pick(pick_id)
        if not pick or pick["status"] not in ("open", "planned"):
            return
        chosen = blocks.picked_indexes(state)
        if not chosen:
            self._pick_update(channel, ts, blocks.pick_blocks(pick, [], "Marca al menos una tarea."))
            return
        if self.tasks.start_pick(pick_id, chosen) is None:
            return
        self._pick_update(channel, ts, blocks.picked_blocks(pick, chosen))

    def on_pick_orig(self, user_id: str, pick_id: str) -> None:
        if self.is_owner(user_id):
            self.tasks.post_original(pick_id)

    def on_route_new(self, user_id: str, tid: str) -> None:
        if self.is_owner(user_id):
            self.tasks.route_new(tid)

    def on_route_yes(self, user_id: str, tid: str) -> None:
        if self.is_owner(user_id):
            self.tasks.route_yes(tid)

    def _pick_update(self, channel: str, ts: str, new_blocks: Any) -> None:
        try:
            self.bot.chat_update(channel=channel, ts=ts, text="Tareas", blocks=new_blocks)
        except Exception as exc:
            log.error("pick update failed: %s", error_code(exc))

    def on_reopen(self, user_id: str, rid: str) -> None:
        if not self.is_owner(user_id):
            return
        if db.reopen(self.conn, rid):
            self.refresh_dm(rid)
        else:
            self._stale(rid)

    # -- a click that changes nothing must say so --------------------------
    STALE_WORDS = {
        "pending": "en cola", "shown": "abierta", "sending": "en envío", "sent": "enviada",
        "chosen": "respondida", "answered": "respondida", "discarded": "descartada",
        "replaced": "reemplazada", "expired": "vencida", "failed": "con error", "skipped": "saltada",
        "revise": "con cambios pedidos", "noted": "leída", "filed": "archivada",
    }

    def _stale(self, rid: str, src: Optional[str] = None) -> None:
        """A press found the card in another state. Tell the owner in that DM (only he sees it) and redraw the
        card, so the old buttons go away. The web API (`src`) answers its caller itself."""
        if src:
            return
        req = db.get(self.conn, rid)
        if not req:
            return
        word = self.STALE_WORDS.get(req["status"])
        text = ("Esta tarjeta ya estaba %s. No hice nada." % word) if word else \
            "Esta tarjeta ya cambió. No hice nada."
        if req.get("dm_channel"):
            try:
                self.bot.chat_postEphemeral(channel=req["dm_channel"], user=self.owner_id, text=text)
            except Exception as exc:
                log.error("stale notice failed for %s: %s", rid, error_code(exc))
        self.refresh_dm(rid)

    # -- clicks ------------------------------------------------------------
    def on_send(self, user_id: str, rid: str, src: Optional[str] = None) -> None:
        if not self.is_owner(user_id):
            return
        req = db.get(self.conn, rid)
        if not req or req["kind"] not in db.REPLY_KINDS:
            return
        # Claim the request first. A second click finds status "sending" and stops.
        if not db.transition(self.conn, rid, "shown", "sending"):
            return self._stale(rid, src)
        text = blocks.reply_text(req)
        if req["kind"] == "email":
            return self._send_email(req, text, src)
        try:
            # markdown_text renders standard Markdown (**bold**, [link](url), lists), which is what agents write.
            # Slack refuses it together with `text` or `blocks`.
            kwargs: Dict[str, Any] = {"channel": req["target_channel"], "markdown_text": text}
            if req.get("target_thread_ts"):
                kwargs["thread_ts"] = req["target_thread_ts"]
            posted = self.user.chat_postMessage(**kwargs)
            result: Dict[str, Any] = {"channel": posted["channel"], "ts": posted["ts"]}
            if src:
                result["src"] = src
            try:
                result["permalink"] = self.user.chat_getPermalink(
                    channel=posted["channel"], message_ts=posted["ts"])["permalink"]
            except Exception as exc:  # the send worked; only the link is missing
                log.warning("permalink failed for %s: %s", rid, error_code(exc))
            db.transition(self.conn, rid, "sending", "sent", final_text=text,
                          result_json=json.dumps(result), decided_at=db.now_iso())
        except Exception as exc:
            db.transition(self.conn, rid, "sending", "failed",
                          result_json=_res(src, error=error_code(exc)),
                          decided_at=db.now_iso())
            alarm.alarm("polar:send-failed", "Polar: no salió un mensaje que aprobaste",
                        "%s no se envió: %s" % (rid, error_code(exc)))
        self.refresh_dm(rid)

    def _send_email(self, req: Dict[str, Any], text: str, src: Optional[str] = None) -> None:
        from polar import gmail
        rid = req["id"]
        try:
            res = gmail.send_reply(self.gmail(), req["target_thread_ts"], text, req.get("email_cc"))
            db.transition(self.conn, rid, "sending", "sent", final_text=text,
                          result_json=json.dumps(dict(res, src=src) if src else res), decided_at=db.now_iso())
        except Exception as exc:
            code = "Gmail no está conectado" if isinstance(exc, gmail.NotConnected) else gmail.error_code(exc)
            db.transition(self.conn, rid, "sending", "failed",
                          result_json=_res(src, error=code), decided_at=db.now_iso())
            alarm.alarm("polar:send-failed", "Polar: no salió un correo que aprobaste", "%s no se envió: %s" % (rid, code))
        self.refresh_dm(rid)

    def on_discard(self, user_id: str, rid: str, src: Optional[str] = None) -> None:
        if not self.is_owner(user_id):
            return
        extra = {"result_json": _res(src)} if src else {}
        if db.transition(self.conn, rid, "shown", "discarded", decided_at=db.now_iso(), **extra):
            self.refresh_dm(rid)
        else:
            self._stale(rid, src)

    def on_revise_click(self, user_id: str, rid: str, trigger_id: str) -> None:
        if not self.is_owner(user_id):
            return
        req = db.get(self.conn, rid)
        if not req or req["kind"] not in ("reply", "decision", "email", "report", "questions"):
            return
        if req["status"] not in ("shown", "noted"):
            return self._stale(rid)
        self._open_modal(req, trigger_id, blocks.revise_modal(req), "revise")

    def on_revise_submit(self, user_id: str, view: Dict[str, Any], src: Optional[str] = None,
                         respond: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Return a Slack error response, or None when the request is saved."""
        if not self.is_owner(user_id):
            return None
        text = blocks.modal_text(view).strip()
        if not text:
            return {"response_action": "errors", "errors": {"text_block": "Escribe qué cambio quieres."}}
        if len(text) > db.MAX_REPLY_CHARS:
            return {"response_action": "errors", "errors": {
                "text_block": f"Máximo {db.MAX_REPLY_CHARS} caracteres."}}
        self._to_revise(view["private_metadata"], text, src=src, extra=respond)
        return None

    @staticmethod
    def _next_indexes(index: Any, indexes: Any, count: int) -> Optional[List[int]]:
        """The follow-up numbers of one press, ascending, or None when the input is bad."""
        if indexes is None:
            indexes = [index]
        if not isinstance(indexes, (list, tuple)) or not 1 <= len(indexes) <= db.MAX_REPORT_NEXT:
            return None
        if any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < count for i in indexes):
            return None
        if len(set(indexes)) != len(indexes):
            return None
        return sorted(indexes)

    def on_next(self, user_id: str, rid: str, index: Optional[int] = None, src: Optional[str] = None,
                indexes: Optional[List[int]] = None, respond: Optional[Dict[str, Any]] = None) -> None:
        """The owner starts one or more follow-ups that the report suggests: same as «Pedir cambios» with that text.
        `index` is one follow-up; `indexes` is a list of distinct ones (1 to 3). One step: `instructions` is the
        step text. Several: one line «1) A 2) B»."""
        if not self.is_owner(user_id):
            return
        req = db.get(self.conn, rid)
        if not req or req["kind"] != "report":
            return
        if req["status"] not in ("shown", "noted"):
            return self._stale(rid, src)
        steps = (req.get("report") or {}).get("next") or []
        picked = self._next_indexes(index, indexes, len(steps))
        if picked is None:
            return
        texts = [steps[i] for i in picked]
        text = texts[0] if len(texts) == 1 else " ".join("%d) %s" % (n, t) for n, t in enumerate(texts, 1))
        self._to_revise(rid, text, via="next", src=src, extra=dict(respond or {}, indexes=picked, steps=texts))

    # -- scheduled follow-ups ------------------------------------------------
    def later(self, rid: str, key: Any) -> Dict[str, Any]:
        """«Revisar después»: schedule a follow-up for this card and redraw it. Return the bus JSON.
        Raise followups.FollowupError (Spanish text, HTTP code)."""
        req = db.get(self.conn, rid)
        if not req:
            raise followups.FollowupError("No existe esa tarjeta.", 409)
        data = followups.add_later(self.conn, req, key)
        self.refresh_dm(rid)
        return data

    def fu_cancel(self, rid: str, fu: Any) -> Dict[str, Any]:
        """Cancel one follow-up of the card with the bus and redraw the card. Raise followups.FollowupError."""
        req = db.get(self.conn, rid)
        if not req:
            raise followups.FollowupError("No existe esa tarjeta.", 409)
        data = followups.cancel_followup(self.conn, req, fu)
        self.refresh_dm(rid)
        return data

    def _tell_owner(self, rid: str, text: str) -> None:
        req = db.get(self.conn, rid)
        if not req or not req.get("dm_channel"):
            return
        try:
            self.bot.chat_postEphemeral(channel=req["dm_channel"], user=self.owner_id, text=text)
        except Exception as exc:
            log.error("follow-up notice failed for %s: %s", rid, error_code(exc))

    def on_later(self, user_id: str, value: str) -> None:
        """Slack select «Revisar después». `value` is «<card id>:<key>»."""
        if not self.is_owner(user_id):
            return
        rid, _, key = (value or "").partition(":")
        try:
            self.later(rid, key)
        except followups.FollowupError as exc:
            self._tell_owner(rid, exc.error)

    def on_fu_cancel(self, user_id: str, value: str) -> None:
        """Slack «Cancelar» of a follow-up line. `value` is «<card id>:<follow-up id>»."""
        if not self.is_owner(user_id):
            return
        rid, _, fu = (value or "").partition(":")
        try:
            self.fu_cancel(rid, int(fu))
        except ValueError:
            return
        except followups.FollowupError as exc:
            self._tell_owner(rid, exc.error)

    def on_next_go(self, user_id: str, rid: str, state: Dict[str, Any]) -> None:
        """«Empezar marcados» on a card with 2 or 3 follow-ups: start the ones that are checked."""
        if not self.is_owner(user_id):
            return
        req = db.get(self.conn, rid)
        if not req or req["kind"] != "report":
            return
        if req["status"] not in ("shown", "noted"):
            return self._stale(rid)
        chosen = blocks.next_checked(state, rid)
        if not chosen:
            if req.get("dm_channel"):
                try:
                    self.bot.chat_postEphemeral(channel=req["dm_channel"], user=self.owner_id,
                                                text="Marca al menos un seguimiento.")
                except Exception as exc:
                    log.error("next notice failed for %s: %s", rid, error_code(exc))
            return
        self.on_next(user_id, rid, indexes=chosen)

    def _to_revise(self, rid: str, text: str, via: Optional[str] = None, src: Optional[str] = None,
                   extra: Optional[Dict[str, Any]] = None) -> None:
        """Move a shown or noted card to `revise`. The transition wins once, so a second call does nothing."""
        req = db.get(self.conn, rid)
        res: Dict[str, Any] = {"instructions": text}
        if via:
            res["via"] = via
        elif req and req["kind"] == "questions":
            res["instructions"] = text + blocks.given_answers_tail(req)
        if extra:
            res.update(extra)
        stored = dict(res, src=src) if src else res
        fields = dict(result_json=json.dumps(stored, ensure_ascii=False), decided_at=db.now_iso(), dm_done=0)
        if (db.transition(self.conn, rid, "shown", "revise", **fields)
                or db.transition(self.conn, rid, "noted", "revise", **fields)):
            self.refresh_dm(rid)
            self._orphan(rid, res["instructions"])
        else:
            self._stale(rid, src)

    def _orphan(self, rid: str, text: str) -> None:
        """The owner asked for changes on a card that names no session: his text starts a task, so it is not lost."""
        req = db.get(self.conn, rid)
        if not req or card_has_reader(req):
            return
        try:
            if self.tasks.orphan_card(req, text) and req.get("dm_ts") and self.tasks.notices_on():  # no dm_ts: orphan_card posted the notice
                self.tasks.say(req.get("dm_channel") or OWNER_DM, req.get("dm_ts"),
                               self.tasks.orphan_said(req))
        except Exception:
            log.exception("card %s has no session and its task did not start", rid)

    def on_choose(self, user_id: str, rid: str, key: str, src: Optional[str] = None,
                  and_continue: bool = False, respond: Optional[Dict[str, Any]] = None) -> None:
        """`respond` (web «Responder» box only): {"respond_summary", "respond_note"}, stored with the result."""
        if not self.is_owner(user_id):
            return
        req = db.get(self.conn, rid)
        if not req or req["kind"] not in ("decision", "report"):
            return
        if req["kind"] == "report":
            allowed = ["ok"] + (["resume"] if (req.get("report") or {}).get("resume") else [])
        else:
            allowed = [o["key"] for o in req["options"]]
        if key not in allowed:
            return
        fields = dict(result_json=_res(src, option=key, **(respond or {})), decided_at=db.now_iso())
        if and_continue and blocks.can_continue(req) and req["kind"] == "decision":
            fields["and_continue"] = 1
        if (db.transition(self.conn, rid, "shown", "chosen", **fields)
                or (req["kind"] == "report" and db.transition(self.conn, rid, "noted", "chosen", **fields))):
            if req["kind"] == "report" and key == "resume":
                # The shown->chosen step wins once, so a second click starts nothing twice.
                self.tasks.resume_session(req["report"]["resume"])
            if key == "agent" and req.get("agent_where"):
                # The shown->chosen step wins once, so a second click makes no second task.
                channel, thread = req.get("dm_channel") or OWNER_DM, req.get("dm_ts")
                msg_ts = None
                if not thread:
                    if self.tasks.notices_on():  # web mode: the notice that starts the task is the thread root
                        thread = self.tasks.say(channel, None, "🧭 Abro un agente: %s" % blocks.clip(req["title"], 100))
                    else:  # quiet mode: no notice, so no thread
                        thread, msg_ts = "", "agent:" + rid
                if thread or msg_ts:
                    self.tasks.create_agent_task(channel, thread,
                                                 req["draft_text"] or req["title"], req["agent_where"],
                                                 msg_ts=msg_ts)
            self.refresh_dm(rid)
        else:
            self._stale(rid, src)

    def on_edit_click(self, user_id: str, rid: str, trigger_id: str) -> None:
        if not self.is_owner(user_id):
            return
        req = db.get(self.conn, rid)
        if not req or req["kind"] not in db.REPLY_KINDS:
            return
        if req["status"] != "shown":
            return self._stale(rid)
        self._open_modal(req, trigger_id, blocks.edit_modal(req), "edit")

    def on_edit_submit(self, user_id: str, view: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a Slack error response, or None when the edit is saved."""
        if not self.is_owner(user_id):
            return None
        text = blocks.modal_text(view).strip()
        if not text:
            return {"response_action": "errors", "errors": {"text_block": "El texto no puede estar vacío."}}
        if len(text) > db.MAX_REPLY_CHARS:
            return {"response_action": "errors", "errors": {
                "text_block": f"Máximo {db.MAX_REPLY_CHARS} caracteres."}}
        rid = view["private_metadata"]
        if db.set_final_text(self.conn, rid, text):
            self.refresh_dm(rid)
        return None

    # -- questions ---------------------------------------------------------
    def _open_questions(self, rid: str) -> Optional[Dict[str, Any]]:
        req = db.get(self.conn, rid)
        if not req or req["kind"] != "questions" or req["status"] != "shown":
            return None
        return req

    def _save_answers(self, req: Dict[str, Any], answers: Dict[str, Any],
                      note: Optional[str] = None) -> None:
        res = dict(req.get("result") or {})
        res["answers"] = answers
        if note is not None:
            res["note"] = note
        db.set_result(self.conn, req["id"], res)

    def on_q_answer(self, user_id: str, rid: str, qkey: str, okey: str) -> None:
        if not self.is_owner(user_id):
            return
        with self._qlock:
            req = self._open_questions(rid)
            q = next((q for q in req["options"] if q["key"] == qkey), None) if req else None
            opt = next((o for o in q["options"] if o["key"] == okey), None) if q else None
            if opt:
                answers = dict(blocks.answers_of(req))
                answers[qkey] = {"option": okey, "text": opt["label"]}
                self._save_answers(req, answers)
        if req is None:
            return self._stale(rid)
        if opt:
            self.refresh_dm(rid)

    def on_q_reset(self, user_id: str, rid: str, qkey: str) -> None:
        if not self.is_owner(user_id):
            return
        with self._qlock:
            req = self._open_questions(rid)
            if req:
                answers = dict(blocks.answers_of(req))
                if answers.pop(qkey, None) is None:
                    return
                self._save_answers(req, answers)
        if not req:
            return self._stale(rid)
        self.refresh_dm(rid)

    def on_q_other_click(self, user_id: str, rid: str, qkey: str, trigger_id: str) -> None:
        if not self.is_owner(user_id):
            return
        req = self._open_questions(rid)
        if not req:
            return self._stale(rid)
        if any(q["key"] == qkey for q in req["options"]):
            self._open_modal(req, trigger_id, blocks.answer_modal(req, qkey), "qother", qkey)

    def on_q_note_click(self, user_id: str, rid: str, trigger_id: str) -> None:
        if not self.is_owner(user_id):
            return
        req = self._open_questions(rid)
        if not req:
            return self._stale(rid)
        self._open_modal(req, trigger_id, blocks.note_modal(req), "qnote")

    def on_q_submit(self, user_id: str, view: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Save a typed answer or the note. Return a Slack error response, or None."""
        if not self.is_owner(user_id):
            return None
        try:
            meta = json.loads(view["private_metadata"])
        except (KeyError, ValueError):
            return None
        rid, qkey = meta.get("id"), meta.get("q")
        text = blocks.modal_text(view).strip()
        if qkey and not text:
            return {"response_action": "errors", "errors": {"text_block": "Escribe una respuesta."}}
        if len(text) > db.MAX_REPLY_CHARS:
            return {"response_action": "errors", "errors": {
                "text_block": f"Máximo {db.MAX_REPLY_CHARS} caracteres."}}
        with self._qlock:
            req = self._open_questions(rid)
            if req:
                if qkey:
                    if not any(q["key"] == qkey for q in req["options"]):
                        return None
                    answers = dict(blocks.answers_of(req))
                    answers[qkey] = {"option": None, "text": text}
                    self._save_answers(req, answers)
                else:
                    self._save_answers(req, dict(blocks.answers_of(req)), note=text)
        if not req:
            self._stale(rid)
            return None
        self.refresh_dm(rid)
        return None

    def on_q_accept(self, user_id: str, rid: str) -> None:
        if not self.is_owner(user_id):
            return
        with self._qlock:
            req = self._open_questions(rid)
            if req:
                answers = dict(blocks.answers_of(req))
                for q in req["options"]:
                    if q["key"] not in answers and q.get("recommended"):
                        label = next(o["label"] for o in q["options"] if o["key"] == q["recommended"])
                        answers[q["key"]] = {"option": q["recommended"], "text": label}
                self._save_answers(req, answers)
        if not req:
            return self._stale(rid)
        self.refresh_dm(rid)

    def on_q_send(self, user_id: str, rid: str, src: Optional[str] = None,
                  respond: Optional[Dict[str, Any]] = None) -> None:
        if not self.is_owner(user_id):
            return
        lost = False
        with self._qlock:
            req = self._open_questions(rid)
            if not req:
                return self._stale(rid, src)
            gap = blocks.missing(req)
            if gap:
                hint = "Faltan: " + ", ".join(str(n) for n in gap)
            else:
                res = dict(req.get("result") or {})
                final = {"answers": res.get("answers", {}), "note": res.get("note") or ""}
                if src:
                    final["src"] = src
                final.update(respond or {})
                if not db.transition(self.conn, rid, "shown", "answered",
                                     result_json=json.dumps(final, ensure_ascii=False),
                                     decided_at=db.now_iso()):
                    lost = True
                hint = None
        if lost:
            return self._stale(rid, src)
        self.refresh_dm(rid, hint)

    def recover(self) -> None:
        """After a crash, a request may be stuck in "sending". We cannot know if the post
        happened, so we mark it failed and let the owner check the channel."""
        for r in self.conn.execute("SELECT id FROM requests WHERE status='sending'").fetchall():
            db.transition(self.conn, r["id"], "sending", "failed",
                          result_json=json.dumps({"error": "interrupted: revisa el canal antes de reintentar"}),
                          decided_at=db.now_iso())
            alarm.alarm("polar:send-failed", "Polar: un envío quedó interrumpido", "%s: revisa el canal" % r["id"])
        # A report in flight from an older version shows «Visto». Move it to noted and redraw it.
        for r in self.conn.execute(
                "SELECT id FROM requests WHERE status='shown' AND kind='report'").fetchall():
            try:
                if db.transition(self.conn, r["id"], "shown", "noted"):
                    self.refresh_dm(r["id"])
            except Exception as exc:
                log.error("report migration failed for %s: %s", r["id"], error_code(exc))
