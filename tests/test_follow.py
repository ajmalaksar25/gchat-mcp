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
