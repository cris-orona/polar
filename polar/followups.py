"""Scheduled follow-ups: «revisa esto el 30-nov» or «cuando salga a producción».

The bus (`agent-bus followup …`) owns the data and the delivery. Polar creates, shows and cancels them.
Polar keeps a copy of what a card shows in `requests.followups_json`: a list of
{"id", "phrase", "text", "by", "state"} where state is open | cancelled | delivered.
The bus is never called at import time."""
from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polar import db

BUS_TIMEOUT = 15  # seconds for one bus call
MAX_FOLLOWUPS = 3
BUS_DOWN = "No pude hablar con agent-bus. Intenta otra vez."
NO_SESSION = "Falta la sesión."

# Owner button key -> label and bus SPEC. «prod» needs a PR link on the card.
LATER_OPTIONS = (("manana", "Mañana 9:00", "mañana 9:00"),
                 ("3d", "En 3 días", "+3d 9:00"),
                 ("lunes", "El lunes 9:00", "lunes 9:00"))
PROD_LABEL = "Cuando salga a producción"
PR_RE = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:[/?#]|$)", re.I)


class FollowupError(Exception):
    """A refusal with a Spanish sentence for the owner. `code` is the HTTP code for the web API."""

    def __init__(self, error: str, code: int = 422):
        super().__init__(error)
        self.error, self.code = error, code


def bus_bin() -> str:
    return os.environ.get("AGENT_BUS_BIN") or str(Path.home() / ".local/bin/agent-bus")


def bus(*args: str) -> Dict[str, Any]:
    """Run `agent-bus followup <args>`. Return its JSON object. A failure is {"error": "<Spanish>"}."""
    try:
        out = subprocess.run([bus_bin(), "followup", *args], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=BUS_TIMEOUT)
        data = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {"error": BUS_DOWN}
    if not isinstance(data, dict):
        return {"error": BUS_DOWN}
    if out.returncode != 0 and not data.get("error"):
        return {"error": BUS_DOWN}
    return data


def add(when: str, session: str, by: str, title: str, text: str = "", card: str = "",
        dry_run: bool = False) -> Dict[str, Any]:
    args = ["add", "--when", when, "--session", session, "--by", by, "--title", title]
    if text:
        args += ["--text", text]
    if card:
        args += ["--card", card]
    if dry_run:
        args.append("--dry-run")
    return bus(*args)


def cancel(fu: int, by: str = "owner") -> Dict[str, Any]:
    return bus("cancel", str(fu), "--by", by)


def set_card(fu: int, card: str) -> Dict[str, Any]:
    return bus("set-card", str(fu), card)


def session_id(req_or_requester: Any) -> Optional[str]:
    """The first word of the requester when it is a UUID."""
    who = req_or_requester.get("requester") if isinstance(req_or_requester, dict) else req_or_requester
    parts = (who or "").split()
    if not parts:
        return None
    try:
        uuid.UUID(parts[0])
    except ValueError:
        return None
    return parts[0]


def parse_revisar(raw: str) -> Tuple[str, str]:
    """«SPEC|what» -> (spec, what). Raise ValueError (English, for the CLI) when it is malformed."""
    spec, sep, what = raw.partition("|")
    spec, what = spec.strip(), what.strip()
    if not sep or not spec or not what:
        raise ValueError('bad --revisar %r: use "SPEC|what to check"' % raw[:40])
    return spec, what


def github_org() -> str:
    """The GitHub org whose PRs can be followed to production. Env POLAR_GITHUB_ORG; empty means any org."""
    return (os.environ.get("POLAR_GITHUB_ORG") or "").strip()


def pr_of(req: Dict[str, Any]) -> Optional[str]:
    """«repo#n» of the first link to a PR on the card, else None. With POLAR_GITHUB_ORG set, only that org counts."""
    links = (req.get("report") or {}).get("links") if req.get("kind") == "report" else (req.get("extra") or {}).get("links")
    for ln in links or []:
        m = PR_RE.match(str(ln.get("url") or ""))
        if m and (not github_org() or m.group(1).lower() == github_org().lower()):
            return "%s#%s" % (m.group(2), m.group(3))
    return None


def can_later(req: Dict[str, Any]) -> bool:
    """«Revisar después» goes on a report, and on a decision or questions card that the owner already answered.
    Never on a card that waits, a reply or email card, a prod-op card, or a card with no session id."""
    if (req.get("requester") or "").startswith("prod-op "):
        return False
    if not session_id(req):
        return False
    kind, status = req.get("kind"), req.get("status")
    if kind == "report":
        return status in ("pending", "shown", "noted")
    if kind == "decision":
        return status == "chosen"
    if kind == "questions":
        return status == "answered"
    return False


def later_options(req: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(key, label) of the owner's choices on this card."""
    opts = [(k, label) for k, label, _ in LATER_OPTIONS]
    if pr_of(req):
        opts.append(("prod", PROD_LABEL))
    return opts


def later_spec(req: Dict[str, Any], key: Any) -> str:
    for k, _, spec in LATER_OPTIONS:
        if key == k:
            return spec
    if key == "prod":
        pr = pr_of(req)
        if not pr:
            raise FollowupError("Esta tarjeta no trae un PR.", 409)
        return "prod:" + pr
    raise FollowupError("Opción no válida.", 400)


def stored(req: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(req.get("followups") or [])


def save(conn: Any, rid: str, items: List[Dict[str, Any]]) -> None:
    conn.execute("UPDATE requests SET followups_json=? WHERE id=?",
                 (json.dumps(items, ensure_ascii=False) if items else None, rid))


def entry(data: Dict[str, Any], state: str = "open") -> Dict[str, Any]:
    return {"id": data["id"], "phrase": data.get("phrase") or data.get("label") or "",
            "text": data.get("text") or "", "by": data.get("by") or "session", "state": state}


def add_later(conn: Any, req: Dict[str, Any], key: Any) -> Dict[str, Any]:
    """The owner picks «Revisar después». Return the bus JSON. Raise FollowupError."""
    if not can_later(req):
        raise FollowupError("«Revisar después» no aplica a esta tarjeta.", 409)
    spec = later_spec(req, key)
    data = add(spec, session_id(req) or "", "owner", req["title"], card=req["id"])
    if data.get("error"):
        raise FollowupError(str(data["error"]), 422)
    items = stored(req)
    if data.get("id") is not None and not any(i["id"] == data["id"] for i in items):
        items.append(entry(data))
        save(conn, req["id"], items)
    return data


def cancel_followup(conn: Any, req: Dict[str, Any], fu: Any) -> Dict[str, Any]:
    """The owner cancels one follow-up of this card. Return the bus JSON. Raise FollowupError."""
    if not isinstance(fu, int) or isinstance(fu, bool):
        raise FollowupError("Falta el seguimiento («fu»).", 400)
    items = stored(req)
    if not any(i["id"] == fu for i in items):
        raise FollowupError("Esta tarjeta no tiene ese seguimiento.", 409)
    data = cancel(fu)
    if data.get("error"):
        raise FollowupError(str(data["error"]), 422)
    state = data.get("state")
    new = "cancelled" if data.get("cancelled") else ("delivered" if state == "delivered" else None)
    if new:
        for i in items:
            if i["id"] == fu:
                i["state"] = new
        save(conn, req["id"], items)
    return data


def line(item: Dict[str, Any]) -> str:
    """The text of one follow-up on the card."""
    if item.get("by") == "owner":
        text = "🗓 Revisión programada %s" % item["phrase"]
    else:
        text = "🗓 Revisaré %s: %s" % (item["phrase"], item["text"])
    state = item.get("state")
    if state == "cancelled":
        return "🗓 ~%s~ · cancelada" % text[2:].strip().replace("~", "")
    if state == "delivered":
        return "✅ entregada · %s" % text[2:].strip()
    return text
