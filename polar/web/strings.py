"""Every string of the web UI, in English and Spanish. `t(key, **kw)` returns it in the active language.

POLAR_UI_LANG picks the language (default `en`); `polar demo --lang` overrides it with `set_lang`. The Slack cards
keep their own Spanish strings: this table is for the web UI only."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

EN: Dict[str, str] = {
    # layout and navigation
    "nav_pending": "Pending", "nav_history": "History", "nav_reports": "Reports", "nav_label": "Main",
    "skip": "Skip to content",
    "demo_banner": "Demo mode: sample data. Nothing is sent.",
    # kinds
    "kind_decision": "Decision", "kind_questions": "Questions", "kind_reply": "Reply", "kind_email": "Email",
    "kind_report": "Report",
    # statuses and outcomes
    "st_pending": "Waiting", "st_shown": "Waiting", "st_sending": "Sending", "st_noted": "Not seen",
    "st_sent": "Sent", "st_chosen": "Chosen", "st_discarded": "Discarded", "st_expired": "Expired",
    "st_failed": "Failed to send", "st_answered": "Answered", "st_skipped": "Skipped",
    "st_revise": "Changes requested", "st_replaced": "Replaced by a newer card", "st_filed": "Filed",
    "st_unseen": "Not seen",
    "did_chose": "Chose: {label}", "did_seen": "Seen", "did_resume": "Session resumed",
    "did_next": "Follow-up: {steps}", "did_revise": "Changes requested", "did_withdrawn": "Withdrawn by the requester",
    # time
    "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec", "weekdays": "Mon Tue Wed Thu Fri Sat Sun",
    "date_fmt": "{m} {d}, {hm}", "day_fmt": "{wd}, {m} {d}", "today": "Today", "yesterday": "Yesterday",
    "ago_now": "just now", "ago_min": "{n} min ago", "ago_h": "{n} h ago", "ago_d": "{n} d ago",
    # cards
    "no_requester": "Unnamed session", "recommended": "Recommended",
    "revise": "Request changes", "revise_label": "What should change?", "revise_send": "Send changes",
    "q_other": "Other answer", "q_answer": "Your answer", "q_note": "Note (optional)", "q_send": "Send answers",
    "no_answer": "(no answer)", "note": "Note:",
    "subject": "Subject: {s}", "open_original": "Open the original", "email_cc": "Cc: {cc}", "draft": "Draft",
    "send": "Send", "discard": "Discard",
    "send_off": "Sending from the web is off. Send it from Slack, or set POLAR_WEB_ALLOW=send.",
    "next_steps": "Next steps", "next_start": "Start selected", "seen": "Seen", "resume": "Resume the session",
    "full_report": "Full report", "back_reports": "All reports",
    "closed_note": "This card is closed. It shows what was decided.",
    "empty": "Nothing waits for you.",
    # history and reports
    "history_empty": "No decisions yet.", "newer": "Newer", "older": "Older", "pages": "Pages",
    "search": "Search", "search_hint": "Search the reports", "reports_empty": "No reports yet.",
    "reports_none_q": "No report matches this search.",
    # errors
    "not_found": "Not found.", "err_host": "Host not allowed.", "err_owner": "Not allowed.",
    "err_origin": "Origin not allowed.", "err_csrf": "The page is old. Reload it and try again.",
    "err_too_big": "The request is too large.", "err_json": "The request is not valid JSON.",
    "err_gate": "This action is off on the web.", "err_changed": "This card changed already ({status}).",
    "err_send": "It could not be sent. Check the card.", "err_bad": "The request is not valid.",
    "err_internal": "Internal error.",
    "v_id": "The card id is missing.", "v_action": "Unknown action.", "v_option": "Choose an option.",
    "v_answers": "Answer the questions first.", "v_text": "The text is empty.",
    "v_revise": "Write what should change.", "v_next": "Select 1 to 3 different next steps.",
    "v_note": "The note is too long.",
    # page script (static/app.js)
    "js_err_net": "No connection. Try again.", "js_viewer": "Photo viewer", "js_close": "Close",
    "js_prev": "Previous", "js_next": "Next", "js_open_tab": "Open in a new tab", "js_count": "{i} of {n}",
}

ES: Dict[str, str] = {
    "nav_pending": "Pendientes", "nav_history": "Historial", "nav_reports": "Informes", "nav_label": "Principal",
    "skip": "Ir al contenido",
    "demo_banner": "Modo demo: datos de ejemplo. No se envía nada.",
    "kind_decision": "Decisión", "kind_questions": "Preguntas", "kind_reply": "Respuesta", "kind_email": "Correo",
    "kind_report": "Informe",
    "st_pending": "En espera", "st_shown": "En espera", "st_sending": "Enviando", "st_noted": "Sin ver",
    "st_sent": "Enviado", "st_chosen": "Elegido", "st_discarded": "Descartado", "st_expired": "Venció",
    "st_failed": "No se pudo enviar", "st_answered": "Respondido", "st_skipped": "Omitido",
    "st_revise": "Cambios pedidos", "st_replaced": "La reemplazó una tarjeta nueva", "st_filed": "Archivado",
    "st_unseen": "Sin ver",
    "did_chose": "Elegiste: {label}", "did_seen": "Visto", "did_resume": "Sesión retomada",
    "did_next": "Seguimiento: {steps}", "did_revise": "Cambios pedidos", "did_withdrawn": "La retiró quien la pidió",
    "months": "ene feb mar abr may jun jul ago sep oct nov dic", "weekdays": "lun mar mié jue vie sáb dom",
    "date_fmt": "{d} {m}, {hm}", "day_fmt": "{wd} {d} {m}", "today": "Hoy", "yesterday": "Ayer",
    "ago_now": "ahora", "ago_min": "hace {n} min", "ago_h": "hace {n} h", "ago_d": "hace {n} d",
    "no_requester": "Sesión sin nombre", "recommended": "Recomendada",
    "revise": "Pedir cambios", "revise_label": "¿Qué debe cambiar?", "revise_send": "Enviar cambios",
    "q_other": "Otra respuesta", "q_answer": "Tu respuesta", "q_note": "Nota (opcional)",
    "q_send": "Enviar respuestas", "no_answer": "(sin respuesta)", "note": "Nota:",
    "subject": "Asunto: {s}", "open_original": "Abrir el original", "email_cc": "Cc: {cc}", "draft": "Borrador",
    "send": "Enviar", "discard": "Descartar",
    "send_off": "El envío desde la web está apagado. Envíalo desde Slack o pon POLAR_WEB_ALLOW=send.",
    "next_steps": "Siguientes pasos", "next_start": "Empezar marcados", "seen": "Visto",
    "resume": "Retomar la sesión", "full_report": "Informe completo", "back_reports": "Todos los informes",
    "closed_note": "Esta tarjeta está cerrada. Muestra lo que se decidió.",
    "empty": "No hay nada pendiente.",
    "history_empty": "Aún no hay decisiones.", "newer": "Más recientes", "older": "Más antiguas",
    "pages": "Páginas", "search": "Buscar", "search_hint": "Buscar en los informes",
    "reports_empty": "Aún no hay informes.", "reports_none_q": "Ningún informe coincide con la búsqueda.",
    "not_found": "No existe.", "err_host": "Host no permitido.", "err_owner": "No permitido.",
    "err_origin": "Origen no permitido.", "err_csrf": "La página es vieja. Recárgala y vuelve a intentar.",
    "err_too_big": "La petición es demasiado grande.", "err_json": "La petición no es JSON válido.",
    "err_gate": "Esta acción está apagada en la web.", "err_changed": "Esta tarjeta ya cambió ({status}).",
    "err_send": "No se pudo enviar. Revisa la tarjeta.", "err_bad": "La petición no es válida.",
    "err_internal": "Error interno.",
    "v_id": "Falta el id de la tarjeta.", "v_action": "Acción desconocida.", "v_option": "Elige una opción.",
    "v_answers": "Responde primero las preguntas.", "v_text": "El texto está vacío.",
    "v_revise": "Escribe qué debe cambiar.", "v_next": "Marca de 1 a 3 pasos distintos.",
    "v_note": "La nota es demasiado larga.",
    "js_err_net": "Sin conexión. Vuelve a intentar.", "js_viewer": "Visor de fotos", "js_close": "Cerrar",
    "js_prev": "Anterior", "js_next": "Siguiente", "js_open_tab": "Abrir en otra pestaña", "js_count": "{i} de {n}",
}

TABLES = {"en": EN, "es": ES}
JS_KEYS = tuple(k for k in EN if k.startswith("js_"))
_forced: Optional[str] = None


def set_lang(code: Optional[str]) -> None:
    """Force a language for this process (`polar demo --lang`). None goes back to POLAR_UI_LANG."""
    global _forced
    _forced = code if code in TABLES else None


def lang() -> str:
    if _forced:
        return _forced
    code = (os.environ.get("POLAR_UI_LANG") or "en").strip().lower()
    return code if code in TABLES else "en"


def has(key: str) -> bool:
    return key in EN


def t(key: str, **kw: Any) -> str:
    """The string `key` in the active language. A key missing from the table is a bug: it raises KeyError."""
    text = TABLES[lang()][key]
    return text.format(**kw) if kw else text
