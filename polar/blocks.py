"""Block Kit builders. The text in Slack is Spanish."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from polar import db

# Workspace URL for message permalinks, e.g. https://myteam.slack.com (POLAR_SLACK_URL).
SLACK_URL = (os.environ.get("POLAR_SLACK_URL") or "https://example.slack.com").rstrip("/")

SECTION_LIMIT = 2900  # Slack allows 3000 characters in one text object


def clip(text: str, limit: int = SECTION_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quote(text: str) -> str:
    return "\n".join("> " + line for line in clip(text, SECTION_LIMIT - 200).splitlines() or [""])


def reply_text(req: Dict[str, Any]) -> str:
    return req.get("final_text") or req.get("draft_text") or ""


def mx_hhmm(ts: Optional[str]) -> str:
    """Slack ts to HH:MM in Mexico City time."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(float(ts), ZoneInfo("America/Mexico_City")).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def icon(req: Dict[str, Any]) -> str:
    who = (req.get("requester") or "").strip()
    if req["kind"] == "report":
        return "📋"
    if who.startswith("watchdog"):
        return "🚨"
    if who.startswith("release-desk"):
        return "🚀"
    if (req.get("title") or "").startswith("Paso pendiente"):
        return "⚙️"
    if req["kind"] == "questions":
        return "📝"
    if req.get("agent_where"):
        return "🤖"
    if req["kind"] == "email":
        return "✉️"
    return "💬" if req["kind"] == "reply" else "✅"


def where(req: Dict[str, Any]) -> str:
    if req["kind"] == "email":
        return "correo"
    if (req.get("target_channel") or "").startswith("D"):
        return "DM"
    name = req.get("orig_channel_name")
    return "#" + name if name else "canal"


def header_text(req: Dict[str, Any]) -> str:
    if req["kind"] in db.REPLY_KINDS and req.get("orig_name"):
        return clip(f"{icon(req)} Respuesta a {req['orig_name']} · {where(req)}", 150)
    if req["kind"] == "questions":
        return clip(f"📝 Preguntas · {req['title']}", 150)
    return clip(f"{icon(req)} {req['title']}", 150)


def session_label(req: Dict[str, Any]) -> str:
    return (req.get("requester_label") or "").strip() or "Sesión sin nombre"


BODY_SECTION_CHARS = 2800
BODY_SECTIONS = 3


def body_sections(text: str) -> List[str]:
    """Split a long Markdown body into at most 3 parts of about 2,800 characters. Cut with «…»."""
    text = (text or "").strip()
    parts: List[str] = []
    while text and len(parts) < BODY_SECTIONS:
        if len(text) <= BODY_SECTION_CHARS:
            parts.append(text)
            text = ""
            break
        cut = text.rfind("\n", 0, BODY_SECTION_CHARS)
        if cut < BODY_SECTION_CHARS // 2:
            cut = BODY_SECTION_CHARS
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        parts[-1] = parts[-1][: BODY_SECTION_CHARS - 1].rstrip() + "…"
    return parts


def extra_blocks(req: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Body («📄 Qué es») and links of a decision or questions card."""
    ex = req.get("extra") or {}
    out: List[Dict[str, Any]] = []
    if ex.get("body"):
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📄 Qué es*"}})
        out += [{"type": "markdown", "text": p} for p in body_sections(ex["body"])]
    if ex.get("links"):
        line = "  ·  ".join("<%s|%s>" % (ln["url"], ln["label"].replace("|", "/").replace(">", "")) for ln in ex["links"])
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": clip("🔗 " + line, 2900)}]})
    return out


# Slack refuses a card whose blocks are too long in total (msg_blocks_too_long). The size depends on the
# text (tables count more), so Polar starts with the longest body and steps down until Slack accepts.
BODY_LIMITS = (11000, 9000, 6000, 3000)


def body_level(req: Dict[str, Any]) -> int:
    """The body step of this card: an index into BODY_LIMITS (the `compact` column)."""
    try:
        n = int(req.get("compact") or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(n, len(BODY_LIMITS) - 1))


def body_text(req: Dict[str, Any]) -> str:
    """The long text of a card. Only a report has one: a reply is short (db.MAX_REPLY_CHARS)."""
    return (req.get("draft_text") or "") if req["kind"] == "report" else ""


def next_level(req: Dict[str, Any], level: int) -> Optional[int]:
    """The next step that makes the body of this card shorter. None when no step does."""
    size = min(len(body_text(req)), BODY_LIMITS[level])
    for n in range(level + 1, len(BODY_LIMITS)):
        if BODY_LIMITS[n] < size:
            return n
    return None


def _head(req: Dict[str, Any]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text(req)}},
    ]
    if req["kind"] in db.REPLY_KINDS or req.get("agent_where"):
        if req["kind"] == "email" and req.get("email_subject"):
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                       "text": "*Asunto:* " + clip(req["email_subject"], 300)}})
        if req.get("orig_text"):
            link = f" <{req['orig_permalink']}|Ver hilo ↗>" if req.get("orig_permalink") else ""
            when = f" ({req['orig_time']})" if req.get("orig_time") else ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": quote(
                f"{req.get('orig_name') or 'Alguien'} escribió{when}: \"{req['orig_text']}\"") + link}})
    if req["kind"] == "report":
        body, limit = body_text(req), BODY_LIMITS[body_level(req)]
        blocks.append({"type": "markdown", "text": clip(body, limit)})
        if len(body) > limit and report_web_url(req["id"]):
            blocks.append({"type": "context", "elements": [{
                "type": "mrkdwn", "text": "✂️ Aquí va solo una parte. El texto entero está en «📄 Informe completo»."}]})
    if req["kind"] in db.REPLY_KINDS:
        if req.get("email_cc"):
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": "Cc: " + clip(req["email_cc"], 300)}]})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Tu respuesta:*"}})
        # Same renderer as the send (markdown_text), so the preview matches what the recipient will see.
        blocks.append({"type": "markdown", "text": clip(reply_text(req), 11000)})
    if req.get("context"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": "*Por qué:* " + clip(req["context"])}})
    blocks += extra_blocks(req)
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": "🧵 Sesión: " + clip(session_label(req), 200)}]})
    if req.get("replaces"):
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "↪️ Reemplaza una tarjeta anterior"}]})
    return blocks


def answers_of(req: Dict[str, Any]) -> Dict[str, Any]:
    return (req.get("result") or {}).get("answers") or {}


def given_answers_tail(req: Dict[str, Any]) -> str:
    """Text for the end of «Pedir cambios» on a questions card: the answers and the note the owner already gave."""
    have = answers_of(req)
    parts = ["%d = %s" % (i, answer_text(have[q["key"]]))
             for i, q in enumerate(req.get("options") or [], 1) if q["key"] in have]
    note = ((req.get("result") or {}).get("note") or "").strip()
    if not parts and not note:
        return ""
    tail = "\n\nRespuestas ya dadas: " + "; ".join(parts) + "." if parts else "\n\nRespuestas ya dadas: ninguna."
    if note:
        tail += " Nota: " + note
    return tail


def missing(req: Dict[str, Any]) -> List[int]:
    """The numbers (from 1) of the questions with no answer."""
    have = answers_of(req)
    return [i for i, q in enumerate(req["options"], 1) if q["key"] not in have]


def answer_text(ans: Dict[str, Any]) -> str:
    return (ans.get("text") or "").strip()


def questions_blocks(req: Dict[str, Any], hint: Optional[str] = None) -> List[Dict[str, Any]]:
    blocks = _head(req)
    rid, have = req["id"], answers_of(req)
    for i, q in enumerate(req["options"], 1):
        if q["key"] in have:
            blocks.append({
                "type": "section", "block_id": "polar_q_" + q["key"],
                "text": {"type": "mrkdwn", "text": clip(f"*{i}.* {q['text']} → *{answer_text(have[q['key']])}*")},
                "accessory": {"type": "button", "action_id": "polar_qreset", "value": f"{rid}:{q['key']}",
                              "text": {"type": "plain_text", "text": "Cambiar"}},
            })
            continue
        blocks.append({"type": "section", "block_id": "polar_q_" + q["key"],
                       "text": {"type": "mrkdwn", "text": clip(f"*{i}.* {q['text']}")}})
        els: List[Dict[str, Any]] = []
        for o in q.get("options", []):
            rec = q.get("recommended") == o["key"]
            btn: Dict[str, Any] = {
                "type": "button", "action_id": f"polar_qa:{q['key']}:{o['key']}", "value": rid,
                "text": {"type": "plain_text", "text": clip(o["label"] + (" (recomendado)" if rec else ""), 70)}}
            if rec:
                btn["style"] = "primary"
            els.append(btn)
        els.append({"type": "button", "action_id": "polar_qother", "value": f"{rid}:{q['key']}",
                    "text": {"type": "plain_text", "text": "Otra…"}})
        blocks.append({"type": "actions", "block_id": "polar_qrow_" + q["key"], "elements": els})
    if hint:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "⚠️ " + hint}]})
    bottom: List[Dict[str, Any]] = []
    if any(q["key"] not in have and q.get("recommended") for q in req["options"]):
        bottom.append({"type": "button", "action_id": "polar_qaccept", "value": rid,
                       "text": {"type": "plain_text", "text": "✅ Aceptar lo recomendado"}})
    note = (req.get("result") or {}).get("note")
    bottom.append({"type": "button", "action_id": "polar_qnote", "value": rid,
                   "text": {"type": "plain_text", "text": "📝 Editar nota" if note else "📝 Agregar nota"}})
    bottom.append({"type": "button", "style": "primary", "action_id": "polar_qsend", "value": rid,
                   "text": {"type": "plain_text", "text": "Enviar respuestas"}})
    bottom.append(revise_button(rid))
    if note:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": clip("*Nota:* " + note)}})
    blocks.append({"type": "actions", "block_id": "polar_actions", "elements": bottom})
    return blocks


def revise_button(rid: str) -> Dict[str, Any]:
    return {"type": "button", "action_id": "polar_revise", "value": rid,
            "text": {"type": "plain_text", "text": "🔁 Pedir cambios"}}


def report_web_url(rid: str) -> Optional[str]:
    """The page of a report on the web front end, when POLAR_WEB_URL is set."""
    base = (os.environ.get("POLAR_WEB_URL") or "").strip().rstrip("/")
    return "%s/informes/%s" % (base, rid) if base else None


def report_buttons(req: Dict[str, Any]) -> List[Dict[str, Any]]:
    rid, rep = req["id"], req.get("report") or {}
    els: List[Dict[str, Any]] = []
    if rep.get("resume"):
        els.append({"type": "button", "style": "primary", "action_id": "polar_choose:resume", "value": rid,
                    "text": {"type": "plain_text", "text": "▶️ Reanudar"}})
    for i, ln in enumerate((rep.get("links") or [])[:db.MAX_REPORT_LINKS]):
        els.append({"type": "button", "action_id": "polar_link:%d" % i, "url": ln["url"],
                    "text": {"type": "plain_text", "text": clip(ln["label"], 70)}})
    web = report_web_url(rid)
    if web:
        els.append({"type": "button", "action_id": "polar_link:web", "url": web,
                    "text": {"type": "plain_text", "text": "📄 Informe completo"}})
    steps = (rep.get("next") or [])[:db.MAX_REPORT_NEXT]
    if len(steps) == 1:
        els.append({"type": "button", "action_id": "polar_next:0", "value": rid,
                    "text": {"type": "plain_text", "text": clip("➡️ " + steps[0], 75)}})
    elif steps:  # 2 or 3: the owner checks some in `next_select` and presses this button
        els.append({"type": "button", "style": "primary", "action_id": "polar_next_go", "value": rid,
                    "text": {"type": "plain_text", "text": "▶️ Empezar marcados"}})
    els.append(revise_button(rid))
    return els  # at most 11 elements; Slack allows 25


def next_block_id(rid: str) -> str:
    return "polar_nextsel:" + rid


def next_select(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The checkboxes of a report with 2 or 3 follow-ups (an `actions` block); None for 0 or 1."""
    steps = ((req.get("report") or {}).get("next") or [])[:db.MAX_REPORT_NEXT]
    if len(steps) < 2:
        return None
    opts = [{"value": str(i), "text": {"type": "plain_text", "text": clip(step, 75)}}
            for i, step in enumerate(steps)]
    return {"type": "actions", "block_id": next_block_id(req["id"]),
            "elements": [{"type": "checkboxes", "action_id": "polar_next_check", "options": opts}]}


def next_checked(state: Dict[str, Any], rid: str) -> List[int]:
    """The follow-up numbers checked on this card, read from a Slack block_actions `state` object."""
    val = ((state.get("values") or {}).get(next_block_id(rid)) or {}).get("polar_next_check") or {}
    out = set()
    for o in val.get("selected_options") or []:
        try:
            out.add(int(o["value"]))
        except (KeyError, ValueError, TypeError):
            pass
    return sorted(out)


CONTINUE_LABEL = "Sí, y sigue"


def can_continue(req: Dict[str, Any]) -> bool:
    """«Sí, y sigue» is offered on a decision card, never on a prod-op step. It is bound to the first option
    (a decision card has no recommended option)."""
    return (req.get("kind") == "decision" and bool(req.get("options"))
            and not (req.get("requester") or "").startswith("prod-op"))


def request_blocks(req: Dict[str, Any], hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """The message with buttons."""
    if req["kind"] == "questions":
        return questions_blocks(req, hint)
    blocks = _head(req)
    rid = req["id"]
    if req["kind"] == "report":
        sel = next_select(req)
        return blocks + ([sel] if sel else []) + [
            {"type": "actions", "block_id": "polar_actions", "elements": report_buttons(req)}]
    if req["kind"] in db.REPLY_KINDS:
        elements = [
            {"type": "button", "style": "primary", "action_id": "polar_send", "value": rid,
             "text": {"type": "plain_text", "text": "Enviar"}},
            {"type": "button", "action_id": "polar_edit", "value": rid,
             "text": {"type": "plain_text", "text": "Editar"}},
            {"type": "button", "action_id": "polar_discard", "value": rid,
             "text": {"type": "plain_text", "text": "Descartar"}},
        ]
    else:
        elements = [
            {"type": "button", "action_id": "polar_choose:" + o["key"], "value": rid,
             "text": {"type": "plain_text", "text": clip(o["label"], 70)}}
            for o in req["options"]
        ]
        if can_continue(req):
            elements.append({"type": "button", "action_id": "polar_choose_go:" + req["options"][0]["key"],
                             "value": rid, "text": {"type": "plain_text", "text": CONTINUE_LABEL}})
    elements.append(revise_button(rid))
    blocks.append({"type": "actions", "block_id": "polar_actions", "elements": elements})
    return blocks


def option_label(req: Dict[str, Any], key: str) -> str:
    for o in req.get("options", []):
        if o["key"] == key:
            return o["label"]
    return key


def result_line(req: Dict[str, Any]) -> str:
    """One line for a decided card. «· desde la web» when the web API took the decision."""
    line = _result_line(req)
    if line and (req.get("result") or {}).get("src") == "web":
        line += " · desde la web"
    return line


def _result_line(req: Dict[str, Any]) -> str:
    """One line for a decided card."""
    status, res = req["status"], req.get("result") or {}
    if status == "sent":
        to = req.get("orig_name") or (f"#{req['orig_channel_name']}" if req.get("orig_channel_name") else (
            "su correo" if req["kind"] == "email" else "su canal"))
        link = res.get("permalink")
        edited = bool(req.get("final_text")) and req.get("final_text") != req.get("draft_text")
        lead = "✏️ Editado y enviado a " if edited else "✅ Enviado a "
        return lead + to + (f" · <{link}|ver ↗>" if link else "")
    if status == "chosen":
        if res.get("option") == "agent" and req.get("agent_where"):
            return "🚀 Abrí un agente: " + clip(req["title"], 150)
        if req["kind"] == "report":
            if res.get("option") == "resume":
                return "▶️ Reanudé la sesión — " + clip(req["title"], 150)
            return "📋 Visto — " + clip(req["title"], 150)
        if req.get("and_continue"):
            return "👉 Elegiste «%s» · y sigue" % option_label(req, res.get("option", ""))
        line = "👉 Elegiste: " + option_label(req, res.get("option", ""))
        if len(req["title"]) <= 60:
            line += " — " + req["title"]
        return line
    if status == "answered":
        n = len(req["options"])
        return f"📝 Respondiste {n} pregunta{'s' if n != 1 else ''} · {req['title']}"
    if status == "discarded":
        return "🗑️ Descartado"
    if status == "revise":
        if res.get("via") == "next":
            steps = res.get("steps") or []
            if len(steps) > 1:
                return "➡️ Seguimientos: " + clip(" · ".join(steps), 200)
            return "➡️ Seguimiento: " + clip(res.get("instructions", ""), 200)
        return "🔁 Pediste cambios: " + clip(res.get("instructions", ""), 200)
    if status == "replaced":
        return "↪️ Reemplazada por una tarjeta nueva"
    if status == "expired":
        return "⌛ Expiró"
    if status == "skipped":
        return f"⏭️ Saltada · respondiste otras más nuevas — {clip(req['title'], 150)}"
    if status == "filed":
        return "📋 Informe — " + clip(req["title"], 150)
    if status == "failed":
        return "⚠️ Falló: " + clip(str(res.get("error", "error")), 200)
    return ""


def resolved_blocks(req: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A decided card is one line. A skipped card also has «Reabrir»."""
    if req["status"] == "skipped":
        return [{"type": "section", "text": {"type": "mrkdwn", "text": clip(result_line(req))},
                 "accessory": {"type": "button", "action_id": "polar_reopen", "value": req["id"],
                               "text": {"type": "plain_text", "text": "Reabrir"}}}]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": clip(result_line(req))}}]


def pending_age(created: str, now: datetime) -> str:
    try:
        then = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""
    mins = max(0, int((now - then).total_seconds() // 60))
    return "hace %d min" % mins if mins < 60 else "hace %d h" % (mins // 60)


def pending_text(n: int) -> str:
    if n == 1:
        return "⏳ *Pendientes: 1 tarjeta espera tu respuesta*"
    return "⏳ *Pendientes: %d tarjetas esperan tu respuesta*" % n


def pending_blocks(reqs: List[Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """One message that lists the cards the owner passed over. No buttons."""
    now = now or datetime.utcnow()
    lines = [pending_text(len(reqs))]
    for r in reqs[:15]:
        title = clip(_link_text(" ".join((r["title"] or "").split())), 80)
        if r.get("dm_channel") and r.get("dm_ts"):
            title = "<" + SLACK_URL + "/archives/%s/p%s|%s>" % (
                r["dm_channel"], r["dm_ts"].replace(".", ""), title)
        lines.append("• %s · %s" % (title, pending_age(r.get("reopened_at") or r["created_at"], now)))
    if len(reqs) > 15:
        lines.append("… y %d más" % (len(reqs) - 15))
    return [{"type": "section", "text": {"type": "mrkdwn", "text": clip("\n".join(lines))}}]


def fallback_text(req: Dict[str, Any]) -> str:
    if req["status"] in ("pending", "shown", "sending"):
        return clip(req["title"], 150)
    return clip(result_line(req), 150) or clip(req["title"], 150)


def edit_modal(req: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "polar_edit_submit",
        "private_metadata": req["id"],
        "title": {"type": "plain_text", "text": "Editar respuesta"},
        "submit": {"type": "plain_text", "text": "Guardar"},
        "close": {"type": "plain_text", "text": "Cancelar"},
        "blocks": [{
            "type": "input", "block_id": "text_block",
            "label": {"type": "plain_text", "text": "Texto"},
            "element": {
                "type": "plain_text_input", "action_id": "text", "multiline": True,
                "initial_value": reply_text(req)[:3000],
            },
        }],
    }


def revise_modal(req: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "polar_revise_submit",
        "private_metadata": req["id"],
        "title": {"type": "plain_text", "text": "Pedir cambios"},
        "submit": {"type": "plain_text", "text": "Enviar"},
        "close": {"type": "plain_text", "text": "Cancelar"},
        "blocks": [{
            "type": "input", "block_id": "text_block",
            "label": {"type": "plain_text", "text": "¿Qué cambio quieres?"},
            "element": {"type": "plain_text_input", "action_id": "text", "multiline": True},
        }],
    }


def modal_text(view: Dict[str, Any]) -> str:
    return view["state"]["values"]["text_block"]["text"]["value"] or ""


def text_view(text: str, private_metadata: str) -> Dict[str, Any]:
    """A modal `view` as Slack sends it on submit, built from a text typed in a thread."""
    return {"private_metadata": private_metadata,
            "state": {"values": {"text_block": {"text": {"value": text}}}}}


def answer_modal(req: Dict[str, Any], qkey: str) -> Dict[str, Any]:
    """Modal for a typed answer to one question."""
    n, q = next((i, q) for i, q in enumerate(req["options"], 1) if q["key"] == qkey)
    old = answers_of(req).get(qkey) or {}
    return {
        "type": "modal", "callback_id": "polar_q_submit",
        "private_metadata": json.dumps({"id": req["id"], "q": qkey}),
        "title": {"type": "plain_text", "text": f"Pregunta {n}"},
        "submit": {"type": "plain_text", "text": "Guardar"},
        "close": {"type": "plain_text", "text": "Cancelar"},
        "blocks": [{
            "type": "input", "block_id": "text_block",
            "label": {"type": "plain_text", "text": clip(q["text"], 2000)},
            "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                        **({"initial_value": answer_text(old)[:3000]} if old else {})},
        }],
    }


def note_modal(req: Dict[str, Any]) -> Dict[str, Any]:
    note = (req.get("result") or {}).get("note") or ""
    return {
        "type": "modal", "callback_id": "polar_q_submit",
        "private_metadata": json.dumps({"id": req["id"], "q": None}),
        "title": {"type": "plain_text", "text": "Nota"},
        "submit": {"type": "plain_text", "text": "Guardar"},
        "close": {"type": "plain_text", "text": "Cancelar"},
        "blocks": [{
            "type": "input", "block_id": "text_block", "optional": True,
            "label": {"type": "plain_text", "text": "Contexto extra para la sesión"},
            "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                        **({"initial_value": note[:3000]} if note else {})},
        }],
    }


# -- task pick card ------------------------------------------------------------
PICK_CHUNK = 10  # Slack allows 10 options in one checkboxes element


def _opt(i: int, item: str) -> Dict[str, Any]:
    return {"text": {"type": "plain_text", "text": clip(" ".join(item.split()), 75)}, "value": str(i)}


def pick_blocks(pick: Dict[str, Any], selected: Optional[List[int]] = None,
                hint: Optional[str] = None, intro: Optional[str] = None) -> List[Dict[str, Any]]:
    if pick.get("plan"):
        return plan_blocks(pick, selected, hint)
    items = pick["items"]
    out: List[Dict[str, Any]] = [{"type": "section", "text": {
        "type": "mrkdwn", "text": "🧭 *Encontré %d tareas.* ¿Cuáles arranco?" % len(items)}}]
    if intro:
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": intro}]})
    chosen = set(selected or [])
    for ci, start in enumerate(range(0, len(items), PICK_CHUNK)):
        opts = [_opt(i, it) for i, it in enumerate(items[start:start + PICK_CHUNK], start)]
        el: Dict[str, Any] = {"type": "checkboxes", "action_id": "polar_pick_check:%d" % ci, "options": opts}
        init = [o for o in opts if int(o["value"]) in chosen]
        if init:
            el["initial_options"] = init
        out.append({"type": "actions", "block_id": "pick_%d" % ci, "elements": [el]})
    if hint:
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": hint}]})
    out.append({"type": "actions", "block_id": "pick_go", "elements": [{
        "type": "button", "action_id": "polar_pick_go", "style": "primary", "value": pick["id"],
        "text": {"type": "plain_text", "text": "Arrancar las marcadas"}}]})
    return out


PLAN_GROUPS = (("now", "▶️ Recomiendo empezar"), ("running", "🔄 Ya en marcha"), ("later", "🕒 Después"),
               ("done", "✅ Ya hecho"), ("drop", "🗑️ Descartar"))


def plan_blocks(pick: Dict[str, Any], selected: Optional[List[int]] = None,
                hint: Optional[str] = None) -> List[Dict[str, Any]]:
    """The planned card: one section for each non-empty group, checkboxes for its items.
    `selected=None` uses the `checked` flag of the plan; a list overrides it."""
    plan = pick["plan"]
    chosen = None if selected is None else set(selected)
    head = "🧭 *Ordené tus %d tareas.* Marca las que arranco." % len(pick["items"])
    if plan.get("summary"):
        head += "\n" + plan["summary"]
    out: List[Dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": clip(head)}}]
    n = 0
    for key, title in PLAN_GROUPS:
        group = next((g for g in plan["groups"] if g["key"] == key), None)
        if not group or not group["items"]:
            continue
        lines = []
        for it in group["items"]:
            line = "• *%s* — %s" % (it["title"], it["reason"])
            if it.get("label"):
                line += " _(%s)_" % it["label"]
            lines.append(line)
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": clip("*%s*\n%s" % (title, "\n".join(lines)))}})
        for start in range(0, len(group["items"]), PICK_CHUNK):
            part = group["items"][start:start + PICK_CHUNK]
            opts = []
            for it in part:
                o: Dict[str, Any] = {"text": {"type": "plain_text", "text": clip(it["title"], 75)},
                                     "value": str(it["i"])}
                desc = it.get("label") or it["reason"]
                if desc:
                    o["description"] = {"type": "plain_text", "text": clip(desc, 75)}
                opts.append(o)
            el: Dict[str, Any] = {"type": "checkboxes", "action_id": "polar_pick_check:%d" % n, "options": opts}
            init = [o for o, it in zip(opts, part)
                    if (it["i"] in chosen if chosen is not None else it.get("checked"))]
            if init:
                el["initial_options"] = init
            out.append({"type": "actions", "block_id": "pick_%d" % n, "elements": [el]})
            n += 1
    if hint:
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": hint}]})
    out.append({"type": "actions", "block_id": "pick_go", "elements": [
        {"type": "button", "action_id": "polar_pick_go", "style": "primary", "value": pick["id"],
         "text": {"type": "plain_text", "text": "Arrancar las marcadas"}},
        {"type": "button", "action_id": "polar_pick_orig", "value": pick["id"],
         "text": {"type": "plain_text", "text": "Ver lista original"}}]})
    return out


def picked_blocks(pick: Dict[str, Any], chosen: List[int]) -> List[Dict[str, Any]]:
    lines = ["• " + clip(" ".join(pick["items"][i].split()), 200) for i in sorted(chosen)]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": clip(
        "✅ *Arranqué %d de %d tareas*\n%s" % (len(chosen), len(pick["items"]), "\n".join(lines)))}}]


def picked_indexes(state: Dict[str, Any]) -> List[int]:
    """The checked item numbers, read from a Slack block_actions `state` object."""
    out: List[int] = []
    for block in (state.get("values") or {}).values():
        for aid, val in block.items():
            if aid.startswith("polar_pick_check:"):
                for o in val.get("selected_options") or []:
                    try:
                        out.append(int(o["value"]))
                    except (KeyError, ValueError):
                        pass
    return sorted(set(out))


# -- task triage ----------------------------------------------------------------
def _btn(aid: str, tid: str, label: str, style: Optional[str] = None) -> Dict[str, Any]:
    b: Dict[str, Any] = {"type": "button", "action_id": aid, "value": tid,
                         "text": {"type": "plain_text", "text": label}}
    if style:
        b["style"] = style
    return b


def route_blocks(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": clip(
                "➡️ *Lo mandé a %s:* %s" % (t["match_label"], t["match_reason"]))}},
            {"type": "actions", "block_id": "route_" + t["id"],
             "elements": [_btn("polar_route_new", t["id"], "No, sesión nueva")]}]


def confirm_blocks(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": clip(
                "¿Es de *%s*? %s" % (t["match_label"], t["match_reason"]))}},
            {"type": "actions", "block_id": "route_" + t["id"], "elements": [
                _btn("polar_route_yes", t["id"], "Mandar a esa sesión", "primary"),
                _btn("polar_route_new", t["id"], "Sesión nueva")]}]


# -- one «Go» for a group of production steps
GROUP_MAX = 20


def _link_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "/")


def prod_group_text(n: int) -> str:
    return "Pasos de producción: %d esperan tu Go" % n


def prod_group_done_text(n: int) -> str:
    return "Diste Go a 1 paso de producción" if n == 1 else "Diste Go a %d pasos de producción" % n


def prod_group_blocks(reqs: List[Dict[str, Any]], gid: str) -> List[Dict[str, Any]]:
    shown = reqs[:GROUP_MAX]
    n = len(shown)
    lines = []
    for r in shown:
        title = clip(_link_text((r["title"] or "").replace("Producción: ", "", 1)), 90)
        url = SLACK_URL + "/archives/%s/p%s" % (r["dm_channel"], (r["dm_ts"] or "").replace(".", ""))
        lines.append("• <%s|%s>" % (url, title))
    if len(reqs) > n:
        lines.append("… y %d más en sus tarjetas" % (len(reqs) - n))
    out: List[Dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "🚀 *Pasos de producción: %d esperan tu Go*" % n}}]
    for i in range(0, len(lines), 10):  # one section holds at most 3000 characters
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": clip("\n".join(lines[i:i + 10]))}})
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                "Abre una tarjeta para ver su detalle, decir «No correr» o pedir cambios."}]})
    out.append({"type": "actions", "elements": [{
        "type": "button", "style": "primary", "action_id": "polar_group_go", "value": gid,
        "text": {"type": "plain_text", "text": "🚀 Correr los %d en producción" % n, "emoji": True},
        "confirm": {
            "title": {"type": "plain_text", "text": "¿Correr los %d pasos?" % n},
            "text": {"type": "plain_text", "text": "Das Go a los %d pasos de producción de la lista." % n},
            "confirm": {"type": "plain_text", "text": "Sí, correr"},
            "deny": {"type": "plain_text", "text": "Cancelar"}}}]})
    return out


def prod_group_done(n: int) -> List[Dict[str, Any]]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": "🚀 " + prod_group_done_text(n)}}]
