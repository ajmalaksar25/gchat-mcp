"""Bare @Full Name mentions, quoting one message, and the footer the approval hook added."""
import types

import pytest

from gchat_mcp import chat, mentions, server
from tests.transport import ME, ChatServer


@pytest.fixture
def srv(monkeypatch):
    s = ChatServer()
    monkeypatch.setattr(chat, "_acquire", lambda: (s, "grant-1"))
    monkeypatch.setattr(chat, "time", types.SimpleNamespace(sleep=lambda t: None))
    chat._identities.clear()
    chat._people.clear()
    monkeypatch.setattr(chat, "label", lambda sp: "Team")
    monkeypatch.delenv("GCHAT_MCP_FOOTER", raising=False)
    s.profiles = {ME: {"name": "Sam Rivera", "email": "sam@x.com"}, "222": {"name": "Pratik Kodag", "email": "p@x.com"},
                  "333": {"name": "Priya Shah", "email": "priya@x.com"}, "444": {"name": "Noor Ali", "email": "n@x.com"}}
    yield s
    chat._people.clear()


def posted(srv):
    return [x for x in srv.log if x[0] == "POST" and x[1].endswith("/messages")]


def test_bare_full_name_becomes_a_real_mention(srv):
    server.send_message("AAA", "@Pratik Kodag I'm not that dumb")
    assert posted(srv)[-1][3]["text"] == "<users/222> I'm not that dumb"  # Chat renders <users/ID> as a mention


def test_bare_name_that_is_nobody_is_refused_before_sending(srv):
    with pytest.raises(mentions.NoMatch, match=r"'@Pratik' is not a mention.*Did you mean @\[Pratik Kodag\]"):
        server.send_message("AAA", "@Pratik hi")
    assert not posted(srv)


def test_escaped_at_and_emails_stay_plain(srv):
    server.send_message("AAA", "meet at 5 \\@ the cafe, mail sam@x.com")
    assert posted(srv)[-1][3]["text"] == "meet at 5 @ the cafe, mail sam@x.com"


def test_quote_one_message_in_its_thread(srv):
    srv.threads.add("spaces/AAA/threads/T1")
    srv.messages["spaces/AAA/messages/m1"] = {"name": "spaces/AAA/messages/m1", "createTime": "2026-11-27T06:00:00Z",
                                              "threadReply": True, "thread": {"name": "spaces/AAA/threads/T1"},
                                              "sender": {"name": "users/222", "type": "HUMAN"}, "text": "share the env"}
    out = server.send_message("AAA", "not that dumb", quote_message_id="m1")
    body, params = posted(srv)[-1][3], posted(srv)[-1][2]
    assert body["quotedMessageMetadata"] == {"name": "spaces/AAA/messages/m1", "quoteType": "REPLY",
                                             "lastUpdateTime": "2026-11-27T06:00:00Z"}
    assert body["thread"] == {"name": "spaces/AAA/threads/T1"} and params["messageReplyOption"] == "REPLY_MESSAGE_OR_FAIL"
    assert "Quoting `spaces/AAA/messages/m1`." in out


def test_footer_added_by_the_hook_is_reported(srv):
    out = server.send_message("AAA", "hello\n\n_Sent by a hook_", client_footer="_Sent by a hook_")
    assert "footer added: yes (by the approval hook)" in out
    assert "footer added: no" in server.send_message("AAA", "hello")
