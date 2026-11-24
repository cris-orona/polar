"""Google Calendar, read only, for Mando's «Brief». Its own token file, so the Gmail token never changes.

The sign-in runs from the web: Mando asks for the consent link, the owner opens it, Google sends the browser to
http://localhost:8765/?code=… (nothing listens there) and the owner pastes that address back into Mando."""
from __future__ import annotations

import os
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from polar import db

warnings.filterwarnings("ignore", category=FutureWarning)
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
REDIRECT = "http://localhost:8765/"
TZ = "America/Mexico_City"
CACHE_S = 300
_lock = threading.Lock()
_flow: Dict[str, Any] = {}
_cache: Dict[str, Any] = {}


class NotConnected(Exception):
    pass


def token_path():
    return db.data_dir() / "google-cal-token.json"


def client_path():
    return db.data_dir() / "google-client.json"


def connected() -> bool:
    return token_path().is_file()


def _write(creds: Any) -> None:
    p = token_path()
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.chmod(p, 0o600)


def auth_url() -> str:
    if not client_path().is_file():
        raise ValueError("Falta el cliente de Google en Polar.")
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(client_path()), scopes=SCOPES, redirect_uri=REDIRECT)
    url, _state = flow.authorization_url(access_type="offline", prompt="consent")
    with _lock:
        _flow["flow"], _flow["at"] = flow, time.time()
    return url


def finish(pasted: str) -> None:
    pasted = (pasted or "").strip()
    if not pasted:
        raise ValueError("Pega la dirección que te dio Google.")
    with _lock:
        flow = _flow.pop("flow", None)
        at = _flow.pop("at", 0)
    if flow is None or time.time() - at > 900:
        raise ValueError("El enlace ya venció. Pide uno nuevo.")
    if "code=" in pasted:
        flow.fetch_token(authorization_response=pasted)
    else:
        flow.fetch_token(code=pasted)
    _write(flow.credentials)
    _cache.clear()


def _creds() -> Any:
    if not connected():
        raise NotConnected()
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    except (ValueError, OSError):
        raise NotConnected()
    if not creds.valid:
        if not (creds.refresh_token and creds.expired):
            raise NotConnected()
        try:
            creds.refresh(Request())
        except RefreshError:
            raise NotConnected()
        _write(creds)
    return creds


def _mx_day(now: datetime) -> Any:
    from zoneinfo import ZoneInfo  # Python 3.9 has it

    mx = now.astimezone(ZoneInfo(TZ))
    start = mx.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def shape(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for e in items:
        if e.get("status") == "cancelled":
            continue
        me = next((a for a in e.get("attendees") or [] if a.get("self")), None)
        if me and me.get("responseStatus") == "declined":
            continue
        s, en = e.get("start") or {}, e.get("end") or {}
        out.append({"title": e.get("summary") or "(sin título)", "start": s.get("dateTime") or s.get("date"),
                    "end": en.get("dateTime") or en.get("date"), "all_day": "date" in s and "dateTime" not in s,
                    "meet": bool(e.get("hangoutLink")), "where": (e.get("location") or "")[:80]})
    return out


def today(fetch: Optional[Any] = None) -> Dict[str, Any]:
    """Today's events (Mexico City). {"connected": False} when there is no token."""
    if not connected() and fetch is None:
        return {"connected": False, "events": []}
    hit = _cache.get("today")
    if hit and time.time() - hit[0] < CACHE_S and fetch is None:
        return hit[1]
    start, end = _mx_day(datetime.now(timezone.utc))
    if fetch is None:
        from googleapiclient.discovery import build

        svc = build("calendar", "v3", credentials=_creds(), cache_discovery=False)
        fetch = lambda: svc.events().list(calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),  # noqa: E731
                                          singleEvents=True, orderBy="startTime", maxResults=30, timeZone=TZ).execute()
    out = {"connected": True, "events": shape(fetch().get("items") or [])}
    _cache["today"] = (time.time(), out)
    return out
