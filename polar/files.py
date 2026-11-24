"""Files on a Slack message (images, PDFs, sheets): save them next to the database so a session can read them.

A message with a file reaches the app as a `message` event with subtype `file_share` and a `files` list.
Polar downloads each file with the bot token (scope `files:read`), then with the user token: the bot is
not a member of the channels the owner forwards from or tags Polar in. Files land in
`data/files/<key>/<name>`; the key is the message ts (or the file id for `polar files <F…>`).
`polar files <key>` prints the paths. The laptop wrapper copies them to the laptop first."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from polar import db

log = logging.getLogger("polar")

MAX_BYTES = 100 * 1024 * 1024
MAX_FILES = 20
KEEP_DAYS = 30
TIMEOUT = 60
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.]{0,39}$")  # a message ts or a file id: safe as a folder name
FILE_ID = re.compile(r"^F[A-Z0-9]{6,}$")
GONE = ("tombstone", "hidden_by_limit")
NO_ACCESS = "Slack no dio acceso (falta el permiso files:read)"

# fetch(file, dest) saves one Slack file object at dest. It returns None, or the reason it failed.
Fetch = Callable[[Dict[str, Any], Path], Optional[str]]


def files_dir() -> Path:
    return db.data_dir() / "files"


def message_files(ev: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The file objects of one message event: its own files, then the files of forwarded messages."""
    found = list(ev.get("files") or [])
    for a in ev.get("attachments") or []:
        found += a.get("files") or []
    out, seen = [], set()
    for f in found:
        fid = f.get("id") if isinstance(f, dict) else None
        if fid and fid not in seen:
            seen.add(fid)
            out.append(f)
    return out


def safe_name(f: Dict[str, Any]) -> str:
    raw = os.path.basename(str(f.get("name") or f.get("title") or "").replace("\\", "/"))
    name = re.sub(r"[^\w.\-() ]", "_", raw).strip(" .")[:120]
    if not name:
        name = str(f.get("id") or "archivo")
        if f.get("filetype"):
            name += "." + re.sub(r"\W", "", str(f["filetype"]))
    return name


def _stem(name: str) -> str:
    return os.path.splitext(name)[0].lower()


def _slack_https(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    return u.scheme == "https" and (host == "slack.com" or host.endswith(".slack.com"))


def human_size(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024 / 1024)
    return "%d KB" % max(1, round(n / 1024))


def _int(n: Any) -> int:
    try:
        return int(n or 0)
    except (TypeError, ValueError):
        return 0


def sweep(now: Optional[float] = None) -> None:
    """Delete file folders older than KEEP_DAYS."""
    base = files_dir()
    if not base.is_dir():
        return
    limit = (now or time.time()) - KEEP_DAYS * 86400
    for d in base.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < limit:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def to_jpeg(path: Path) -> Path:
    """An iPhone HEIC photo becomes a JPEG (macOS `sips`): a session cannot read HEIC. Keep the file on failure."""
    if path.suffix.lower() not in (".heic", ".heif"):
        return path
    out = path.with_suffix(".jpg")
    try:
        r = subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg", str(path), "--out", str(out)],
                           capture_output=True, timeout=30)
        if r.returncode == 0 and out.is_file():
            path.unlink()
            return out
    except Exception:
        pass
    return path


def save(found: List[Dict[str, Any]], key: str, fetch: Optional[Fetch] = None) -> List[Dict[str, Any]]:
    """Download the files into data/files/<key>/. One row per file: id, name, mimetype, size, and `path`
    or `error` (external files carry `url`)."""
    if not KEY.match(key or ""):
        raise ValueError("bad files key")
    fetch = fetch or download
    folder = files_dir() / key
    rows: List[Dict[str, Any]] = []
    used = set()
    for f in found[:MAX_FILES]:
        row: Dict[str, Any] = {"id": f.get("id"), "name": safe_name(f), "mimetype": f.get("mimetype") or "",
                               "size": f.get("size") or 0}
        rows.append(row)
        if f.get("mode") in GONE:
            row["error"] = "ya no está en Slack"
            continue
        if f.get("is_external") or f.get("mode") == "external":
            row["url"] = f.get("url_private") or f.get("permalink") or ""
            row["error"] = "es un enlace externo"
            continue
        if _int(f.get("size")) > MAX_BYTES:
            row["error"] = "pesa más de %d MB" % (MAX_BYTES // 1024 // 1024)
            continue
        if _stem(row["name"]) in used:  # macOS names ignore case; a HEIC becomes <stem>.jpg
            row["name"] = "%s-%s" % (f.get("id"), row["name"])
        used.add(_stem(row["name"]))
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / row["name"]
        try:
            err = fetch(f, dest)
        except Exception as exc:  # never the request data: it holds the token
            err = type(exc).__name__
        if err or not dest.is_file():
            row["error"] = err or "no se guardó"
            continue
        dest = to_jpeg(dest)
        row["name"], row["path"] = dest.name, str(dest)
    if len(found) > MAX_FILES:
        rows.append({"id": None, "name": "%d archivos más" % (len(found) - MAX_FILES), "mimetype": "", "size": 0,
                     "error": "solo bajo los primeros %d" % MAX_FILES})
    sweep()
    return rows


def describe(rows: List[Dict[str, Any]], key: str) -> str:
    """The lines a session reads at the end of the task text."""
    if not rows:
        return ""

    def label(r: Dict[str, Any]) -> str:
        extra = ", ".join(x for x in (r.get("mimetype"), human_size(r.get("size")) if r.get("size") else "") if x)
        return r["name"] + (" (%s)" % extra if extra else "")

    ok = [r for r in rows if r.get("path")]
    bad = [r for r in rows if not r.get("path")]
    parts = []
    if ok:
        parts.append("Archivos adjuntos del mensaje (%d): %s. Para abrirlos ejecuta `polar files %s`: imprime la "
                     "ruta local de cada uno; léelos con Read (las imágenes también)." % (
                         len(ok), "; ".join(label(r) for r in ok), key))
    if bad:
        parts.append("Archivos adjuntos que no pude bajar (%d): %s." % (len(bad), "; ".join(
            "%s: %s%s" % (label(r), r.get("error") or "error", (" " + r["url"]) if r.get("url") else "")
            for r in bad)))
    return " ".join(parts)


def listing(key: str) -> Optional[List[Dict[str, Any]]]:
    """The saved files of one key, or None when the folder does not exist."""
    if not KEY.match(key or ""):
        return None
    folder = files_dir() / key
    if not folder.is_dir():
        return None
    return [{"name": p.name, "path": str(p), "size": p.stat().st_size}
            for p in sorted(folder.iterdir()) if p.is_file() and not p.name.endswith(".part")]


# -- Slack download (not used in tests) ---------------------------------------------
def _tokens() -> List[str]:
    from polar.tokens import get_token
    return [t for t in (get_token("bot"), get_token("user")) if t]


class _SlackOnly(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but send the token only to Slack hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _slack_https(newurl):
            new.remove_header("Authorization")
        return new


def http_get(url: str, token: str, dest: Path, mimetype: str = "") -> Optional[str]:
    if not _slack_https(url):
        return "enlace fuera de Slack"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    part = dest.with_name("%s.%s.part" % (dest.name, uuid.uuid4().hex[:8]))  # one per download: two events may race
    try:
        with urllib.request.build_opener(_SlackOnly).open(req, timeout=TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype and "html" not in (mimetype or "").lower():
                return NO_ACCESS  # Slack answers with its sign-in page when the token may not read the file
            size = 0
            with open(part, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return "pesa más de %d MB" % (MAX_BYTES // 1024 // 1024)
                    out.write(chunk)
        os.replace(part, dest)
        return None
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return NO_ACCESS
        return "Slack respondió %s" % code if code else type(exc).__name__
    finally:
        if part.exists():
            part.unlink()


def file_info(file_id: str) -> Optional[Dict[str, Any]]:
    """The full file object from `files.info`: user token first (it sees every channel of the owner)."""
    from slack_sdk import WebClient
    for token in reversed(_tokens()):
        try:
            return WebClient(token=token).files_info(file=file_id)["file"]
        except Exception:
            continue
    return None


def download(f: Dict[str, Any], dest: Path) -> Optional[str]:
    url = f.get("url_private_download") or f.get("url_private")
    if not url and f.get("id"):  # `file_access: check_file_info`: the event carries no link
        full = file_info(f["id"]) or {}
        url = full.get("url_private_download") or full.get("url_private")
    if not url:
        return "sin enlace de descarga"
    err: Optional[str] = "sin token"
    for token in _tokens():
        err = http_get(url, token, dest, f.get("mimetype") or "")
        if not err:
            return None
    return err


def fetch_by_id(file_id: str, fetch: Optional[Fetch] = None,
                info: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None) -> Optional[List[Dict[str, Any]]]:
    """`polar files <F…>`: download one file by id (a file of a thread that `polar thread` listed)."""
    if not FILE_ID.match(file_id or ""):
        return None
    f = (info or file_info)(file_id)
    if not f:
        return None
    return save([f], file_id, fetch)
