"""Thread read markers and edit read-back, against the in-memory Chat transport (tests/transport.py).

API facts these tests encode (Google Chat REST reference):
- spaces.messages.list `filter` supports only `createTime` and `thread.name`; Message.threadReply is true for a
  reply inside a reply thread (developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages).
- users.spaces.updateSpaceReadState: the space marker affects only top-level messages; replies "rely on the
  thread read state" (.../rest/v1/users.spaces/updateSpaceReadState).
- users.spaces.threads.getThreadReadState / updateThreadReadState (`lastReadTime`) are per thread.
- Message.text is plain; Message.formattedText carries the markup (.../rest/v1/spaces.messages).
"""
import types

import pytest

from gchat_mcp import chat, server
from tests.transport import ChatServer, ME

SP = "spaces/AAA"
SPACE_M = f"users/{ME}/{SP}/spaceReadState"


def thread_m(t):
    return f"users/{ME}/{SP}/threads/{t}/threadReadState"


@pytest.fixture
def srv(monkeypatch):
    s = ChatServer()
    monkeypatch.setattr(chat, "_acquire", lambda: (s, "grant-1"))
    chat._identities.clear()
    monkeypatch.setattr(chat, "time", types.SimpleNamespace(sleep=lambda t: None))  # no real retry back-off
    monkeypatch.setattr(chat, "label", lambda sp: "Team")
    monkeypatch.setattr(chat, "who", lambda u: "Priya")
    return s


def put(srv, mid, t, thread, reply=False, sender="222", text="hello"):
    srv.messages[f"{SP}/messages/{mid}"] = {
        "name": f"{SP}/messages/{mid}", "createTime": t, "text": text, "threadReply": reply,
        "sender": {"name": f"users/{sender}", "type": "HUMAN", "displayName": "Priya"},
        "thread": {"name": f"{SP}/threads/{thread}"}}


def two_threads(srv, marker="2026-11-23T15:00:00Z"):
    """Everything read: top-level + reply in T1, top-level + reply in T2, all markers at 15:00."""
    put(srv, "top1", "2026-11-23T14:05:00Z", "T1")
    put(srv, "r1", "2026-11-23T14:10:00Z", "T1", reply=True)
    put(srv, "top2", "2026-11-23T14:20:00Z", "T2")
    put(srv, "r2", "2026-11-23T14:30:00Z", "T2", reply=True)
    for k in (SPACE_M, thread_m("T1"), thread_m("T2")):
        srv.markers[k] = marker


def patched(srv):
    return [x[1].replace(f"v1/users/{ME}/{SP}/", "") for x in srv.log if x[0] == "PATCH"]


# --- mark_unread(since) must move the thread markers of threads with replies at or after the cutoff
def test_mark_unread_since_also_moves_every_thread_with_replies_after_the_cutoff(srv):
    two_threads(srv)
    out = server.mark_unread("AAA", since="2026-11-23T14:00:00Z")
    assert srv.markers[SPACE_M] == "2026-11-23T13:59:59.999000Z"
    assert srv.markers[thread_m("T1")] == "2026-11-23T13:59:59.999000Z"
    assert srv.markers[thread_m("T2")] == "2026-11-23T13:59:59.999000Z"
    assert "threads/T1" in out and "threads/T2" in out  # reported per thread


def test_thread_markers_only_move_earlier_and_threads_before_the_cutoff_are_untouched(srv):
    two_threads(srv)
    put(srv, "old", "2026-11-23T12:00:00Z", "T3", reply=True)  # reply before the cutoff
    srv.markers[thread_m("T3")] = "2026-11-23T15:00:00Z"
    srv.markers[thread_m("T1")] = "2026-11-23T13:00:00Z"  # already unread from before the cutoff
    out = server.mark_unread("AAA", since="2026-11-23T14:00:00Z")
    assert srv.markers[thread_m("T1")] == "2026-11-23T13:00:00Z"  # not moved later
    assert srv.markers[thread_m("T3")] == "2026-11-23T15:00:00Z"
    assert "threads/T3/threadReadState" not in " ".join(patched(srv))
    assert "threads/T1/threadReadState" not in " ".join(patched(srv))
    assert "threads/T1" in out and "left alone" in out


def test_top_level_messages_do_not_make_a_thread_sweep_write(srv):
    put(srv, "top1", "2026-11-23T14:05:00Z", "T1")  # a top-level message only: no thread marker involved
    srv.markers[SPACE_M] = "2026-11-23T15:00:00Z"
    server.mark_unread("AAA", since="2026-11-23T14:00:00Z")
    assert patched(srv) == ["spaceReadState"]


def test_mark_unread_from_a_top_level_message_moves_threads_with_later_replies(srv):
    two_threads(srv)
    out = server.mark_unread("AAA", message_id="top1")  # 14:05: T1's reply (14:10) and all of T2 come after
    assert srv.markers[SPACE_M] == "2026-11-23T14:04:59.999000Z"
    assert srv.markers[thread_m("T1")] == "2026-11-23T14:04:59.999000Z"
    assert srv.markers[thread_m("T2")] == "2026-11-23T14:04:59.999000Z"
    assert "threads/T2" in out


def test_thread_sweep_is_capped_and_says_so(srv, monkeypatch):
    monkeypatch.setattr(server, "THREAD_SWEEP_CAP", 3)
    srv.markers[SPACE_M] = "2026-11-23T15:00:00Z"
    for i in range(5):
        put(srv, f"r{i}", f"2026-11-23T14:1{i}:00Z", f"T{i}", reply=True)
        srv.markers[thread_m(f"T{i}")] = "2026-11-23T15:00:00Z"
    out = server.mark_unread("AAA", since="2026-11-23T14:00:00Z")
    assert len([p for p in patched(srv) if "threads/" in p]) == 3
    assert "3 of 5 threads" in out


def test_message_scan_is_capped_and_says_so(srv, monkeypatch):
    monkeypatch.setattr(server, "MAX_PER_SPACE", 2)
    srv.markers[SPACE_M] = "2026-11-23T15:00:00Z"
    for i in range(4):
        put(srv, f"r{i}", f"2026-11-23T14:1{i}:00Z", f"T{i}", reply=True)
        srv.markers[thread_m(f"T{i}")] = "2026-11-23T15:00:00Z"
    out = server.mark_unread("AAA", since="2026-11-23T14:00:00Z")
    assert "newest 2 messages" in out and "older threads" in out


def test_thread_sweep_failure_is_reported_after_the_space_marker_was_written(srv, monkeypatch):
    two_threads(srv)
    real = srv.route

    def route(method, path, params, js, body):
        if method == "GET" and path.endswith("/messages") and params.get("filter"):
            return __import__("tests.transport", fromlist=["reply"]).reply(503, {"error": {"message": "down"}})
        return real(method, path, params, js, body)
    srv.route = route
    out = server.mark_unread("AAA", since="2026-11-23T14:00:00Z")
    assert srv.markers[SPACE_M] == "2026-11-23T13:59:59.999000Z"
    assert "could not check its threads" in out.lower() or "could not list" in out.lower()


def test_one_thread_only_still_touches_only_that_thread(srv):
    two_threads(srv)
    server.mark_unread("AAA", since="2026-11-23T14:00:00Z", thread_id="T2")
    assert patched(srv) == ["threads/T2/threadReadState"]


# --- unread() uses each thread's own marker for replies
def test_reply_is_unread_by_its_thread_marker_even_when_the_space_marker_is_later(srv):
    two_threads(srv)  # everything read ...
    srv.markers[thread_m("T1")] = "2026-11-23T14:00:00Z"  # ... then T1 set unread from 14:00 (space marker stays 15:00)
    out = server.unread(since="2026-11-23T00:00:00Z")
    assert "1 unread" in out and "`" in out and "r1" in out


def test_reply_marked_read_by_its_thread_marker_is_not_listed_when_the_space_marker_is_earlier(srv):
    two_threads(srv)
    srv.markers[SPACE_M] = "2026-11-23T14:00:00Z"
    srv.markers[thread_m("T1")] = "2026-11-23T15:00:00Z"  # thread read
    srv.markers[thread_m("T2")] = "2026-11-23T15:00:00Z"
    out = server.unread(since="2026-11-23T00:00:00Z")
    assert "r1" not in out and "r2" not in out  # replies are read; top-level 14:05/14:20 follow the space marker
    assert "top1" in out and "top2" in out


def test_top_level_messages_still_follow_the_space_marker(srv):
    two_threads(srv)
    srv.markers[SPACE_M] = "2026-11-23T14:10:00Z"  # top1 (14:05) read, top2 (14:20) unread
    srv.markers[thread_m("T1")] = "2026-11-23T15:00:00Z"
    srv.markers[thread_m("T2")] = "2026-11-23T15:00:00Z"
    out = server.unread(since="2026-11-23T00:00:00Z")
    assert "top2" in out and "top1" not in out


def test_own_replies_are_never_unread(srv):
    two_threads(srv)
    put(srv, "mine", "2026-11-23T14:40:00Z", "T1", reply=True, sender=ME)
    srv.markers[thread_m("T1")] = "2026-11-23T14:00:00Z"
    out = server.unread(since="2026-11-23T00:00:00Z")
    assert "mine" not in out and "r1" in out


def test_unread_thread_state_calls_are_capped_and_fall_back_to_the_space_marker(srv, monkeypatch):
    monkeypatch.setattr(server, "THREAD_STATE_CAP", 2)
    srv.markers[SPACE_M] = "2026-11-23T14:05:00Z"
    for i in range(4):
        put(srv, f"r{i}", f"2026-11-23T14:{10 + 2 * i}:00Z", f"T{i}", reply=True)  # 14:10, 14:12, 14:14, 14:16
        srv.markers[thread_m(f"T{i}")] = "2026-11-23T15:00:00Z"
    out = server.unread(since="2026-11-23T00:00:00Z")
    states = [x for x in srv.log if x[1].endswith("threadReadState")]
    assert len(states) == 2
    assert "thread read state checked for 2 of 4 threads" in out
    # T3, T2 (newest) have their own marker (read); T1, T0 are beyond the cap, judged by the space marker (14:05)
    assert "`r0`" in out and "`r1`" in out and "`r3`" not in out and "`r2`" not in out


def test_unreadable_thread_state_falls_back_to_the_space_marker_with_a_note(srv):
    two_threads(srv)
    srv.markers[SPACE_M] = "2026-11-23T14:00:00Z"
    srv.deny.add("GET threadReadState")  # insufficient scope on thread state reads
    out = server.unread(since="2026-11-23T00:00:00Z")
    assert "r1" in out and "r2" in out
    assert "thread read state" in out.lower() and "space marker" in out.lower()


# --- edit_message: markup without mentions
SENT = f"{SP}/messages/mine"


def own(srv, text="old"):
    srv.messages[SENT] = {"name": SENT, "sender": {"name": f"users/{ME}", "type": "HUMAN"}, "text": text,
                          "createTime": "2026-11-23T13:30:00Z", "thread": {"name": f"{SP}/threads/T1"}}


@pytest.mark.parametrize("new", ["*bold* text", "_italic_ and ~gone~", "Please *reply*  today", "`code` x\n* item"])
def test_edit_with_markup_and_no_mentions_verifies(srv, new):
    own(srv)
    out = server.edit_message(SENT, new)
    assert "text verified" in out and "could not verify" not in out
    assert srv.messages[SENT]["text"] != new or "*" not in new  # the fake really stored plain text for markup


def test_edit_still_reports_a_real_mismatch(srv):
    own(srv)
    srv.after_patch = lambda m: m.update(text="something else", formattedText="something else")
    out = server.edit_message(SENT, "*bold* text")
    assert "could not verify" in out and "text in the read-back differs" in out and "WAS accepted" in out


def test_edit_with_markup_and_a_mention(srv):
    own(srv)
    srv.profiles = {i: {"name": n, "email": f"{n.split()[0].lower()}@x.com"}
                    for i, n in {"111": "Me Me", "222": "Priya Shah", "333": "Rahul K", "444": "Dev Rao"}.items()}
    out = server.edit_message(SENT, "*hi* @[priya@x.com]")
    assert "text verified, mentions confirmed" in out


# --- Devin on PR #3: the query bound is one ms early; only replies at or after the cutoff may select a thread
def test_sweep_ignores_replies_before_the_cutoff_even_by_half_a_millisecond(srv):
    srv.markers[SPACE_M] = "2026-11-23T15:00:00Z"
    put(srv, "at", "2026-11-23T14:00:00Z", "TA", reply=True)  # exactly at the cutoff: selected
    put(srv, "before", "2026-11-23T13:59:59.999500Z", "TB", reply=True)  # 0.5 ms before: not selected
    for t in ("TA", "TB"):
        srv.markers[thread_m(t)] = "2026-11-23T15:00:00Z"
    server.mark_unread("AAA", since="2026-11-23T14:00:00Z")
    assert srv.markers[thread_m("TA")] == "2026-11-23T13:59:59.999000Z"
    assert srv.markers[thread_m("TB")] == "2026-11-23T15:00:00Z"  # untouched
    assert patched(srv) == ["spaceReadState", "threads/TA/threadReadState"]
