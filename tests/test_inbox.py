from polar import inbox


class FakeClient:
    def __init__(self, by_query):
        self.by_query = by_query
        self.calls = []

    def search_messages(self, **kw):
        self.calls.append(kw["query"])
        return {"messages": {"matches": self.by_query.get(kw["query"], [])}}


def m(ts, user="U1", ch="D1", im=True, text="hola"):
    return {"ts": ts, "user": user, "username": "ben", "text": text,
            "channel": {"id": ch, "name": "x", "is_im": im}, "permalink": "https://x/p"}


def test_new_only_no_owner_dedup_sorted():
    owner = "UOWN"
    c = FakeClient({
        "to:me": [m("200.1"), m("100.0"), m("150.0", user=owner)],
        f"<@{owner}>": [m("200.1"), m("180.0", ch="C9", im=False)],
    })
    out = inbox.fetch(c, owner, since_ts=120.0)
    assert [r["ts"] for r in out] == ["180.0", "200.1"]
    assert out[0]["is_dm"] is False and out[1]["is_dm"] is True
    assert c.calls == ["to:me", f"<@{owner}>"]


def test_text_is_short():
    c = FakeClient({"to:me": [m("5.0", text="x" * 500)]})
    assert len(inbox.fetch(c, "UOWN", 0.0)[0]["text"]) == 200


def test_skips_polar_bot_dms():
    polar_msg = m("9.0", user="UPOLAR")
    c = FakeClient({"to:me": [polar_msg, m("10.0")]})
    assert [r["ts"] for r in inbox.fetch(c, "UOWN", 0.0, skip_users={"UPOLAR"})] == ["10.0"]


def test_replies_in_owner_threads():
    owner = "UOWN"
    th = "https://x/archives/C1/p2?thread_ts=100.0"
    mine = {"ts": "150.0", "user": owner, "channel": {"id": "C1", "name": "ops"}, "permalink": th}
    reply = {"ts": "160.0", "user": "ULEO", "username": "leo", "text": "no me dejó",
             "channel": {"id": "C1", "name": "ops"}, "permalink": th}
    other_thread = {"ts": "170.0", "user": "UX", "channel": {"id": "C1", "name": "ops"},
                    "permalink": "https://x/archives/C1/p3?thread_ts=999.0"}
    top_level = {"ts": "171.0", "user": "UX", "channel": {"id": "C1", "name": "ops"}, "permalink": "https://x/archives/C1/p4"}
    c = FakeClient({f"from:<@{owner}> after:2026-11-16": [mine],
                    "in:<#C1> after:2026-11-16": [top_level, other_thread, reply, mine]})
    out = inbox.fetch(c, owner, since_ts=155.0, after_day="2026-11-16")
    assert [(r["ts"], r["sender"], r["thread_ts"]) for r in out] == [("160.0", "leo", "100.0")]



# --- sender names, transcripts ---

import pytest


@pytest.fixture
def datadir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_DATA_DIR", str(tmp_path / "data"))
    inbox._failed.clear()
    return tmp_path


TOK = "xoxp-" + "t" * 8


class UsersClient(FakeClient):
    token = TOK

    def __init__(self, by_query=None, replies=None, fail=False):
        super().__init__(by_query or {})
        self.replies = replies or []
        self.fail = fail
        self.user_calls = []

    def users_info(self, user):
        self.user_calls.append(user)
        if self.fail:
            raise RuntimeError("boom")
        return {"user": {"real_name": "Sam Sample"}}

    def conversations_replies(self, **kw):
        return {"messages": self.replies}


def test_fetch_has_sender_id_and_name_cached(datadir):
    c = UsersClient({"to:me": [m("5.0"), m("6.0", ch="D2")]})
    rows = inbox.fetch(c, "UOWN", 0)
    assert [r["sender_id"] for r in rows] == ["U1", "U1"]
    assert rows[0]["sender_name"] == "Sam Sample"
    assert c.user_calls == ["U1"]
    inbox.fetch(c, "UOWN", 0)
    assert c.user_calls == ["U1"]  # second fetch: no second users_info


def test_fetch_users_info_error_leaves_name_out(datadir):
    rows = inbox.fetch(UsersClient({"to:me": [m("5.0")]}, fail=True), "UOWN", 0)
    assert "sender_name" not in rows[0] and rows[0]["sender_id"] == "U1"


def test_fetch_client_without_users_info(datadir):
    rows = inbox.fetch(FakeClient({"to:me": [m("5.0")]}), "UOWN", 0)
    assert "sender_name" not in rows[0]


def test_thread_sender_fields_and_cache(datadir):
    c = UsersClient(replies=[{"ts": "1.0", "user": "U7", "text": "a"}, {"ts": "2.0", "user": "U7", "text": "b"}])
    rows = inbox.thread_messages(c, "C1", "1.0")
    assert rows[0]["sender"] == "U7" and rows[0]["sender_id"] == "U7" and rows[0]["sender_name"] == "Sam Sample"
    assert c.user_calls == ["U7"]
    inbox.thread_messages(c, "C1", "1.0")
    assert c.user_calls == ["U7"]


def test_thread_users_info_error_tolerated(datadir):
    c = UsersClient(replies=[{"ts": "1.0", "user": "U7", "text": "a"}], fail=True)
    rows = inbox.thread_messages(c, "C1", "1.0")
    assert "sender_name" not in rows[0] and rows[0]["text"] == "a"


def audio(status="complete", vtt="https://files.slack.com/x.vtt", has_more=True):
    return {"id": "F1", "name": "clip.m4a", "mimetype": "audio/mp4", "subtype": "slack_audio", "vtt": vtt,
            "transcription": {"status": status, "locale": "es-US",
                              "preview": {"content": "hola que tal", "has_more": has_more}}}


def thread_with(f, fetch):
    c = UsersClient(replies=[{"ts": "1.0", "user": "U7", "text": "", "files": [f]}])
    return inbox.thread_messages(c, "C1", "1.0", fetch_vtt=fetch)[0]["files"][0]


def test_vtt_to_text_with_bom():
    vtt = "\ufeffWEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n- hola que tal\n\n00:00:02.000 --> 00:00:04.000\nnos vemos\n"
    assert inbox.vtt_to_text(vtt) == "1 hola que tal nos vemos"


def test_vtt_to_text_plain():
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n- hola\n- que tal\n"
    assert inbox.vtt_to_text(vtt) == "hola que tal"
    assert inbox.vtt_to_text("") == ""


def test_transcript_from_vtt(datadir):
    seen = []

    def fetch(url, token):
        seen.append((url, token))
        return "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\ntexto completo\n"
    f = thread_with(audio(), fetch)
    assert f["transcript"] == "texto completo" and f["id"] == "F1"
    assert seen == [("https://files.slack.com/x.vtt", TOK)]


def test_transcript_falls_back_to_preview(datadir):
    def fetch(url, token):
        raise OSError("down")
    assert thread_with(audio(), fetch)["transcript"] == "hola que tal …"
    assert thread_with(audio(has_more=False), fetch)["transcript"] == "hola que tal"
    assert thread_with(audio(vtt=None), fetch)["transcript"] == "hola que tal …"


def test_no_transcript_when_status_none(datadir):
    def fetch(url, token):
        raise AssertionError("must not download")
    assert "transcript" not in thread_with(audio(status="none"), fetch)
