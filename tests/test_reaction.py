import pytest

from gchat_mcp import auth, chat, server

MSG = "spaces/AAA/messages/m1.m1"


@pytest.fixture
def api(monkeypatch):
    calls, stored = [], []

    def post(path, call, params, body):
        calls.append((path, call, body))
        stored.append({"emoji": body["emoji"], "user": {"name": "users/111"}})
        return stored[-1]

    monkeypatch.setattr(auth, "can_react", lambda: True)
    monkeypatch.setattr(chat, "post", post)
    monkeypatch.setattr(chat, "me_id", lambda: "111")
    monkeypatch.setattr(chat, "reactions", lambda msg, user="": [r for r in stored if r["user"]["name"] == user])
    return calls, stored


def test_reaction_posts_the_unicode_emoji_and_confirms_it_is_mine(api):
    calls, _ = api
    assert server.add_reaction(MSG, "👍") == f"Reacted 👍 to `{MSG}`."
    assert calls == [(f"{MSG}/reactions", "reactions.create", {"emoji": {"unicode": "👍"}})]


def test_reaction_missing_from_read_back_is_reported(api, monkeypatch):
    other = {"emoji": {"unicode": "🎉"}, "user": {"name": "users/111"}}
    monkeypatch.setattr(chat, "reactions", lambda msg, user="": [other])  # read-back lacks the 👍
    assert "does not show it as mine" in server.add_reaction(MSG, "👍")


@pytest.mark.parametrize("message_id", ["spaces/AAA", "AAA/messages/m1", "spaces/AAA/messages/"])
def test_bad_message_id_is_refused_before_any_call(api, message_id):
    with pytest.raises(ValueError, match="Not a message id"):
        server.add_reaction(message_id, "👍")
    assert api[0] == []


@pytest.mark.parametrize("emoji", ["", "ok", ":thumbsup:"])
def test_text_is_not_sent_as_an_emoji(api, emoji):
    with pytest.raises(ValueError, match="Not a Unicode emoji"):
        server.add_reaction(MSG, emoji)
    assert api[0] == []


def test_sign_in_without_the_scope_says_how_to_grant_it(api, monkeypatch):
    monkeypatch.setattr(auth, "can_react", lambda: False)
    with pytest.raises(auth.AuthError, match="gchat-mcp login"):
        server.add_reaction(MSG, "👍")
    assert api[0] == []
