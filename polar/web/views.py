"""The HTML of the web UI, built from the request rows. Every dynamic value goes through `esc` or `md`."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse

import markdown

from polar import db
from polar.web.strings import JS_KEYS, has, lang, t

STATIC = Path(__file__).resolve().parent / "static"
PAGE_SIZE = 40
ISO = "%Y-%m-%dT%H:%M:%SZ"
OPEN_REPORT = ("pending", "shown", "noted")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
REC_RE = re.compile(r"\s*\((?:recomendad[oa]|recommended)\)", re.I)


# -- escaping and Markdown ---------------------------------------------------------------------------------------------
def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


LT, AMP = "", ""  # private-use marks: raw < and & never reach the Markdown parser as HTML
HREF_RE = re.compile(r'href="([^"]*)"')
IMG_RE = re.compile(r'<img\b[^>]*?\balt="([^"]*)"[^>]*?/?>|<img\b[^>]*?/?>')
LIST_GAP_RE = re.compile(r"(?m)^(?![ \t]*(?:[-*+]|\d+[.)])[ \t])([^\n|>#`][^\n]*)\n(?=[ \t]*(?:[-*+]|\d+[.)])[ \t]+\S)")
WRAPPER_RE = re.compile(r"</?agent-message\b[^>\n]{0,300}?>", re.I)


def strip_wrappers(text: str) -> str:
    """Remove the machine wrapper tags («<agent-message from=...>») a relayed text can carry; keep the words."""
    return WRAPPER_RE.sub("", text).strip() if "agent-message" in text.lower() else text


def safe_url(url: str) -> bool:
    """True for http and https links and for paths on this site."""
    u = html.unescape(url).strip()
    if re.search(r"[\x00-\x1f\s]", u):
        return False
    if u.startswith("/") and not u.startswith("//") or u.startswith("#"):
        return True
    return urlparse(u).scheme.lower() in ("http", "https")


def _fix_href(m: "re.Match[str]") -> str:
    url = m.group(1)
    if not safe_url(url):
        return 'href="#"'
    ext = not html.unescape(url).startswith(("/", "#"))
    return 'href="%s"%s' % (url, ' target="_blank" rel="noopener noreferrer"' if ext else "")


def md(text: Any) -> str:
    """Markdown to safe HTML. Raw HTML in the source shows as text; images become their alt text."""
    src = strip_wrappers("" if text is None else str(text)).replace(LT, "").replace(AMP, "")
    src = LIST_GAP_RE.sub(r"\1\n\n", src)  # agents write a list right under a sentence; Markdown needs a blank line
    src = src.replace("&", AMP).replace("<", LT)
    out = markdown.markdown(src, extensions=["tables", "fenced_code", "sane_lists"], output_format="html")
    out = IMG_RE.sub(lambda m: m.group(1) or "", out)
    out = HREF_RE.sub(_fix_href, out)
    return '<div class="md">%s</div>' % out.replace(LT, "&lt;").replace(AMP, "&amp;")


def plain(text: Any) -> str:
    return esc(strip_wrappers("" if text is None else str(text)))


# -- time ----------------------------------------------------------------------------------------------------------------
def _zone() -> Any:
    """POLAR_TZ when it is set and valid, else None (the system's local zone)."""
    name = (os.environ.get("POLAR_TZ") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            return None
    return None


def parse(iso: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(iso or "", ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def local(dt: datetime) -> datetime:
    zone = _zone()
    return dt.astimezone(zone) if zone else dt.astimezone()


def when(iso: Optional[str]) -> str:
    dt = parse(iso)
    if not dt:
        return ""
    d = local(dt)
    return t("date_fmt", d=d.day, m=t("months").split()[d.month - 1], hm=d.strftime("%H:%M"))


def ago(iso: Optional[str], now: Optional[datetime] = None) -> str:
    dt = parse(iso)
    if not dt:
        return ""
    mins = int(((now or datetime.now(timezone.utc)) - dt).total_seconds() // 60)
    if mins < 1:
        return t("ago_now")
    if mins < 60:
        return t("ago_min", n=mins)
    if mins < 24 * 60:
        return t("ago_h", n=mins // 60)
    return t("ago_d", n=mins // (24 * 60))


def time_tag(iso: Optional[str]) -> str:
    return '<time datetime="%s" title="%s">%s</time>' % (esc(iso), esc(when(iso)), esc(ago(iso)))


def day_label(d: date, today: date) -> str:
    if d == today:
        return t("today")
    if d == today - timedelta(days=1):
        return t("yesterday")
    return t("day_fmt", wd=t("weekdays").split()[d.weekday()], d=d.day, m=t("months").split()[d.month - 1])


# -- queries -------------------------------------------------------------------------------------------------------------
NOT_CANARY = " AND COALESCE(extras_json,'') NOT LIKE '%\"canary\": true%'"


def _rows(conn: Any, sql: str, args: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    return [db._row(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]


def is_open(card: Dict[str, Any]) -> bool:
    if card["kind"] == "report":
        return card["status"] in OPEN_REPORT
    return card["status"] in db.OPEN_STATUSES


def pending_cards(conn: Any) -> List[Dict[str, Any]]:
    """What waits for the owner: open action cards (oldest first), then reports not seen yet (oldest first)."""
    rows = _rows(conn, "SELECT * FROM requests WHERE ((kind!='report' AND status IN ('pending','shown'))"
                       " OR (kind='report' AND status IN ('pending','shown','noted') AND silent=0))" + NOT_CANARY +
                 " ORDER BY created_at, rowid")
    return [r for r in rows if r["kind"] != "report"] + [r for r in rows if r["kind"] == "report"]


def waiting_count(conn: Any) -> int:
    return len(pending_cards(conn))


HISTORY_SQL = ("SELECT * FROM requests WHERE status IN (%s) AND NOT (kind='report' AND status IN"
               " ('filed','expired','replaced'))" % ",".join("'%s'" % s for s in db.FINAL_STATUSES) + NOT_CANARY)


def history(conn: Any, page: int) -> Tuple[List[Dict[str, Any]], bool]:
    rows = _rows(conn, HISTORY_SQL + " ORDER BY COALESCE(decided_at, created_at) DESC, rowid DESC LIMIT ? OFFSET ?",
                 (PAGE_SIZE + 1, page * PAGE_SIZE))
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def reports(conn: Any, q: str, page: int) -> Tuple[List[Dict[str, Any]], bool]:
    sql, args = "SELECT * FROM requests WHERE kind='report'", []  # type: Tuple[str, List[Any]]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += " AND (title LIKE ? ESCAPE '\\' OR context LIKE ? ESCAPE '\\' OR draft_text LIKE ? ESCAPE '\\')"
        args += [like, like, like]
    rows = _rows(conn, sql + " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                 tuple(args) + (PAGE_SIZE + 1, page * PAGE_SIZE))
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def state(conn: Any) -> Dict[str, Any]:
    """A small version stamp: the page reloads itself when it changes."""
    r = conn.execute("SELECT COUNT(*) AS n, MAX(created_at) AS c, MAX(decided_at) AS d,"
                     " SUM(CASE WHEN status IN ('pending','shown','noted') THEN 1 ELSE 0 END) AS o FROM requests"
                     ).fetchone()
    stamp = "%s|%s|%s|%s" % (r["n"], r["c"] or "", r["d"] or "", r["o"] or 0)
    return {"v": hashlib.sha1(stamp.encode()).hexdigest()[:16], "waiting": waiting_count(conn)}


# -- pieces of a card ----------------------------------------------------------------------------------------------------
def requester(card: Dict[str, Any]) -> str:
    return (card.get("requester_label") or "").strip() or t("no_requester")


def card_files(card: Dict[str, Any]) -> List[Tuple[int, str]]:
    files = (card.get("report") or {}).get("files") or (card.get("extra") or {}).get("files") or []
    return [(i, str(f).replace("\\", "/").rsplit("/", 1)[-1]) for i, f in enumerate(files)]


def card_links(card: Dict[str, Any]) -> List[Dict[str, str]]:
    return list((card.get("report") or {}).get("links") or (card.get("extra") or {}).get("links") or [])


def links_html(card: Dict[str, Any]) -> str:
    out = ['<a class="btn link" href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
           % (esc(ln.get("url")), esc(ln.get("label") or ln.get("url"))) for ln in card_links(card)
           if safe_url(str(ln.get("url") or ""))]
    return '<p class="links">%s</p>' % "".join(out) if out else ""


def files_html(card: Dict[str, Any]) -> str:
    """Pictures as previews that open in the photo viewer; other files as links (a new tab or a download)."""
    cid = quote(card["id"], safe="")
    thumbs, others = [], []
    for i, name in card_files(card):
        url = "/files/%s/%d" % (cid, i)
        if name.lower().endswith(IMG_EXT):
            thumbs.append('<a class="thumb" data-lbx href="%s" target="_blank" rel="noopener" data-name="%s">'
                          '<img src="%s" alt="%s" loading="lazy" decoding="async"></a>'
                          % (url, esc(name), url, esc(name)))
        else:
            others.append('<a class="file" href="%s" target="_blank" rel="noopener">%s</a>' % (url, esc(name)))
    out = '<div class="thumbs">%s</div>' % "".join(thumbs) if thumbs else ""
    return out + ('<p class="files">%s</p>' % "".join(others) if others else "")


def option_label(card: Dict[str, Any], key: Any) -> str:
    for o in card.get("options") or []:
        if o.get("key") == key:
            return REC_RE.sub("", str(o.get("label") or key))
    return str(key or "")


def outcome(card: Dict[str, Any]) -> str:
    """What happened to a closed card, in one short line."""
    st, res, kind = card["status"], card.get("result") or {}, card["kind"]
    if st == "chosen":
        if kind == "report":
            return t("did_resume") if res.get("option") == "resume" else t("did_seen")
        return t("did_chose", label=option_label(card, res.get("option")))
    if st == "revise":
        if res.get("via") == "next":
            return t("did_next", steps="; ".join(str(s) for s in res.get("steps") or []))
        return t("did_revise")
    if st == "discarded" and res.get("by") == "requester":
        return t("did_withdrawn")
    return t("st_" + st) if has("st_" + st) else st


def _answers_html(card: Dict[str, Any]) -> str:
    have = (card.get("result") or {}).get("answers") or {}
    items = []
    for i, q in enumerate(card.get("options") or [], 1):
        ans = have.get(q["key"]) or {}
        items.append('<li><span class="qt">%d. %s</span> <b>%s</b></li>'
                     % (i, plain(q.get("text")), plain(ans.get("text") or t("no_answer"))))
    note = ((card.get("result") or {}).get("note") or "").strip()
    if note:
        items.append('<li><span class="qt">%s</span> %s</li>' % (esc(t("note")), plain(note)))
    return '<ul class="answers">%s</ul>' % "".join(items)


def _revise_box() -> str:
    return ('<div class="revise-box" hidden><label class="lbl">%s<textarea name="revise" rows="3" maxlength="%d">'
            '</textarea></label><div class="actions"><button class="btn primary" type="button" data-action="revise">'
            '%s</button></div></div>' % (esc(t("revise_label")), db.MAX_REPLY_CHARS, esc(t("revise_send"))))


def _revise_btn() -> str:
    return '<button class="btn" type="button" data-toggle="revise" aria-expanded="false">%s</button>' % esc(t("revise"))


def _rec_badge() -> str:
    return ' <span class="rec">%s</span>' % esc(t("recommended"))


def _decision_actions(card: Dict[str, Any]) -> str:
    out = []
    for o in card.get("options") or []:
        rec = bool(o.get("recommended")) or bool(REC_RE.search(str(o.get("label") or "")))
        out.append('<button class="btn%s" type="button" data-action="choose" data-option="%s">%s%s</button>'
                   % (" primary" if rec else "", esc(o.get("key")), esc(option_label(card, o.get("key"))),
                      _rec_badge() if rec else ""))
    return '<div class="actions">%s%s</div>%s' % ("".join(out), _revise_btn(), _revise_box())


def _questions_form(card: Dict[str, Any]) -> str:
    rid, have = card["id"], (card.get("result") or {}).get("answers") or {}
    parts = []
    for i, q in enumerate(card.get("options") or [], 1):
        prev = have.get(q["key"]) or {}
        picked = prev.get("option") or (None if prev else q.get("recommended"))
        opts = "".join(
            '<label class="opt"><input type="radio" name="q-%s-%s" value="%s"%s> <span>%s%s</span></label>'
            % (esc(rid), esc(q["key"]), esc(o["key"]), " checked" if o["key"] == picked else "", esc(o["label"]),
               _rec_badge() if o["key"] == q.get("recommended") else "") for o in q.get("options") or [])
        typed = prev.get("text") if prev and not prev.get("option") else ""
        other = t("q_other") if q.get("options") else t("q_answer")
        parts.append('<fieldset class="q" data-qkey="%s"><legend>%d. %s</legend>%s<label class="lbl">%s'
                     '<input type="text" name="qt" maxlength="%d" value="%s"></label></fieldset>'
                     % (esc(q["key"]), i, plain(q.get("text")), opts, esc(other), db.MAX_REPLY_CHARS, esc(typed)))
    note = ((card.get("result") or {}).get("note") or "")
    parts.append('<label class="lbl">%s<textarea name="note" rows="2" maxlength="%d">%s</textarea></label>'
                 % (esc(t("q_note")), db.MAX_REPLY_CHARS, esc(note)))
    parts.append('<div class="actions"><button class="btn primary" type="button" data-action="answer">%s</button>%s'
                 '</div>%s' % (esc(t("q_send")), _revise_btn(), _revise_box()))
    return '<div class="qs">%s</div>' % "".join(parts)


def _original(card: Dict[str, Any]) -> str:
    bits = [card.get("orig_name"), card.get("orig_channel_name") and "#" + card["orig_channel_name"],
            card.get("email_subject") and t("subject", s=card["email_subject"]), card.get("orig_time")]
    head = " · ".join(esc(b) for b in bits if b)
    if not (head or card.get("orig_text")):
        return ""
    link = ""
    if card.get("orig_permalink") and safe_url(card["orig_permalink"]):
        link = ' <a href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (esc(card["orig_permalink"]),
                                                                                 esc(t("open_original")))
    return ('<blockquote class="orig"><p class="meta">%s%s</p><p>%s</p></blockquote>'
            % (head, link, plain(card.get("orig_text"))))


def _reply_actions(card: Dict[str, Any], allow_send: bool) -> str:
    to = t("email_cc", cc=card["email_cc"]) if card.get("email_cc") else ""
    box = ('<label class="lbl">%s%s<textarea name="text" rows="6" maxlength="%d">%s</textarea></label>'
           % (esc(t("draft")), (" · " + esc(to)) if to else "", db.MAX_REPLY_CHARS,
              esc(card.get("final_text") or card.get("draft_text"))))
    send = ('<button class="btn primary" type="button" data-action="send">%s</button>' % esc(t("send"))
            if allow_send else "")
    off = "" if allow_send else '<p class="note">%s</p>' % esc(t("send_off"))
    return ('%s<div class="actions">%s<button class="btn danger" type="button" data-action="discard">%s</button>%s'
            '</div>%s%s' % (box, send, esc(t("discard")), _revise_btn(), off, _revise_box()))


def _report_actions(card: Dict[str, Any], on_page: bool) -> str:
    rep, out = card.get("report") or {}, []
    steps = rep.get("next") or []
    if steps:
        picks = "".join('<button class="btn pick" type="button" data-pick="%d" aria-pressed="false">%s</button>'
                        % (i, esc(s)) for i, s in enumerate(steps))
        out.append('<div class="next"><p class="cap">%s</p><div class="actions">%s<button class="btn primary" '
                   'type="button" data-action="next" data-nextbtn disabled>%s</button></div></div>'
                   % (esc(t("next_steps")), picks, esc(t("next_start"))))
    acts = ['<button class="btn%s" type="button" data-action="seen">%s</button>'
            % ("" if steps else " primary", esc(t("seen")))]
    if rep.get("resume"):
        acts.append('<button class="btn" type="button" data-action="choose" data-option="resume">%s</button>'
                    % esc(t("resume")))
    acts.append(_revise_btn())
    if not on_page:
        acts.append('<a class="btn link" href="/reports/%s">%s</a>' % (quote(card["id"], safe=""), esc(t("full_report"))))
    out.append('<div class="actions">%s</div>%s' % ("".join(acts), _revise_box()))
    return "".join(out)


def card_html(card: Dict[str, Any], allow_send: bool, focus: bool = False, on_page: bool = False) -> str:
    """One card. A closed card shows what happened and has no answer buttons."""
    kind, rid, live = card["kind"], card["id"], is_open(card)
    title = plain(card.get("title"))
    if kind == "report" and not on_page:
        title = '<a href="/reports/%s">%s</a>' % (quote(rid, safe=""), title)
    tag = "h1" if on_page else "h2"
    parts = ['<p class="fmeta"><span class="ftag k-%s">%s</span><span>%s · %s</span></p>'
             % (esc(kind), esc(t("kind_" + kind)), esc(requester(card)), time_tag(card.get("created_at"))),
             '<%s class="ct">%s</%s>' % (tag, title, tag)]
    if card.get("context"):
        parts.append('<p class="ctx">%s</p>' % plain(card["context"]))
    if kind == "report":
        body = md(card.get("draft_text"))
        parts.append('<div class="body%s">%s</div>' % ("" if on_page or not live else " clip", body))
    elif (card.get("extra") or {}).get("body"):
        parts.append('<div class="body">%s</div>' % md(card["extra"]["body"]))
    if kind in db.REPLY_KINDS:
        parts.append(_original(card))
    parts.append(links_html(card) + files_html(card))
    if live:
        if kind == "decision":
            parts.append(_decision_actions(card))
        elif kind == "questions":
            parts.append(_questions_form(card))
        elif kind in db.REPLY_KINDS:
            parts.append(_reply_actions(card, allow_send))
        else:
            parts.append(_report_actions(card, on_page))
    else:
        if kind in db.REPLY_KINDS:
            parts.append('<div class="draft">%s</div>' % plain(card.get("final_text") or card.get("draft_text")))
        if kind == "questions" and card["status"] == "answered":
            parts.append(_answers_html(card))
        said = (card.get("result") or {}).get("instructions")
        if card["status"] == "revise" and said:
            parts.append('<blockquote class="said">%s</blockquote>' % plain(said))
        parts.append('<p class="outcome"><b>%s</b> · %s</p>' % (esc(outcome(card)), time_tag(card.get("decided_at"))))
    parts.append('<p class="card-msg" role="alert" hidden></p>')
    cls = "card k-%s%s%s" % (esc(kind), "" if live else " done", " focus" if focus else "")
    return ('<article class="%s" id="c-%s" data-id="%s"%s>%s</article>'
            % (cls, esc(rid), esc(rid), " data-stay" if on_page else "", "".join(parts)))


# -- pages ---------------------------------------------------------------------------------------------------------------
def _asset(name: str) -> str:
    try:
        v = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
    except OSError:
        v = "0"
    return "/static/%s?v=%s" % (name, v)


def layout(title: str, active: str, body: str, csrf: str, waiting: int, demo: bool, stamp: str = "") -> str:
    nav = []
    for href, key in (("/", "nav_pending"), ("/history", "nav_history"), ("/reports", "nav_reports")):
        cur = ' aria-current="page"' if href == active else ""
        badge = (' <span class="badge" data-count>%d</span>' % waiting) if href == "/" else ""
        nav.append('<a href="%s"%s>%s%s</a>' % (href, cur, esc(t(key)), badge))
    banner = '<p class="demo" role="note">%s</p>' % esc(t("demo_banner")) if demo else ""
    strings = json.dumps({k: t(k) for k in JS_KEYS}, ensure_ascii=False).replace("<", "\\u003c")
    return ('<!doctype html><html lang="%s"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark">'
            '<meta name="csrf-token" content="%s"><link rel="icon" href="data:,"><title>%s · Polar</title>'
            '<link rel="stylesheet" href="%s"><script src="%s" defer></script></head>'
            '<body data-v="%s"><a class="skip" href="#main">%s</a><header class="top"><a class="brand" href="/">Polar</a>'
            '<nav aria-label="%s">%s</nav></header>%s<main id="main">%s</main>'
            '<script type="application/json" id="ui-strings">%s</script></body></html>'
            % (esc(lang()), esc(csrf), esc(title), _asset("app.css"), _asset("app.js"), esc(stamp), esc(t("skip")),
               esc(t("nav_label")), "".join(nav), banner, body, strings))


def pending_page(cards: List[Dict[str, Any]], allow_send: bool, focus: Optional[Dict[str, Any]] = None) -> str:
    out = ['<h1 class="sr">%s</h1>' % esc(t("nav_pending"))]
    if focus is not None:
        if not is_open(focus):
            out.append('<p class="banner">%s</p>' % esc(t("closed_note")))
        out.append(card_html(focus, allow_send, focus=True, on_page=False))
        cards = [c for c in cards if c["id"] != focus["id"]]
    if cards:
        out.append('<div class="feed">%s</div>' % "".join(card_html(c, allow_send) for c in cards))
    out.append('<p class="empty"%s>%s</p>' % ("" if not cards and focus is None else " hidden", esc(t("empty"))))
    return "".join(out)


def _pager(base: str, page: int, more: bool, extra: Optional[Dict[str, str]] = None) -> str:
    def href(p: int) -> str:
        q = dict(extra or {})
        if p:
            q["page"] = str(p)
        return base + ("?" + urlencode(q) if q else "")
    links = []
    if page > 0:
        links.append('<a class="btn" href="%s" rel="prev">%s</a>' % (esc(href(page - 1)), esc(t("newer"))))
    if more:
        links.append('<a class="btn" href="%s" rel="next">%s</a>' % (esc(href(page + 1)), esc(t("older"))))
    return '<nav class="pager" aria-label="%s">%s</nav>' % (esc(t("pages")), "".join(links)) if links else ""


def _card_href(card: Dict[str, Any]) -> str:
    rid = quote(card["id"], safe="")
    return "/reports/%s" % rid if card["kind"] == "report" else "/?c=%s" % rid


def history_page(rows: List[Dict[str, Any]], page: int, more: bool) -> str:
    out = ['<h1>%s</h1>' % esc(t("nav_history"))]
    if not rows:
        out.append('<p class="empty">%s</p>' % esc(t("history_empty")))
    today, day, items = local(datetime.now(timezone.utc)).date(), None, []  # type: ignore[var-annotated]
    for r in rows:
        dt = local(parse(r.get("decided_at") or r["created_at"]) or datetime.now(timezone.utc))
        if dt.date() != day:
            if items:
                out.append('<ul class="rows">%s</ul>' % "".join(items))
                items = []
            day = dt.date()
            out.append('<h2 class="day">%s</h2>' % esc(day_label(day, today)))
        items.append('<li><a class="row" href="%s"><span class="ftag k-%s">%s</span><span class="rt">%s</span>'
                     '<span class="rd">%s</span><time datetime="%s">%s</time></a></li>'
                     % (esc(_card_href(r)), esc(r["kind"]), esc(t("kind_" + r["kind"])), plain(r.get("title")),
                        esc(outcome(r)), esc(r.get("decided_at")), esc(dt.strftime("%H:%M"))))
    if items:
        out.append('<ul class="rows">%s</ul>' % "".join(items))
    out.append(_pager("/history", page, more))
    return "".join(out)


def reports_page(rows: List[Dict[str, Any]], q: str, page: int, more: bool) -> str:
    out = ['<h1>%s</h1>' % esc(t("nav_reports")),
           '<form class="filters" role="search" action="/reports" method="get"><label class="sr" for="rq">%s</label>'
           '<input id="rq" type="search" name="q" value="%s" placeholder="%s" maxlength="200">'
           '<button class="btn" type="submit">%s</button></form>'
           % (esc(t("search")), esc(q), esc(t("search_hint")), esc(t("search")))]
    if not rows:
        out.append('<p class="empty">%s</p>' % esc(t("reports_none_q") if q else t("reports_empty")))
    items = []
    for r in rows:
        state_ = t("st_unseen") if is_open(r) else outcome(r)
        items.append('<li><a class="row" href="/reports/%s"><span class="rt">%s</span><span class="rd">%s · %s</span>'
                     '<time datetime="%s">%s</time></a></li>'
                     % (quote(r["id"], safe=""), plain(r.get("title")), esc(requester(r)), esc(state_),
                        esc(r.get("created_at")), esc(when(r.get("created_at")))))
    if items:
        out.append('<ul class="rows">%s</ul>' % "".join(items))
    out.append(_pager("/reports", page, more, {"q": q} if q else None))
    return "".join(out)


def report_page(card: Dict[str, Any], allow_send: bool) -> str:
    return ('<p class="crumb"><a href="/reports">%s</a></p>%s'
            % (esc(t("back_reports")), card_html(card, allow_send, on_page=True)))


def message_page(text: str) -> str:
    return '<h1>%s</h1><p><a href="/">%s</a></p>' % (esc(text), esc(t("nav_pending")))
