import re

from polar import app as polar_app
from polar import tokens
from polar.core import Polar


class FakeBolt:
    def __init__(self):
        self.actions, self.views, self.events = {}, {}, {}

    def event(self, key):
        def deco(fn):
            self.events[key] = fn
            return fn
        return deco

    def action(self, key):
        def deco(fn):
            self.actions[key] = fn
            return fn
        return deco

    def view(self, key):
        def deco(fn):
            self.views[key] = fn
            return fn
        return deco


def test_handlers_ack_and_route(polar, reply_id, user):
    bolt = FakeBolt()
    polar_app.register(bolt, polar)
    polar.deliver_pending()
    acked = []
    body = {"user": {"id": "U0000000000"}, "actions": [{"value": reply_id, "action_id": "polar_send"}]}
    bolt.actions["polar_send"](lambda *a: acked.append(a), body)
    assert acked == [()] and len(user.of("chat_postMessage")) == 1
    pat = [k for k in bolt.actions if isinstance(k, re.Pattern)][0]
    assert pat.match("polar_choose:go")


def test_keychain_then_env(monkeypatch):
    monkeypatch.setattr(tokens, "_keychain", lambda s: None)
    monkeypatch.setenv("POLAR_BOT_TOKEN", "xoxb-test")
    assert tokens.get_token("bot") == "xoxb-test"
    monkeypatch.setattr(tokens, "_keychain", lambda s: "xoxb-keychain")
    assert tokens.get_token("bot") == "xoxb-keychain"


def test_wrong_shape_is_missing(monkeypatch):
    monkeypatch.setattr(tokens, "_keychain", lambda s: None)
    monkeypatch.setenv("POLAR_USER_TOKEN", "xoxb-wrong")
    assert tokens.get_token("user") is None


def test_doctor_prints_no_token(monkeypatch, capsys):
    monkeypatch.setattr(tokens, "_keychain", lambda s: None)
    monkeypatch.setattr(polar_app, "get_token", lambda n: None)
    assert polar_app.doctor() == 1
    out = capsys.readouterr().out
    assert "no encontrado" in out and "xox" not in out


def test_private_file_between_keychain_and_env(monkeypatch, tmp_path):
    folder = tmp_path / "polar"
    folder.mkdir(mode=0o700)
    folder.chmod(0o700)
    f = folder / "bot-token"
    f.write_text("xoxb-file\n")
    f.chmod(0o600)
    monkeypatch.setenv("POLAR_TOKEN_DIR", str(folder))
    monkeypatch.setenv("POLAR_BOT_TOKEN", "xoxb-env")
    monkeypatch.setattr(tokens, "_keychain", lambda s: None)
    assert tokens.get_token("bot") == "xoxb-file"
    monkeypatch.setattr(tokens, "_keychain", lambda s: "xoxb-keychain")
    assert tokens.get_token("bot") == "xoxb-keychain"


def test_private_file_ignored_when_readable_by_others(monkeypatch, tmp_path):
    folder = tmp_path / "polar"
    folder.mkdir()
    folder.chmod(0o700)
    f = folder / "bot-token"
    f.write_text("xoxb-file")
    f.chmod(0o644)
    monkeypatch.setenv("POLAR_TOKEN_DIR", str(folder))
    monkeypatch.delenv("POLAR_BOT_TOKEN", raising=False)
    monkeypatch.setattr(tokens, "_keychain", lambda s: None)
    assert tokens.get_token("bot") is None


def test_message_event_and_pick_routes(polar, bot):
    bolt = FakeBolt()
    polar_app.register(bolt, polar)
    bolt.events["message"]({"channel": "D1", "channel_type": "im", "user": "U0000000000",
                            "text": "haz algo", "ts": "5.5"})
    assert polar.tasks.rows("task")[0]["text"] == "haz algo"
    bolt.events["message"]({"subtype": "message_changed"})  # a bad event does not raise
    acked = []
    bolt.actions["polar_pick_go"](lambda: acked.append(1), {"user": {"id": "U9"}, "actions": [
        {"value": "nope"}], "state": {}, "channel": {"id": "D1"}, "message": {"ts": "1.1"}})
    assert acked == [1]
