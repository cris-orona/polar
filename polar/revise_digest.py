"""Weekly digest of the owner's corrections («Pedir cambios») → candidate rules.

Code node: collect the real corrections of the last N days (a follow-up tap, `via: next`, is not a correction) and
write them as Markdown. With `start`, hand them to Polar as one owner task (the same path as a task typed in Mando):
the task session groups them, proposes 1–3 rules in one decision card, and adds the rules the owner accepts.
Nothing here calls a model: grouping is judgment, so the session does it.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List, Optional

from polar import db

MAX_TEXT = 400       # characters of one correction in the digest
MAX_CONTEXT = 6000   # webapi.new_task keeps this much context

BRIEF = (
    "Resumen semanal de «Pedir cambios» → reglas. En el contexto están las correcciones reales del dueño de los "
    "últimos {days} días ({n}). Agrúpalas por tema. Propón de 1 a 3 reglas nuevas (o cambios a reglas que ya "
    "existen en el archivo de reglas o en la memoria del agente) que habrían evitado al menos 2 correcciones cada una. Manda "
    "UNA tarjeta de decisión: cada regla con su texto exacto, cuántas correcciones evita y un ejemplo real citado. "
    "Opción recomendada: agregar las que propones. Si no hay un tema repetido, manda un informe corto y no "
    "propongas nada. Lo que el dueño acepte: escríbelo en la regla o memoria que toca y dilo en el informe final."
)


def corrections(conn: Any, days: int = 7) -> List[Dict[str, Any]]:
    """Cards where the owner wrote a correction in the last `days` days, oldest first."""
    rows = conn.execute(
        "SELECT id, kind, title, decided_at, result_json FROM requests "
        "WHERE decided_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
        "AND json_extract(result_json, '$.instructions') IS NOT NULL "
        "AND COALESCE(json_extract(result_json, '$.via'), '') != 'next' "
        "ORDER BY decided_at", ("-%d days" % days,)).fetchall()
    out = []
    for r in rows:
        res = json.loads(r[4] or "{}")
        text = str(res.get("instructions") or "").strip()
        if text:
            out.append({"id": r[0], "kind": r[1], "title": r[2], "at": r[3], "text": text})
    return out


def markdown(items: List[Dict[str, Any]], days: int) -> str:
    lines = ["# Correcciones del dueño, últimos %d días (%d)" % (days, len(items)), ""]
    for it in items:
        text = it["text"].replace("\n", " ")
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "…"
        lines.append("- %s · %s · «%s» → %s" % ((it["at"] or "")[:10], it["kind"], it["title"][:80], text))
    return "\n".join(lines) + "\n"


def save(text: str) -> str:
    """Keep the full digest next to polar.db, one file per day; return its path."""
    from datetime import datetime, timezone
    folder = db.data_dir() / "revise-digest"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%s.md" % datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    path.write_text(text, encoding="utf-8")
    return str(path)


def start(items: List[Dict[str, Any]], days: int, port: Optional[int] = None,
          token: Optional[str] = None) -> Dict[str, Any]:
    """Send the digest to Polar's local web API as one owner task. Return its JSON answer."""
    from polar import webapi
    import os
    port = port or int(os.environ.get("POLAR_WEB_PORT") or webapi.DEFAULT_PORT)
    token = token or webapi.load_token()
    full = markdown(items, days)
    path = save(full)
    context = "Lista completa (%d): %s\n\n" % (len(items), path) + full
    if len(context) > MAX_CONTEXT:
        context = context[:MAX_CONTEXT - 60] + "\n… (lista cortada: lee el archivo de arriba)\n"
    body = json.dumps({"action": "task", "text": BRIEF.format(days=days, n=len(items)),
                       "context": context}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/act" % port, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(days: int = 7, send: bool = False, min_items: int = 2, conn: Any = None) -> Dict[str, Any]:
    items = corrections(conn if conn is not None else db.connect(), days)
    if not send:
        return {"count": len(items), "markdown": markdown(items, days)}
    if len(items) < min_items:
        return {"count": len(items), "started": False, "why": "fewer than %d corrections" % min_items}
    answer = start(items, days)
    return {"count": len(items), "started": bool(answer.get("ok")), "answer": answer}
