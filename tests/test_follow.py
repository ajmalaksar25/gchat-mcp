"""`gchat-mcp watch --chat`: follow chosen conversations, resume after a restart, report each message once."""
# ruff: noqa: F811  # imported pytest fixtures are used by parameter name
import io
import json

import anyio
import pytest

from gchat_mcp import follow, watch
from tests.test_triage import put, srv  # noqa: F401 (srv is a fixture)
from tests.transport import ME

START = "2026-11-23T10:30:00Z"  # after DM1/a1 (mine) and before DM1/a2, DM2/b2 (theirs)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(follow, "now", lambda: datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc))


def run(chats, since=START, inbox=False, include_mine=False, polls=1):
    out = io.StringIO()
    code = anyio.run(lambda: watch.run({"reply_needed", "unclassified"}, 0, since, 0, polls=polls, out=out,
                                       chats=chats, inbox=inbox, include_mine=include_mine))
    assert code == 0
    return [json.loads(x) for x in out.getvalue().splitlines()]


@pytest.mark.parametrize("ref", ["spaces/AbC_9-x", "AbC_9-x", "https://chat.google.com/dm/AbC_9-x?cls=11",
                                 "https://chat.google.com/room/AbC_9-x", "https://mail.google.com/chat/u/0/#chat/space/AbC_9-x"])
def test_conversation_accepts_ids_and_links(ref):
    assert follow.conversation(ref) == "spaces/AbC_9-x"


def test_conversation_rejects_other_text():
    with pytest.raises(ValueError):
        follow.conversation("Caleb's DM")


def test_follows_several_chats_in_one_watcher_and_skips_my_messages(srv):
    lines = run(["spaces/DM1", "DM2"])
    assert [(x["event"], x["message_id"]) for x in lines] == [
        ("message", "spaces/DM1/messages/a2"), ("message", "spaces/DM2/messages/b2")]
    assert lines[0]["text"] == "Can you join the client call tomorrow at 5 PM?"
    assert set(lines[0]) == {"event", "conversation", "conversation_id", "sender", "message_id", "thread_id", "reply",
                             "link", "text", "sent_at_local"}


def test_restart_resumes_from_the_cursor_without_gap_or_repeat(srv):
    run(["DM1"])
    put(srv, "DM1", "a3", "2026-11-23T13:00:00Z", "222", "arrived while no watcher ran")
    assert [x["message_id"] for x in run(["DM1"], since="")] == ["spaces/DM1/messages/a3"]
    assert run(["DM1"], since="") == []


def test_include_mine_and_inbox_together(srv):
    lines = run(["DM1"], include_mine=False, inbox=True)
    assert [x["event"] for x in lines] == ["message", "inbox", "inbox"]
    put(srv, "DM1", "a4", "2026-11-23T14:30:00Z", ME, "my own note")
    assert [x["message_id"] for x in run(["DM1"], since="", include_mine=True)] == ["spaces/DM1/messages/a4"]


def test_concurrent_watcher_already_reported_it(srv):
    f1, f2 = follow.Follow(["DM1"], START), follow.Follow(["DM1"], START)
    fetched2, _ = f2.fetch()
    assert [x["message_id"] for x in f1.step()] == ["spaces/DM1/messages/a2"]
    assert f2.commit(fetched2) == []


def test_old_cursor_restarts_at_now_with_a_note(srv):
    from datetime import datetime, timezone
    f = follow.Follow(["DM1"])
    f.fetch(datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc))  # cursor set long before "now" below
    fetched, notes = f.fetch(datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc))
    assert [n["event"] for n in notes] == ["note"] and fetched == {"spaces/DM1": []}


def test_outage_reports_degraded_once_then_recovered(srv, monkeypatch):
    calls = []
    real = follow.Follow.fetch

    def flaky(self, at=None):
        calls.append(1)
        if len(calls) <= 4:
            raise RuntimeError("HTTP network error: Read timed out")
        return real(self, at)
    monkeypatch.setattr(follow.Follow, "fetch", flaky)

    async def nap(t):
        pass
    monkeypatch.setattr(watch.anyio, "sleep", nap)
    lines = run(["DM1"], polls=6)
    assert [x["event"] for x in lines] == ["degraded", "message", "recovered"]


def test_one_unreadable_chat_does_not_silence_the_others(srv, monkeypatch):
    from gchat_mcp import chat
    real = chat.messages

    def flaky(space, **kw):
        if space == "spaces/DM2":
            raise chat.ApiError("spaces.messages.list", 403, "no access")
        return real(space, **kw)
    monkeypatch.setattr(chat, "messages", flaky)
    f = follow.Follow(["DM1", "DM2"], START)
    lines = f.step()
    assert [(x["event"], x.get("message_id", x.get("conversation_id"))) for x in lines] == [
        ("note", "spaces/DM2"), ("message", "spaces/DM1/messages/a2")]
    assert f.step() == []  # the outage is reported once, not on every poll
    monkeypatch.setattr(chat, "messages", real)
    assert [(x["event"], x.get("message_id", x.get("text"))) for x in f.step()] == [
        ("note", "readable again"), ("message", "spaces/DM2/messages/b2")]  # nothing missed while it was down


def test_label_failure_never_loses_messages(srv, monkeypatch):
    from gchat_mcp import chat
    real = chat.get
    calls = []

    def get(path, call, params=None):
        if call == "spaces.get" and not calls:
            calls.append(1)
            raise chat.ApiError("spaces.get", 503, "blip")
        return real(path, call, params)
    monkeypatch.setattr(chat, "get", get)
    f = follow.Follow(["DM1"], START)
    with pytest.raises(chat.ApiError):
        f.step()  # the label read fails before any cursor moves...
    assert [x["message_id"] for x in f.step() if x["event"] == "message"] == ["spaces/DM1/messages/a2"]  # retry prints it


def test_a_quiet_chat_still_reports_a_new_message_after_six_hours(srv, monkeypatch):
    from datetime import datetime, timezone
    t = [datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(follow, "now", lambda: t[0])
    f = follow.Follow(["DM1"], START)
    f.step()  # reports a2 (11:00) and keeps polling
    for hour in range(16, 23):  # seven quiet hours of polls
        t[0] = datetime(2026, 11, 23, hour, 0, tzinfo=timezone.utc)
        assert f.step() == []
    put(srv, "DM1", "a9", "2026-11-23T22:30:00Z", "222", "still there?")
    t[0] = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)
    assert [x["message_id"] for x in follow.Follow(["DM1"]).step()] == ["spaces/DM1/messages/a9"]  # even re-armed


def test_inbox_still_watched_when_every_chat_fails(srv, monkeypatch):
    from gchat_mcp import chat

    def down(self, at=None):
        raise chat.ApiError("spaces.messages.list", 403, "no access")
    monkeypatch.setattr(follow.Follow, "fetch", down)

    async def nap(t):
        pass
    monkeypatch.setattr(watch.anyio, "sleep", nap)
    lines = run(["DM1"], inbox=True)
    assert [x["event"] for x in lines] == ["inbox", "inbox"]
