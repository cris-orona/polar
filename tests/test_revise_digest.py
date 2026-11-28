"""Weekly digest of the owner's corrections: real corrections only, one task with the full list in a file."""
import json

import pytest

from polar import db, revise_digest


@pytest.fixture(autouse=True)
def _data(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))


def _card(conn, title, result, days_ago=1, kind="report"):
    rid = db.create_request(conn, "report", title, "", "s1", draft_text="x")
    conn.execute("UPDATE requests SET kind=?, status='revise', result_json=?, "
                 "decided_at=strftime('%Y-%m-%dT%H:%M:%SZ','now',?) WHERE id=?",
                 (kind, json.dumps(result), "-%d days" % days_ago, rid))
    return rid


def test_only_real_corrections_in_the_window(conn):
    _card(conn, "A", {"instructions": "hazlo tú, no le preguntes a nadie"})
    _card(conn, "B", {"instructions": "Diseñar el informe", "via": "next"})   # a follow-up tap
    _card(conn, "C", {"instructions": "viejo"}, days_ago=9)
    _card(conn, "D", {"option": "go"})
    got = revise_digest.corrections(conn, 7)
    assert [g["title"] for g in got] == ["A"]
    assert "hazlo tú" in revise_digest.markdown(got, 7)


def test_start_posts_one_task_with_the_file(conn, monkeypatch, tmp_path):
    for i in range(3):
        _card(conn, "T%d" % i, {"instructions": "corrección %d " % i + "x" * 3000})
    sent = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true, "task": "t1"}'

    def fake(req, timeout):
        sent["url"], sent["body"], sent["auth"] = req.full_url, json.loads(req.data), req.headers["Authorization"]
        return Resp()

    monkeypatch.setattr(revise_digest.urllib.request, "urlopen", fake)
    out = revise_digest.run(7, send=True, conn=conn)
    assert out["started"] and out["count"] == 3
    assert sent["url"].endswith("/act") and sent["body"]["action"] == "task"
    assert sent["auth"].startswith("Bearer ")
    ctx = sent["body"]["context"]
    assert len(ctx) <= revise_digest.MAX_CONTEXT and "revise-digest" in ctx
    path = ctx.split(": ", 1)[1].split("\n", 1)[0]
    assert open(path, encoding="utf-8").read().count("corrección") == 3


def test_no_task_when_too_few(conn, monkeypatch):
    _card(conn, "A", {"instructions": "una sola"})
    monkeypatch.setattr(revise_digest, "start", lambda *a, **k: pytest.fail("must not start"))
    assert revise_digest.run(7, send=True, conn=conn)["started"] is False
