"""Gmail for Polar. The owner's OAuth files stay on this host. Polar never prints a token.

Files (POLAR_DATA_DIR, default ~/polar-slack/data):
- google-client.json: the OAuth client (Google «Desktop app»).
- google-token.json: the owner's token, mode 0600. It refreshes by itself.
"""
from __future__ import annotations

import base64
import warnings
import html
import os
import re
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Dict, List, Optional

from polar import db

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]
warnings.filterwarnings("ignore", category=FutureWarning)  # Google warns about Python 3.9 on every call
NOT_CONNECTED = "Gmail no está conectado: corre polar mail auth"
AUTH_REDIRECT = "http://localhost:8765/"  # nothing listens here (port 1 is blocked by browsers); the owner copies the URL from the address bar


class NotConnected(Exception):
    """The OAuth files are missing or the token no longer works."""


def client_path():
    return db.data_dir() / "google-client.json"


def token_path():
    return db.data_dir() / "google-token.json"


def connected() -> bool:
    return token_path().is_file()


def error_code(exc: Exception) -> str:
    """A short code. Never the exception text."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return f"{type(exc).__name__} {status}" if status else type(exc).__name__


# -- OAuth ---------------------------------------------------------------------------
def _write_token(creds: Any) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    os.chmod(path, 0o600)


def credentials() -> Any:
    """The owner's credentials, refreshed when needed. Raise NotConnected when there are none."""
    if not connected():
        raise NotConnected(NOT_CONNECTED)
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    except (ValueError, OSError):
        raise NotConnected(NOT_CONNECTED)
    if not creds.valid:
        if not (creds.refresh_token and creds.expired):
            raise NotConnected(NOT_CONNECTED)
        try:
            creds.refresh(Request())
        except RefreshError:
            raise NotConnected(NOT_CONNECTED)
        _write_token(creds)
    return creds


def service() -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=credentials(), cache_discovery=False)


def auth_url() -> Any:
    """Start the installed-app flow. Return (flow, consent_url). Nothing opens on this host."""
    if not client_path().is_file():
        raise ValueError("falta %s: baja el JSON «Desktop app» de Google Cloud (ver README, sección Gmail)"
                         % client_path())
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # the redirect is http://localhost
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(str(client_path()), scopes=SCOPES, redirect_uri=AUTH_REDIRECT)
    url, _state = flow.authorization_url(access_type="offline", prompt="consent")
    return flow, url


def finish_auth(flow: Any, pasted: str) -> None:
    """Take the pasted redirect URL (or the bare code) and save the token."""
    pasted = pasted.strip()
    if not pasted:
        raise ValueError("no pegaste nada")
    if "code=" in pasted:
        flow.fetch_token(authorization_response=pasted)
    else:
        flow.fetch_token(code=pasted)
    _write_token(flow.credentials)


# -- reading ---------------------------------------------------------------------------
def _headers(msg: Dict[str, Any]) -> Dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(re.sub(r"<[^>]+>", "", raw))).strip()


def _walk(part: Dict[str, Any], mime: str) -> Optional[str]:
    if part.get("mimeType") == mime and (part.get("body") or {}).get("data"):
        return _b64(part["body"]["data"])
    for sub in part.get("parts") or []:
        found = _walk(sub, mime)
        if found:
            return found
    return None


def body_text(msg: Dict[str, Any]) -> str:
    payload = msg.get("payload") or {}
    text = _walk(payload, "text/plain")
    if text is None:
        raw = _walk(payload, "text/html")
        text = _strip_html(raw) if raw else ""
    return text.strip()


def summary(msg: Dict[str, Any]) -> Dict[str, Any]:
    h = _headers(msg)
    return {"id": msg.get("id"), "threadId": msg.get("threadId"), "from": h.get("from", ""),
            "to": h.get("to", ""), "subject": h.get("subject", ""), "date": h.get("date", ""),
            "snippet": html.unescape(msg.get("snippet", ""))}


def full(msg: Dict[str, Any]) -> Dict[str, Any]:
    h = _headers(msg)
    return {"id": msg.get("id"), "from": h.get("from", ""), "to": h.get("to", ""), "cc": h.get("cc", ""),
            "date": h.get("date", ""), "subject": h.get("subject", ""), "body": body_text(msg),
            "message_id": h.get("message-id", "")}


def search(svc: Any, query: str, max_results: int = 20) -> List[Dict[str, Any]]:
    listed = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for ref in listed.get("messages") or []:
        msg = svc.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        out.append(summary(msg))
    return out


def get_thread(svc: Any, thread_id: str, fmt: str = "full") -> Dict[str, Any]:
    return svc.users().threads().get(userId="me", id=thread_id, format=fmt).execute()


def thread(svc: Any, thread_id: str) -> Dict[str, Any]:
    t = get_thread(svc, thread_id)
    return {"id": t.get("id", thread_id), "messages": [full(m) for m in t.get("messages") or []]}


def owner_address(svc: Any) -> str:
    return svc.users().getProfile(userId="me").execute()["emailAddress"]


def thread_link(thread_id: str) -> str:
    return "https://mail.google.com/mail/u/0/#all/" + thread_id


def quote_info(svc: Any, thread_id: str) -> Dict[str, str]:
    """The last message of a thread, for the card: orig_name, orig_time, orig_text, orig_permalink, subject."""
    msgs = get_thread(svc, thread_id).get("messages") or []
    if not msgs:
        return {}
    last = msgs[-1]
    h = _headers(last)
    name, addr = parseaddr(h.get("from", ""))
    try:
        from zoneinfo import ZoneInfo
        when = datetime.fromtimestamp(int(last["internalDate"]) / 1000, ZoneInfo("America/Mexico_City")
                                      ).strftime("%d/%m %H:%M")
    except (KeyError, ValueError, TypeError):
        when = ""
    return {"orig_name": name or addr or None, "orig_time": when,
            "orig_text": " ".join(body_text(last).split())[:300] or " ".join(last.get("snippet", "").split())[:300],
            "orig_permalink": thread_link(thread_id), "subject": h.get("subject", "")}


# -- inbox: new mail for the router ------------------------------------------------------
INBOX_TEXT_MAX = 600
_AUTO_LOCAL = re.compile(r"(?i)(^|[-_.+])(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|"
                         r"notifications?|bounces?)($|[-_.+])")
# «On Mon, Nov 20, 2026 at 10:00 X wrote:» / «El lun, 20 nov 2026 a las 10:00, X escribió:». A real head has a date,
# so a digit is required: «On the route issue:» + «Juan wrote:» is the sender's own text.
_QUOTE_HEAD = re.compile(r"(?i)^(on|el)\b.*\d.*\b(wrote|escribió):\s*$")
_QUOTE_OPEN = re.compile(r"(?i)^(on|el)\b.*\d.*")
_QUOTE_CLOSE = re.compile(r"(?i)^.*\b(wrote|escribió):\s*$")
_QUOTE_RULE = re.compile(r"(?i)^-{2,}\s*(original message|mensaje original|forwarded message|mensaje reenviado)\s*-{2,}$")
_MAIL_FROM = re.compile(r"(?i)^(from|de):\s+\S")
_MAIL_SENT = re.compile(r"(?i)^(sent|enviado|enviado el|date|fecha):\s")


def new_text(body: str) -> str:
    """The text the sender wrote now: quoted lines (>) are left out, and the text stops at the head of the quoted
    reply or at the signature. An answer written below or between the quoted lines stays. When nothing is left (a
    forward with no words above it), the body stays whole."""
    lines = body.replace("\r\n", "\n").split("\n")
    keep: List[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if s.startswith(">"):
            continue
        if (ln.rstrip() == "--" or _QUOTE_HEAD.match(s) or _QUOTE_RULE.match(s)
                or (_QUOTE_OPEN.match(s) and _QUOTE_CLOSE.match(nxt) and not nxt.startswith(">"))
                or (_MAIL_FROM.match(s) and _MAIL_SENT.match(nxt))):
            break
        keep.append(ln)
    return "\n".join(keep).strip() or body.strip()


def _any_part(part: Dict[str, Any], pred: Any) -> bool:
    return bool(pred(part)) or any(_any_part(sub, pred) for sub in part.get("parts") or [])


def _automated(msg: Dict[str, Any], h: Dict[str, str], addr: str) -> bool:
    """True for mail no person wrote: no-reply senders, lists, bulk mail, auto replies, invitations, reactions."""
    if _AUTO_LOCAL.search(addr.split("@", 1)[0]):
        return True
    if h.get("list-id") or h.get("precedence", "").strip().lower() in ("bulk", "list", "junk", "auto_reply"):
        return True
    if h.get("auto-submitted", "no").strip().lower() != "no":
        return True
    return _any_part(msg.get("payload") or {}, lambda p: (
        (p.get("mimeType") or "").lower() in ("text/calendar", "application/ics", "text/vnd.google.email-reaction+json")
        or (p.get("filename") or "").lower().endswith(".ics")))


_DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
_AR_METHOD = re.compile(r"([a-z0-9-]+)=([a-z]+)")
_AR_PROP = re.compile(r"([a-z0-9.-]+)=(.+)")
_AR_DKIM = {"header.i": re.compile(r"[a-z0-9._+-]*@[a-z0-9.-]+"), "header.s": re.compile(r"[a-z0-9._-]+"),
            "header.b": re.compile(r"\"?[a-z0-9+/=]+\"?"), "header.d": re.compile(r"[a-z0-9.-]+")}


def _ar_parts(val: str) -> Optional[List[List[str]]]:
    """The `;` parts of an Authentication-Results value as lists of tokens, comments removed. None when the text
    has a shape Google does not write: a backslash; a comment with a bracket, a quote or a `;` in it; a quoted
    string that is not the whole value of a property, or that has a space, a `;` or a bracket in it."""
    parts: List[List[str]] = []
    tokens: List[str] = []
    tok: List[str] = []
    i, n = 0, len(val)

    def end() -> None:
        if tok:
            tokens.append("".join(tok))
            tok.clear()

    while i < n:
        ch = val[i]
        if ch in "\\)":
            return None
        if ch == "(":
            j = val.find(")", i + 1)
            if j < 0 or any(c in val[i + 1:j] for c in '(";\\'):
                return None
            end()
            i = j + 1
        elif ch == '"':
            j = val.find('"', i + 1)
            if j < 0 or any(c in val[i + 1:j] for c in " ;()\\") or not tok or tok[-1] != "=":
                return None
            if j + 1 < n and val[j + 1] not in " ;":
                return None
            tok.append(val[i:j + 1])
            i = j + 1
        elif ch == ";":
            end()
            parts.append(tokens)
            tokens = []
            i += 1
        elif ch == " ":
            end()
            i += 1
        else:
            tok.append(ch)
            i += 1
    end()
    parts.append(tokens)
    return parts


def auth_results(msg: Dict[str, Any]) -> Optional[List[Any]]:
    """Google's own check of the mail: [(method, result, {property: value})] from the first
    Authentication-Results header, or None when it is missing, was not written by mx.google.com, or has anything
    this strict reader does not know.
    The sender controls text inside this header: the envelope sender (in the SPF comment and in `smtp.mailfrom=`;
    «dmarc=pass@evil.example» is a valid one) and the `i=` tag of a DKIM signature. So a search over the whole
    value can be fooled. Here the value is cut into `;` parts and tokens (_ar_parts), each part must be
    `method=result property=value …`, and a result is read only from its own part. Also not proven, because a real
    header from Google has none of these and a forged part would need one:
    - a DKIM part after another method (Google writes the DKIM results first; text from the SPF fields comes later);
    - a DKIM part with a property other than header.i, header.s, header.b, header.d, a property twice, or a
      header.i that is quoted or is not `[name]@domain`;
    - an SPF part with a property other than smtp.mailfrom and smtp.helo; a DMARC part that is not exactly one
      header.from with a domain; two SPF, two DMARC, two ARC or two DARA parts."""
    first = next((hd.get("value", "") for hd in (msg.get("payload") or {}).get("headers", [])
                  if hd.get("name", "").lower() == "authentication-results"), "")
    parts = _ar_parts(" ".join(first.lower().split()))
    if not parts or parts[0] != ["mx.google.com"]:
        return None
    out: List[Any] = []
    for tokens in parts[1:]:
        if not tokens:
            continue
        head = _AR_METHOD.fullmatch(tokens[0])
        if not head:
            return None
        method, result = head.group(1), head.group(2)
        props: Dict[str, str] = {}
        for token in tokens[1:]:
            m = _AR_PROP.fullmatch(token)
            if not m or m.group(1) in props:
                return None
            value = m.group(2)
            if '"' in value and not (len(value) >= 2 and value[0] == value[-1] == '"' and value.count('"') == 2):
                return None
            props[m.group(1)] = value
        if method == "dkim":
            if any(o[0] != "dkim" for o in out):
                return None
            if "header.i" not in props or any(k not in _AR_DKIM or not _AR_DKIM[k].fullmatch(v) for k, v in props.items()):
                return None
        elif method == "spf":
            if any(k not in ("smtp.mailfrom", "smtp.helo") for k in props):
                return None
        elif method == "dmarc":
            if list(props) != ["header.from"] or not _DNS_NAME.fullmatch(props["header.from"]):
                return None
        if method in ("spf", "dmarc", "arc", "dara") and any(o[0] == method for o in out):
            return None
        out.append((method, result, props))
    return out


def auth_ok(msg: Dict[str, Any], addr: str, own_domain: str = "") -> bool:
    """True when Google checked that the mail really comes from the domain of `addr` (the «From» address).
    Only Google's own result counts (auth_results). The sender is proven by one of:
    - dmarc=pass for the From domain (or its parent domain);
    - a DKIM signature of the From domain itself;
    - for the owner's own domain only: a DKIM signature of the Google Workspace of that domain
      (<domain with dashes>.<digits>.gappssmtp.com). A Workspace domain with no DKIM key of its own fails DMARC, but
      only its own Workspace can sign with that name. For other domains the name is not proof: «my.co.com» and
      «my-co.com» give the same one.
    A DKIM pass of any other domain proves nothing about «From». No header: not proven.
    Trust in Google that is left: `header.i` is the identity of the signature (the `i=` tag), and a verifier must
    fail a signature whose `i=` is outside its signing domain `d=` (RFC 6376). When Google writes `header.d`, this
    is checked here too."""
    dom = addr.rsplit("@", 1)[-1].lower()
    results = auth_results(msg)
    if not results or not _DNS_NAME.fullmatch(dom):
        return False
    for method, result, props in results:
        if result != "pass":
            continue
        if method == "dmarc":
            hf = props["header.from"]
            if hf == dom or dom.endswith("." + hf):
                return True
        elif method == "dkim":
            signer = props["header.i"].rsplit("@", 1)[1]
            if props["header.i"].count("@") != 1 or not _DNS_NAME.fullmatch(signer):
                continue
            signed_by = props.get("header.d")  # the signing domain, when Google writes it: `i=` must be inside it
            if signed_by is not None and signer != signed_by and not signer.endswith("." + signed_by):
                continue
            if signer == dom:
                return True
            if dom == own_domain.lower() and re.fullmatch(re.escape(dom.replace(".", "-")) + r"\.\d+\.gappssmtp\.com", signer):
                return True
    return False


def clean(text: Any, cap: Optional[int] = None) -> str:
    """One line of plain text: control and format characters (ESC, Ctrl-C, new lines, direction marks) become a
    space and white space is joined. This text is typed into a terminal later, so a mail must not carry keys."""
    import unicodedata
    out = " ".join("".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in str(text or "")).split())
    return out[:cap] if cap else out


def _addresses(value: str) -> List[str]:
    from email.utils import getaddresses
    return [a.lower() for _n, a in getaddresses([value]) if a]


def inbox_row(msg: Dict[str, Any], me: str, since: float) -> Optional[Dict[str, Any]]:
    """One `polar inbox` shaped row for a message, or None when the router must not see it."""
    try:
        ms = int(msg["internalDate"])
    except (KeyError, ValueError, TypeError):
        return None
    if ms <= since * 1000:
        return None
    h = _headers(msg)
    name, addr = parseaddr(h.get("from", ""))
    addr = addr.lower()
    if not addr or addr == me or _automated(msg, h, addr):
        return None
    addr = clean(addr, 254)
    subject = clean(h.get("subject", ""), 200)
    text = clean(new_text(body_text(msg)), INBOX_TEXT_MAX) or clean(html.unescape(msg.get("snippet", "")), INBOX_TEXT_MAX)
    tid = msg.get("threadId") or msg.get("id")
    proven = auth_ok(msg, addr, me.rsplit("@", 1)[-1])
    return {"source": "mail", "ts": "%.3f" % (ms / 1000), "channel": "mail", "thread_ts": tid,
            "message_id": msg.get("id"), "sender": clean(name, 80) or addr, "sender_id": addr, "subject": subject,
            "text": " — ".join(x for x in (subject, text) if x), "permalink": thread_link(tid), "is_dm": True,
            "to_owner": me in _addresses(h.get("to", "")), "auth_ok": proven,
            "internal": proven and addr.rsplit("@", 1)[-1] == me.rsplit("@", 1)[-1]}


INBOX_PAGE = 100
INBOX_LIST_MAX = 1000  # message ids listed in one read
INBOX_BATCH = 50       # messages opened in one read (each one costs Gmail quota; the mailbox gets about 10 a day)
INBOX_SECONDS = 20     # time for one read; the caller stops the command after 40 s


def inbox(svc: Any, since: float, batch: int = INBOX_BATCH, seconds: float = INBOX_SECONDS,
          list_max: int = INBOX_LIST_MAX, clock: Any = time.monotonic) -> Any:
    """New messages after `since` (epoch seconds) that a person wrote to the owner, oldest first. No attachment
    and no full body leaves Polar: only the subject and the first 600 characters of the new text.
    Returns (rows, more). Gmail lists the newest first, so all ids are listed (cheap) and the OLDEST `batch` messages
    are opened, for at most `seconds`: after a long stop the read stays short and the oldest mail comes first. A read
    that Gmail stops in the middle (quota) also ends there.
    `more` is None when every listed message was opened; else it is the time of the newest opened message, and the
    caller asks again from there. The caller may ask again for the same time: it keeps the message ids it placed."""
    me = owner_address(svc).lower()
    start = clock()
    refs: List[Dict[str, Any]] = []
    token = None
    while len(refs) < list_max:
        kw: Dict[str, Any] = {"userId": "me", "q": "after:%d -from:me -in:chats" % int(since), "maxResults": INBOX_PAGE}
        if token:
            kw["pageToken"] = token
        listed = svc.users().messages().list(**kw).execute()
        refs += listed.get("messages") or []
        token = listed.get("nextPageToken")
        if not token:
            break
    if token:
        print("polar mail inbox: over %d messages after %d; the oldest are not read" % (len(refs), int(since)),
              file=sys.stderr)
    out, more, newest = [], None, since * 1000
    for n, ref in enumerate(reversed(refs)):
        # The batch ends only when it reached mail over 2 s after `since`: the caller asks again from `more` - 1 s,
        # and a burst of more than `batch` messages in one second would give the same batch for ever.
        if (n >= batch and newest > since * 1000 + 2000) or (n and clock() - start > seconds):
            more = newest / 1000
            break
        try:
            msg = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        except Exception as e:
            if getattr(getattr(e, "resp", None), "status", None) == 404:
                continue  # deleted after the list: there is nothing to place
            if not n:
                raise
            # Gmail refused in the middle (quota, network): keep what was opened; the caller asks again from here.
            print("polar mail inbox: stopped after %d messages (%s)" % (n, type(e).__name__), file=sys.stderr)
            more = newest / 1000
            break
        try:
            newest = max(newest, int(msg["internalDate"]))
        except (KeyError, ValueError, TypeError):
            pass
        row = inbox_row(msg, me, since)
        if row:
            out.append(row)
    return sorted(out, key=lambda r: float(r["ts"])), more


# -- writing ---------------------------------------------------------------------------
def to_html(text: str) -> str:
    """Simple Markdown to HTML: paragraphs, line breaks, **bold**, *italic*, [link](url), - lists."""
    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)

    out: List[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if lines and all(re.match(r"\s*[-*] ", ln) for ln in lines):
            out.append("<ul>" + "".join("<li>%s</li>" % inline(re.sub(r"^\s*[-*] ", "", ln)) for ln in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(inline(ln) for ln in lines) + "</p>")
    return "\n".join(out)


def build_reply(svc: Any, thread_id: str, text: str, cc: Optional[str] = None) -> Dict[str, Any]:
    """The Gmail message body {raw, threadId} for a reply in a thread."""
    t = get_thread(svc, thread_id, "metadata")
    msgs = t.get("messages") or []
    if not msgs:
        raise ValueError("el hilo no tiene mensajes")
    me = owner_address(svc).lower()
    last = msgs[-1]
    h = _headers(last)
    other = next((m for m in reversed(msgs) if parseaddr(_headers(m).get("from", ""))[1].lower() != me), None)
    if other is not None:
        oh = _headers(other)
        to = oh.get("reply-to") or oh.get("from", "")
    else:  # the owner wrote last: write to the same people again
        to = h.get("to", "")
    if not to:
        raise ValueError("no encuentro a quién responder")
    subject = h.get("subject", "")
    if not re.match(r"(?i)^\s*re:", subject):
        subject = "Re: " + subject
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    mid = h.get("message-id")
    if mid:
        msg["In-Reply-To"] = mid
        msg["References"] = " ".join(x for x in [h.get("references"), mid] if x)
    msg.set_content(text)
    msg.add_alternative(to_html(text), subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return {"raw": raw, "threadId": thread_id}


def create_draft(svc: Any, thread_id: str, text: str, cc: Optional[str] = None) -> str:
    body = build_reply(svc, thread_id, text, cc)
    return svc.users().drafts().create(userId="me", body={"message": body}).execute()["id"]


def send_reply(svc: Any, thread_id: str, text: str, cc: Optional[str] = None) -> Dict[str, str]:
    body = build_reply(svc, thread_id, text, cc)
    sent = svc.users().messages().send(userId="me", body=body).execute()
    return {"message_id": sent["id"], "thread_id": sent.get("threadId", thread_id),
            "permalink": thread_link(sent.get("threadId", thread_id))}

