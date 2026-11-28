"""New Slack messages for the owner: DMs to him and mentions of him. Uses the user token on this host only.

Prints one JSON line per message (ts, channel, sender, short text, permalink). The token never leaves
this process, so a laptop script can call `polar inbox` over SSH without holding any Slack token."""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

NAME_TTL = 7 * 86400
NAME_RETRY = 600  # a failed lookup is not asked again for 10 minutes (the inbox loop runs every 20 s)
VTT_TIMEOUT = 15
_failed: Dict[str, float] = {}


def _names_path():
    from polar import db
    return db.data_dir() / "slack-users.json"


def user_name(client: Any, uid: Optional[str]) -> Optional[str]:
    """Real name of a Slack user. One id is asked at most once per 7 days (file cache). Never raises."""
    if not uid:
        return None
    try:
        path = _names_path()
        cache: Dict[str, Any] = {}
        try:
            cache = json.loads(path.read_text("utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        hit = cache.get(uid)
        now = time.time()
        if isinstance(hit, dict) and hit.get("name") and now - float(hit.get("at") or 0) < NAME_TTL:
            return hit["name"]
        if now - _failed.get(uid, 0) < NAME_RETRY:
            return None
        try:
            name = (client.users_info(user=uid)["user"].get("real_name") or "").strip()
        except Exception:
            name = ""
        if not name:
            _failed[uid] = now
            return None
        cache[uid] = {"name": name, "at": now}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
            tmp.replace(path)
        except Exception:
            pass
        return name
    except Exception:
        return None


def vtt_to_text(vtt: str) -> str:
    """Plain text of a WebVTT caption file: no header, no cue time lines, no leading «- », one space between lines."""
    out: List[str] = []
    for line in (vtt or "").replace("\ufeff", "").splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            out.append(line)
    return " ".join(out)


def download_vtt(url: str, token: str) -> str:
    """GET a caption file from files.slack.com with the user token. The token is never logged."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=VTT_TIMEOUT) as r:  # noqa: S310 (a Slack URL)
        return r.read().decode("utf-8", "replace")


def transcript_of(f: Dict[str, Any], token: str, fetch_vtt: Callable[[str, str], str] = download_vtt) -> Optional[str]:
    """Transcript of a Slack audio clip, or None. Never raises."""
    try:
        tr = f.get("transcription") or {}
        if tr.get("status") != "complete":
            return None
        if f.get("vtt") and token:
            try:
                text = vtt_to_text(fetch_vtt(f["vtt"], token))
                if text:
                    return text
            except Exception:
                pass
        prev = tr.get("preview") or {}
        text = (prev.get("content") or "").strip()
        if not text:
            return None
        return text + (" …" if prev.get("has_more") else "")
    except Exception:
        return None


def queries(owner: str) -> List[str]:
    # DMs and group DMs to the owner, then mentions of the owner anywhere.
    return ["to:me", f"<@{owner}>"]


def thread_of(match: Dict[str, Any]) -> Optional[str]:
    """thread_ts of a search match, read from its permalink; None for a top-level message."""
    link = match.get("permalink") or ""
    if "thread_ts=" not in link:
        return None
    return link.split("thread_ts=", 1)[1].split("&", 1)[0]


def owner_threads(client: Any, owner: str, after_day: str) -> Dict[str, set]:
    """Channel threads the owner wrote in since after_day: {channel_id: {thread_ts, ...}}. DMs are left out
    because `to:me` already covers them."""
    out: Dict[str, set] = {}
    resp = client.search_messages(query=f"from:<@{owner}> after:{after_day}", sort="timestamp", sort_dir="desc", count=100)
    for m in (resp.get("messages") or {}).get("matches") or []:
        ch = m.get("channel") or {}
        if ch.get("is_im") or ch.get("is_mpim") or not ch.get("id"):
            continue
        root = thread_of(m) or m.get("ts")
        out.setdefault(ch["id"], set()).add(root)
    return out


def fetch(client: Any, owner: str, since_ts: float, limit: int = 20, skip_users: Optional[set] = None,
          after_day: Optional[str] = None) -> List[Dict[str, Any]]:
    skip_users = skip_users or set()
    seen: Dict[str, Dict[str, Any]] = {}
    for q in queries(owner):
        resp = client.search_messages(query=q, sort="timestamp", sort_dir="desc", count=limit)
        for m in (resp.get("messages") or {}).get("matches") or []:
            ts = m.get("ts") or ""
            try:
                if float(ts) <= since_ts:
                    continue
            except ValueError:
                continue
            if m.get("user") == owner:
                continue  # the owner's own messages
            if m.get("user") in skip_users:
                continue  # Polar's own DMs to the owner (search results carry no bot_id)
            ch = m.get("channel") or {}
            key = f"{ch.get('id')}:{ts}"
            seen[key] = {
                "ts": ts,
                "channel": ch.get("id"),
                "channel_name": ch.get("name"),
                "is_dm": bool(ch.get("is_im") or ch.get("is_mpim")),
                "sender": m.get("username") or m.get("user"),
                "sender_id": m.get("user"),
                "text": (m.get("text") or "")[:200],
                "permalink": m.get("permalink"),
            }
    if after_day:
        threads = owner_threads(client, owner, after_day)
        if threads:
            q = " ".join(f"in:<#{c}>" for c in threads) + f" after:{after_day}"
            resp = client.search_messages(query=q, sort="timestamp", sort_dir="desc", count=100)
            for m in (resp.get("messages") or {}).get("matches") or []:
                ch = (m.get("channel") or {}).get("id")
                root = thread_of(m)
                ts = m.get("ts") or ""
                if not root or root not in threads.get(ch, set()):
                    continue  # only replies inside a thread the owner is part of
                try:
                    if float(ts) <= since_ts:
                        continue
                except ValueError:
                    continue
                if m.get("user") == owner or m.get("user") in skip_users:
                    continue
                key = f"{ch}:{ts}"
                if key not in seen:
                    seen[key] = {
                        "ts": ts, "channel": ch, "channel_name": (m.get("channel") or {}).get("name"),
                        "is_dm": False, "thread_ts": root, "sender": m.get("username") or m.get("user"),
                        "sender_id": m.get("user"),
                        "text": (m.get("text") or "")[:200], "permalink": m.get("permalink"),
                    }
    names: Dict[str, Optional[str]] = {}
    for r in seen.values():
        uid = r.get("sender_id")
        if uid:
            if uid not in names:
                names[uid] = user_name(client, uid)
            if names[uid]:
                r["sender_name"] = names[uid]
    return sorted(seen.values(), key=lambda r: float(r["ts"]))


def run(since_ts: float, owner: Optional[str] = None) -> List[Dict[str, Any]]:
    from slack_sdk import WebClient
    from polar.app import owner_id
    from polar.tokens import get_token

    token = get_token("user")
    if not token:
        raise RuntimeError("user token missing")
    skip = set()
    bot = get_token("bot")
    if bot:
        try:
            skip.add(WebClient(token=bot).auth_test()["user_id"])
        except Exception:
            pass
    from datetime import date, timedelta
    after_day = (date.today() - timedelta(days=2)).isoformat()
    return fetch(WebClient(token=token), owner or owner_id() or "", since_ts, skip_users=skip, after_day=after_day)


def thread_messages(client: Any, channel: str, thread_ts: str, base_url: str = "",
                    channel_name: Optional[str] = None,
                    fetch_vtt: Callable[[str, str], str] = download_vtt) -> List[Dict[str, Any]]:
    """Every message of one Slack thread, with the same fields as `polar inbox` (text is not clipped)."""
    out: List[Dict[str, Any]] = []
    cursor = None
    for _ in range(20):  # 20 pages of 200 is more than any thread
        kw: Dict[str, Any] = {"channel": channel, "ts": thread_ts, "limit": 200}
        if cursor:
            kw["cursor"] = cursor
        resp = client.conversations_replies(**kw)
        for m in resp.get("messages") or []:
            ts = m.get("ts") or ""
            link = ""
            if base_url:
                link = "%sarchives/%s/p%s" % (base_url, channel, ts.replace(".", ""))
                if ts != thread_ts:
                    link += "?thread_ts=%s&cid=%s" % (thread_ts, channel)
            row = {
                "ts": ts, "channel": channel, "channel_name": channel_name,
                "is_dm": channel.startswith("D"), "thread_ts": m.get("thread_ts") or thread_ts,
                "sender": m.get("username") or m.get("user"),
                "sender_id": m.get("user"),
                "text": m.get("text") or "", "permalink": link or None,
            }
            sname = user_name(client, m.get("user"))
            if sname:
                row["sender_name"] = sname
            token = str(getattr(client, "token", "") or "")
            attached = []
            for f in m.get("files") or []:
                if not f.get("id"):
                    continue
                item = {"id": f.get("id"), "name": f.get("name"), "mimetype": f.get("mimetype")}
                tx = transcript_of(f, token, fetch_vtt)
                if tx:
                    item["transcript"] = tx
                attached.append(item)
            if attached:
                row["files"] = attached  # download one with `polar files <id>`
            out.append(row)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def run_thread(channel: str, thread_ts: str) -> List[Dict[str, Any]]:
    from slack_sdk import WebClient
    from polar.tokens import get_token

    token = get_token("user")
    if not token:
        raise RuntimeError("user token missing")
    client = WebClient(token=token)
    base = ""
    try:
        base = client.auth_test().get("url") or ""
    except Exception:
        pass
    name = None
    if not channel.startswith("D"):
        try:
            name = client.conversations_info(channel=channel)["channel"].get("name")
        except Exception:
            pass
    return thread_messages(client, channel, thread_ts, base, name)
