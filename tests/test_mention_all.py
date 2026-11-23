from gchat_mcp import mentions


def test_bare_all_becomes_a_token():
    assert mentions.normalize("hi @all, standup moved") == "hi @[all], standup moved"
    assert mentions.extract(mentions.normalize("@all please")) == ["all"]


def test_all_inside_an_email_or_word_is_left_alone():
    for t in ["mail me at ops@all.com", "see x@all", "call@all", "@allison hi", "@all.hands"]:
        assert mentions.normalize(t) == t, t


def test_bracket_all_and_everyone_are_all():
    assert mentions.is_all("all") and mentions.is_all(" Everyone ") and not mentions.is_all("Allison")


def test_insert_all():
    assert mentions.insert("@[all] hi", {"all": mentions.ALL}) == "<users/all> hi"


def test_missing_accepts_a_mention_all_annotation():
    msg = {"annotations": [{"type": "USER_MENTION", "userMention": {"user": {"name": "users/all"}, "type": "MENTION_ALL"}}]}
    assert mentions.missing(msg, [mentions.ALL]) == []
    assert mentions.missing({"annotations": []}, [mentions.ALL]) == [mentions.ALL]


def test_missing_accepts_the_live_shape_of_an_all_annotation():
    # Shape read back from Chat on 23 Nov 2026 for "hi @all".
    msg = {"text": "Live test 5: hi @all",
           "annotations": [{"type": "USER_MENTION", "startIndex": 16, "length": 4, "userMention": {"user": {}}}]}
    assert mentions.missing(msg, [mentions.ALL]) == []
    other = {"text": "Live test 5: hi @bob", "annotations": [{"type": "USER_MENTION", "startIndex": 16, "length": 4,
                                                              "userMention": {"user": {}}}]}
    assert mentions.missing(other, [mentions.ALL]) == [mentions.ALL]


# --- @all must not list members (Devin review on PR #2): big spaces would be refused by member_ids
import pytest

from gchat_mcp import chat, server
from tests.transport import ChatServer


@pytest.fixture
def srv(monkeypatch):
    s = ChatServer()
    monkeypatch.setattr(chat, "_acquire", lambda: (s, "grant-1"))
    chat._identities.clear()
    monkeypatch.setattr(chat, "label", lambda sp: "Team")
    monkeypatch.setattr(chat, "who", lambda u: "Me")
    return s


def _members_calls(srv):
    return [x for x in srv.log if x[1].endswith("/members")]


def test_at_all_in_a_space_with_over_1000_memberships_is_sent_without_listing_members(srv):
    srv.members = [{"member": {"name": f"users/{i}", "type": "HUMAN"}} for i in range(1200)]
    srv.page_size = 100
    out = server.send_message("AAA", "@all standup moved to 10")
    assert _members_calls(srv) == []
    assert "Mentions confirmed" not in out or "everyone" in out
    create = next(x for x in srv.log if x[0] == "POST" and x[1] == "v1/spaces/AAA/messages")
    assert create[3]["text"] == "<users/all> standup moved to 10"


def test_at_all_in_a_dm_is_still_refused_without_listing_members(srv):
    srv.space_type = "DIRECT_MESSAGE"
    with pytest.raises(Exception, match="not a DM"):
        server.send_message("AAA", "@all hi")
    assert _members_calls(srv) == []
    assert not any(x[0] == "POST" for x in srv.log)


def test_a_person_mention_beside_at_all_still_checks_members(srv):
    srv.profiles = {i: {"name": n, "email": f"{n.split()[0].lower()}@x.com"}
                    for i, n in {"111": "Me Me", "222": "Priya Shah", "333": "Rahul K", "444": "Dev Rao"}.items()}
    server._mentions("spaces/AAA", "@all and @[Priya Shah]")
    assert _members_calls(srv)
