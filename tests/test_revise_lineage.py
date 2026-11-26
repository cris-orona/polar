import json
import subprocess

import pytest

from polar import db

OLD = "aaaaaaaa-1111-4111-8111-111111111111"
NEW = "bbbbbbbb-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    db._lineage_cache.clear()
    yield
    db._lineage_cache.clear()


def revise(conn, via=None, requester=OLD + " task-x"):
    rid = db.create_request(conn, "report", "R", "", requester, draft_text="x")
    res = {"instructions": "sigue"}
    if via:
        res["via"] = via
    conn.execute("UPDATE requests SET status='revise', decided_at=?, result_json=? WHERE id=?",
                 ("2099-01-01T00:00:00Z" if False else db.now_iso(), json.dumps(res), rid))
    conn.execute("UPDATE requests SET created_at='2000-01-01T00:00:00Z' WHERE id=?", (rid,))
    return rid


def later(conn, requester, silent=False):
    rid = db.create_request(conn, "report", "N", "", requester, draft_text="y", silent=silent)
    conn.execute("UPDATE requests SET created_at='2099-01-01T00:00:00Z' WHERE id=?", (rid,))
    return rid


def ids(conn):
    return [r["id"] for r in db.list_requests(conn, "open")]


def fake_bus(monkeypatch, chain=None, fail=False):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if fail:
            raise subprocess.TimeoutExpired(argv, 5)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(chain or [argv[-1]]), stderr="")
    monkeypatch.setattr(db.subprocess, "run", run)
    return calls


def test_successor_card_closes_the_old_card(conn, monkeypatch):
    calls = fake_bus(monkeypatch, [OLD, NEW])
    rid = revise(conn)
    assert ids(conn) == [rid]
    later(conn, NEW + " task-y")
    assert ids(conn) == [d for d in ids(conn) if d != rid] and rid not in ids(conn)
    ids(conn)
    assert len(calls) == 1  # cached


def test_unrelated_session_does_not_close(conn, monkeypatch):
    fake_bus(monkeypatch, [OLD, NEW])
    rid = revise(conn)
    later(conn, "cccccccc-3333-4333-8333-333333333333 task-z")
    assert rid in ids(conn)


def test_bus_failure_keeps_today_rule(conn, monkeypatch):
    fake_bus(monkeypatch, fail=True)
    rid = revise(conn)
    later(conn, NEW + " task-y")
    assert rid in ids(conn)
    later(conn, OLD + " task-x")  # same session still closes it (SQL rule)
    assert rid not in ids(conn)


def test_silent_card_counts_only_for_next(conn, monkeypatch):
    fake_bus(monkeypatch, [OLD, NEW])
    plain, follow = revise(conn), revise(conn, via="next")
    later(conn, NEW + " task-y", silent=True)
    open_ids = ids(conn)
    assert plain in open_ids and follow not in open_ids


def test_one_failure_skips_lineage_for_the_rest_of_the_call(conn, monkeypatch):
    calls = fake_bus(monkeypatch, fail=True)
    revise(conn, requester=OLD + " task-x")
    revise(conn, requester="cccccccc-3333-4333-8333-333333333333 task-x")
    revise(conn, requester="dddddddd-4444-4444-8444-444444444444 task-x")
    ids(conn)
    assert len(calls) == 1
