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
