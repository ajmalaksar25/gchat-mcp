"""list_members, notification_settings and set_notifications against tests/transport.ChatServer."""
import types

import pytest

from gchat_mcp import auth, chat, server
from tests.transport import ME, ChatServer


@pytest.fixture
def srv(monkeypatch):
    s = ChatServer()
    monkeypatch.setattr(chat, "_acquire", lambda: (s, "grant-1"))
    monkeypatch.setattr(chat, "time", types.SimpleNamespace(sleep=lambda t: None))
    chat._identities.clear()
    chat._people.clear()
    monkeypatch.setattr(chat, "label", lambda sp: "Team")
    s.profiles = {ME: {"name": "Sam Rivera", "email": "sam@x.com"}, "222": {"name": "Dana", "email": "dana@x.com"},
                  "333": {"name": "Priya", "email": ""}, "444": {"name": "Noor", "email": "noor@x.com"}}
    s.members[1].update(role="ROLE_MANAGER", createTime="2026-08-10T05:52:00Z")
    s.members.append({"member": {"name": "users/app1", "type": "BOT", "displayName": "Build bot"}})
    yield s
    chat._people.clear()


def test_list_members_reads_every_page_with_roles_and_emails(srv):
    out = server.list_members("AAA")
    assert out.startswith("5 member(s) of Team (`spaces/AAA`):")
    assert "- Sam Rivera <sam@x.com> (me): member, human" in out
    assert "- Dana <dana@x.com>: manager, human, joined 10 Aug 11:22 AM" in out
    assert "- Priya: member, human" in out and "- Build bot: member, app" in out
    assert "CUT OFF" not in out


def test_list_members_says_when_cut_off(srv):
    assert "CUT OFF: only the first 2 members" in server.list_members("AAA", limit=2)


def test_notification_settings_read_and_change(srv):
    assert server.notification_settings("AAA") == "`spaces/AAA`: notifications all, not muted."
    assert server.set_notifications("AAA", notify="for_you", mute="on") == \
        "Updated. `spaces/AAA`: notifications for_you, muted."
    method, path, params, body, _ = [x for x in srv.log if x[0] == "PATCH"][-1]
    assert path == "v1/users/me/spaces/AAA/spaceNotificationSetting"
    assert params["updateMask"] == "notificationSetting,muteSetting"
    assert server.set_notifications("AAA", mute="off").endswith("notifications for_you, not muted.")


@pytest.mark.parametrize("kw, says", [({}, "Nothing to change"), ({"notify": "loud"}, "notify must be"),
                                      ({"mute": "maybe"}, "mute must be")])
def test_set_notifications_validates_before_any_call(srv, kw, says):
    with pytest.raises(ValueError, match=says):
        server.set_notifications("AAA", **kw)
    assert not [x for x in srv.log if x[0] == "PATCH"]


def test_missing_grant_says_run_login(srv):
    srv.deny.add(r"GET spaceNotificationSetting")
    with pytest.raises(auth.AuthError, match="chat.users.spacesettings grant: run `gchat-mcp login`"):
        server.notification_settings("AAA")
