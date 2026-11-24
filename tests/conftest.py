import pytest

from polar import db
from polar.core import Polar

OWNER = "U0000000000"


@pytest.fixture(autouse=True)
def _no_real_alarm(monkeypatch):
    """A test never runs a real alarm command. test_alarm.py sets a fake one."""
    monkeypatch.delenv("POLAR_ALARM_BIN", raising=False)


class FakeClient:
    def __init__(self, name):
        self.name = name
        self.calls = []
        self.fail = None
        self.views_fail = False    # views_open raises (an expired trigger_id)

    def _rec(self, method, **kw):
        self.calls.append((method, kw))
        if self.fail == method:
            raise RuntimeError("boom xoxp-secret")

    def conversations_open(self, **kw):
        self._rec("conversations_open", **kw)
        return {"channel": {"id": "D1"}}

    def chat_postMessage(self, **kw):
        self._rec("chat_postMessage", **kw)
        n = len([c for c in self.calls if c[0] == "chat_postMessage"])
        return {"ts": f"100.{n}", "channel": kw["channel"]}

    def chat_update(self, **kw):
        self._rec("chat_update", **kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self._rec("chat_postEphemeral", **kw)
        return {"ok": True}

    def chat_delete(self, **kw):
        self._rec("chat_delete", **kw)
        return {"ok": True}

    def chat_getPermalink(self, **kw):
        self._rec("chat_getPermalink", **kw)
        return {"permalink": "https://x.slack.com/p1"}

    def views_open(self, **kw):
        self._rec("views_open", **kw)
        if self.views_fail:
            raise RuntimeError("expired_trigger_id xoxp-secret")
        return {"ok": True}

    def of(self, method):
        return [kw for m, kw in self.calls if m == method]


class BotClient(FakeClient):
    """The bot client: it can also read the owner's DM (catch-up)."""

    def __init__(self, name):
        super().__init__(name)
        self.history_pages = [[]]  # conversations_history answers page by page (next_cursor = page number)
        self.replies = {}          # parent ts -> messages of conversations_replies

    def conversations_history(self, **kw):
        self._rec("conversations_history", **kw)
        i = int(kw.get("cursor") or 0)
        more = i + 1 < len(self.history_pages)
        return {"messages": self.history_pages[i],
                "response_metadata": {"next_cursor": str(i + 1) if more else ""}}

    def conversations_replies(self, **kw):
        self._rec("conversations_replies", **kw)
        return {"messages": self.replies.get(kw["ts"], [])}


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


@pytest.fixture
def bot():
    return BotClient("bot")


@pytest.fixture
def user():
    return FakeClient("user")


@pytest.fixture
def polar(conn, bot, user):
    return Polar(conn, bot, user, OWNER)


@pytest.fixture
def reply_id(conn):
    return db.create_request(conn, "reply", "Responder a Ana", "contexto", "sesion-1",
                             draft_text="Hola Ana", target_channel="C1", target_thread_ts="55.5")


@pytest.fixture
def decision_id(conn):
    return db.create_request(conn, "decision", "¿Release?", "lote 3", "sesion-2",
                             options=[{"key": "go", "label": "Go"}, {"key": "wait", "label": "Esperar"}])


@pytest.fixture(autouse=True)
def _no_real_token_files(monkeypatch, tmp_path_factory):
    # Keep tests away from a real ~/.config/polar.
    monkeypatch.setenv("POLAR_TOKEN_DIR", str(tmp_path_factory.mktemp("no-tokens")))


@pytest.fixture(autouse=True)
def _no_ambient_session(monkeypatch):
    # The CLI must not find the session of the agent that runs the tests.
    from polar import cli
    for name in ("POLAR_SESSION_ID", "HERDR_PANE_ID", "TMUX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "_parent_pid", lambda pid: None)
    monkeypatch.setattr(cli.os, "getppid", lambda: 0)
