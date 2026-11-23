import pytest

from gchat_mcp import chat, server

MSG = "spaces/AAA/messages/m1.m1"


@pytest.fixture
def api(monkeypatch):
    calls, stored = [], []

    def post(path, call, params, body):
        calls.append(("POST", path, call, body))
        stored.append({"name": f"{MSG}/reactions/r{len(stored)}", "emoji": body["emoji"], "user": {"name": "users/111"}})
        return stored[-1]

    def delete(path, call, params=None):
        calls.append(("DELETE", path, call, None))
        stored[:] = [r for r in stored if r["name"] != path]

    monkeypatch.setattr(chat, "post", post)
    monkeypatch.setattr(chat, "delete", delete)
    monkeypatch.setattr(chat, "me_id", lambda: "111")
    monkeypatch.setattr(chat, "reactions", lambda msg, user="", cap=25: [r for r in stored if r["user"]["name"] == user])
    return calls, stored


def test_reaction_posts_the_unicode_emoji_and_confirms_it_is_mine(api):
    calls, _ = api
    assert server.add_reaction(MSG, "👍") == f"Reacted 👍 to `{MSG}`."
    assert calls == [("POST", f"{MSG}/reactions", "reactions.create", {"emoji": {"unicode": "👍"}})]


def test_shortcodes_and_keycaps_become_unicode(api):
    calls, _ = api
    server.react(MSG, ":thumbsup:")
    server.react(MSG, "eyes")
    server.react(MSG, "1")
    assert [c[3]["emoji"]["unicode"] for c in calls] == ["👍", "👀", "1️⃣"]


def test_short_id_needs_a_conversation(api):
    calls, _ = api
    server.react("m1.m1", "👍", conversation_id="AAA")
    assert calls[0][1] == f"{MSG}/reactions"
    with pytest.raises(ValueError, match="Not a message id"):
        server.react("m1.m1", "👍")


def test_reaction_missing_from_read_back_is_reported(api, monkeypatch):
    other = {"name": "x", "emoji": {"unicode": "🎉"}, "user": {"name": "users/111"}}
    monkeypatch.setattr(chat, "reactions", lambda msg, user="", cap=25: [other])  # read-back lacks the 👍
    assert "does not show it as mine" in server.add_reaction(MSG, "👍")


def test_variation_selector_difference_still_counts_as_mine(api, monkeypatch):
    monkeypatch.setattr(chat, "reactions", lambda msg, user="", cap=25: [
        {"name": "x", "emoji": {"unicode": "❤"}, "user": {"name": "users/111"}}])  # Chat dropped the VS16
    assert server.react(MSG, "❤️").startswith("Reacted")


def test_remove_deletes_only_my_matching_reaction(api):
    calls, stored = api
    server.react(MSG, "👍")
    server.react(MSG, "🎉")
    out = server.react(MSG, ":thumbsup:", remove=True)
    assert out == f"Removed 👍 from `{MSG}`."
    assert [c[:3] for c in calls if c[0] == "DELETE"] == [("DELETE", f"{MSG}/reactions/r0", "reactions.delete")]
    assert [r["emoji"]["unicode"] for r in stored] == ["🎉"]


def test_remove_without_a_reaction_calls_nothing(api):
    calls, _ = api
    assert "nothing to remove" in server.react(MSG, "👍", remove=True)
    assert calls == []


def test_duplicate_reaction_is_reported_not_raised(api, monkeypatch):
    def dup(*a):
        raise chat.ApiError("reactions.create", 409, "already exists")
    monkeypatch.setattr(chat, "post", dup)
    assert "already have 👍" in server.react(MSG, "👍")


@pytest.mark.parametrize("message_id", ["spaces/AAA", "AAA/messages/m1", "spaces/AAA/messages/"])
def test_bad_message_id_is_refused_before_any_call(api, message_id):
    with pytest.raises(ValueError, match="Not a message id"):
        server.add_reaction(message_id, "👍")
    assert api[0] == []


@pytest.mark.parametrize("emoji", ["", "ok", "notanemoji"])
def test_text_is_not_sent_as_an_emoji(api, emoji):
    with pytest.raises(ValueError, match="Not a Unicode emoji"):
        server.add_reaction(MSG, emoji)
    assert api[0] == []


def test_missing_scope_403_says_how_to_grant_it(api, monkeypatch):
    def denied(*a):
        raise chat.ApiError("reactions.create", 403, "Request had insufficient authentication scopes.",
                            "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    monkeypatch.setattr(chat, "post", denied)
    with pytest.raises(server.auth.AuthError, match="gchat-mcp login"):
        server.react(MSG, "👍")


def test_other_403_is_not_blamed_on_scopes(api, monkeypatch):
    def denied(*a):
        raise chat.ApiError("reactions.create", 403, "The caller does not have permission", "")
    monkeypatch.setattr(chat, "post", denied)
    with pytest.raises(chat.ApiError, match="does not have permission"):
        server.react(MSG, "👍")
