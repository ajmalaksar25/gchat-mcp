"""Through the real chat._send path against tests/transport.ChatServer."""
import threading
import time
import types

import pytest

from gchat_mcp import auth, chat, server
from tests.transport import ChatServer, ME

SP = "spaces/AAA"


@pytest.fixture
def srv(monkeypatch):
    s = ChatServer()
    monkeypatch.setattr(chat, "_acquire", lambda: (s, "grant-1"))
    monkeypatch.setattr(chat, "time", types.SimpleNamespace(sleep=lambda t: None))  # no real retry back-off
    chat._identities.clear()
    monkeypatch.setattr(chat, "label", lambda sp: "Team")
    monkeypatch.setattr(chat, "who", lambda u: "Me")
    return s


# --- upload: binary body on the wire
def test_upload_body_is_binary_safe_and_length_exact(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    payload = bytes(range(256)) * 40 + b"\r\n--looks-like-a-boundary--\r\n" + b"\x00\xff"
    f = tmp_path / "blob.bin"
    f.write_bytes(payload)
    out = server.send_file("AAA", str(f))
    up = next(x for x in srv.log if x[1].endswith("attachments:upload"))
    method, path, params, body, headers = up
    assert (method, path, params) == ("POST", f"upload/v1/{SP}/attachments:upload", {"uploadType": "multipart"})
    boundary = headers["Content-Type"].split("boundary=")[1].encode()
    assert headers["Content-Type"].startswith("multipart/related")
    parts = body.split(b"--" + boundary)
    assert b'{"filename": "blob.bin"}' in parts[1]
    file_part = parts[2].split(b"\r\n\r\n", 1)[1][:-2]  # drop the CRLF before the next boundary
    assert file_part == payload
    assert body.endswith(b"--" + boundary + b"--\r\n")
    create = next(x for x in srv.log if x[0] == "POST" and x[1] == f"v1/{SP}/messages")
    assert create[3]["attachment"] == [{"attachmentDataRef": {"attachmentUploadToken": "tok-1", "resourceName": "r"}}]
    assert "f.bin" in out


def test_upload_scope_403_through_the_real_error_parser(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "a.txt").write_text("x")
    srv.deny.add("POST attachments:upload")
    with pytest.raises(auth.AuthError, match="chat.messages.*gchat-mcp login"):
        server.send_file("AAA", str(tmp_path / "a.txt"))


@pytest.mark.parametrize("call,rule,needle", [
    (lambda: server.mark_read("AAA", thread_id="T1"), "PATCH ReadState", "chat.users.readstate"),
    (lambda: server.start_conversation([], "N"), "POST spaces:setup", "chat.spaces.create"),
    (lambda: server.react(f"{SP}/messages/m", "👍", remove=True), "DELETE reactions", "chat.messages.reactions"),
])
def test_scope_403_names_the_missing_grant(srv, call, rule, needle):
    srv.deny.add(rule)
    srv.threads.add(f"{SP}/threads/T1")
    srv.add_reaction(f"{SP}/messages/m", "👍")
    srv.markers[f"users/{ME}/{SP}/threads/T1/threadReadState"] = "2026-11-23T10:00:00Z"
    with pytest.raises((auth.AuthError, Exception)) as e:
        call()
    assert isinstance(e.value, auth.AuthError) and needle in str(e.value)


def test_a_403_that_is_not_about_scopes_stays_an_api_error(srv, monkeypatch):
    from tests.transport import reply
    monkeypatch.setattr(srv, "route", lambda *a: reply(403, {"error": {"message": "caller lacks permission", "status": "PERMISSION_DENIED"}}))
    with pytest.raises(chat.ApiError, match="lacks permission") as e:
        server.send_message("AAA", "hi")
    assert not e.value.scope_missing


# --- thread replies
def test_explicit_thread_must_fail_rather_than_start_a_new_post(srv):
    srv.threads.add(f"{SP}/threads/T1")
    srv.messages[f"{SP}/messages/seed"] = {"name": f"{SP}/messages/seed", "thread": {"name": f"{SP}/threads/T1"}}
    server.send_message("AAA", "reply", thread_id="T1")
    create = next(x for x in srv.log if x[0] == "POST")
    assert create[2]["messageReplyOption"] == "REPLY_MESSAGE_OR_FAIL"


def test_stale_thread_posts_and_uploads_nothing(srv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(ValueError, match="no messages"):
        server.send_file("AAA", str(tmp_path / "a.txt"), thread_id="GONE")
    with pytest.raises(ValueError, match="no messages"):
        server.send_message("AAA", "hi", thread_id="GONE")
    assert not any(x[0] == "POST" for x in srv.log)


# --- mentions against all pages of members
def test_member_list_is_read_through_every_page_and_truncation_fails(srv):
    assert chat.member_ids(SP) == {ME, "222", "333", "444"}
    assert [x[2].get("pageToken") for x in srv.log if x[1].endswith("/members")] == [None, "2"]
    with pytest.raises(chat.ApiError, match="more than 3 members"):
        chat.member_ids(SP, cap=3)


def test_name_needs_exactly_one_member_even_if_the_directory_returns_one(srv, monkeypatch):
    people = {"222": ("Priya Shah", "priya@x.com"), "333": ("Priya Nair", "priya.n@x.com"), "444": ("Rahul K", "r@x.com"), ME: ("Me", "me@x.com")}
    monkeypatch.setattr(chat, "profile_fresh", lambda u: dict(zip(("name", "email"), people[chat.fmt.uid(u)])))
    monkeypatch.setattr(chat, "directory_search", lambda q, cap=20: [  # truncated directory answer: only one Priya
        {"id": "222", "name": "Priya Shah", "emails": ["priya@x.com"]}])
    with pytest.raises(ValueError, match="matches 2 members"):
        server.send_message("AAA", "hi @[Priya]")
    assert not any(x[0] == "POST" for x in srv.log)
    out = server.send_message("AAA", "hi @[Rahul K]")
    assert "<users/444>" in next(x for x in srv.log if x[0] == "POST")[3]["text"]
    assert out


def test_name_is_refused_when_a_member_profile_cannot_be_read(srv, monkeypatch):
    def pe(u):
        if chat.fmt.uid(u) == "333":
            raise chat.ApiError("people.get", 404, "nf")
        return {"name": "Priya Shah", "email": "p@x.com"}
    monkeypatch.setattr(chat, "profile_fresh", pe)
    with pytest.raises(ValueError, match="cannot be proven unique"):
        server.send_message("AAA", "hi @[Priya Shah]")


# --- read markers the server rewrites
def _put(srv, space_marker, msg_time="2026-11-23T14:00:00.500000Z"):
    srv.messages[f"{SP}/messages/anchor"] = {"name": f"{SP}/messages/anchor", "createTime": msg_time,
                                              "sender": {"name": "users/222", "type": "HUMAN"},
                                              "thread": {"name": f"{SP}/threads/T1"}}
    srv.markers[f"users/{ME}/{SP}/spaceReadState"] = space_marker


def test_marker_the_server_truncates_is_reported_not_trusted(srv):
    _put(srv, "2026-11-23T15:00:00Z")
    srv.rewrite = lambda path, v: v[:19] + "Z"  # Chat keeps whole seconds only
    out = server.mark_unread("AAA", message_id="anchor")
    assert "Chat adjusted it" in out and "2026-11-23T14:00:00Z" in out


def test_mark_read_reports_the_marker_chat_actually_stored(srv):
    _put(srv, "2026-11-23T10:00:00Z")
    srv.rewrite = lambda path, v: "2026-11-23T14:00:00.500000Z"  # clamped to the latest message
    out = server.mark_read("AAA")
    assert "marker now 2026-11-23T14:00:00.500000Z" in out


def test_anchor_message_must_be_in_the_given_thread(srv):
    _put(srv, "2026-11-23T15:00:00Z")
    with pytest.raises(ValueError, match="not changing anything"):
        server.mark_unread("AAA", message_id="anchor", thread_id="OTHER")
    assert not any(x[0] == "PATCH" for x in srv.log)


def test_concurrent_mark_unread_cannot_move_the_marker_later(srv):
    _put(srv, "2026-11-23T20:00:00Z")
    srv.patch_delay = lambda body: 0.3 if body["lastReadTime"].startswith("2026-11-23T18:59") else 0.02  # 19:00 write is slow
    errs = []

    def go(since):
        try:
            server.mark_unread("AAA", since=since)
        except Exception as e:  # pragma: no cover
            errs.append(e)
    a = threading.Thread(target=go, args=("2026-11-23T19:00:00Z",))  # 19:00 UTC
    b = threading.Thread(target=go, args=("2026-11-23T18:00:00Z",))
    a.start()
    time.sleep(0.05)  # a has read 20:00 and is mid-write; unserialised, b reads 20:00 too and finishes first
    b.start()
    a.join(), b.join()
    assert not errs
    assert srv.markers[f"users/{ME}/{SP}/spaceReadState"].startswith("2026-11-23T17:59:59")  # the earlier time wins


def test_marker_is_read_again_right_before_the_write(srv):
    _put(srv, "2026-11-23T20:00:00Z")
    server.mark_unread("AAA", since="2026-11-23T19:00:00Z")
    gets = [x[0] for x in srv.log if x[1].endswith("spaceReadState")]
    assert gets == ["GET", "PATCH", "GET"]  # read, write, read-back, in one locked step


# --- identity is never pinned to the first caller
def test_each_grant_gets_its_own_session_and_identity(monkeypatch):
    import types
    made = []

    class S:
        def __init__(self, creds):
            self.creds = creds
            made.append(self)

        def request(self, method, url, **kw):
            from tests.transport import reply
            return reply(200, {"id": self.creds.refresh_token, "email": ""})
    monkeypatch.setattr(chat, "AuthorizedSession", S)
    chat._sessions.clear(), chat._identities.clear()
    who = {"v": "alice"}
    monkeypatch.setattr(auth, "credentials", lambda: types.SimpleNamespace(client_id="c", refresh_token=who["v"], token=None))
    assert chat.me_id() == "alice"
    who["v"] = "bob"
    assert chat.me_id() == "bob" and len(made) == 2
    who["v"] = "alice"
    assert chat.me_id() == "alice" and len(made) == 2


def test_no_credentials_provider_hook_exists():
    assert not hasattr(auth, "set_credentials_provider")


# --- reactions
def test_list_reactions_says_when_it_was_cut_off(srv, monkeypatch):
    monkeypatch.setattr(server, "REACTION_LIST_CAP", 1)
    srv.add_reaction(f"{SP}/messages/m", "👍")
    srv.add_reaction(f"{SP}/messages/m", "🎉")
    assert "CUT OFF" in server.list_reactions(f"{SP}/messages/m")


def test_real_409_on_a_repeat_reaction_is_verified(srv):
    server.react(f"{SP}/messages/m", "👍")
    assert "already have" in server.react(f"{SP}/messages/m", "👍")


# ===== last review round =====
# --- thread replies only in named spaces
@pytest.mark.parametrize("kind,word", [("DIRECT_MESSAGE", "DM"), ("GROUP_CHAT", "group chat")])
def test_explicit_thread_outside_a_named_space_is_refused_before_any_upload(srv, tmp_path, monkeypatch, kind, word):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "a.txt").write_text("x")
    srv.space_type = kind
    srv.threads.add(f"{SP}/threads/T1")
    srv.messages[f"{SP}/messages/seed"] = {"name": "x", "thread": {"name": f"{SP}/threads/T1"}}
    for call in (lambda: server.send_file("AAA", str(tmp_path / "a.txt"), thread_id="T1"),
                 lambda: server.send_message("AAA", "hi", thread_id="T1")):
        with pytest.raises(ValueError, match=f"{word}, which has no threads"):
            call()
    assert not any(x[0] == "POST" for x in srv.log)


# --- mentions: fresh profiles, nameless members refuse
def _profiles(srv, **p):
    srv.profiles = {"111": {"name": "Me", "email": "me@x.com"}, "222": {"name": "Priya Shah", "email": "priya@x.com"},
                    "333": {"name": "Priya Nair", "email": "pn@x.com"}, "444": {"name": "Rahul K", "email": "r@x.com"}, **p}


def test_name_matching_ignores_cached_names_and_fetches_profiles_now(srv):
    _profiles(srv)
    chat._people.update({"444": {"name": "Rahul Old", "email": "r@x.com"}, "222": {"name": "Rahul K", "email": "z"}})  # stale cache
    server.send_message("AAA", "hi @[Rahul K]")
    assert "<users/444>" in next(x for x in srv.log if x[0] == "POST")[3]["text"]  # not the stale 222
    first = len([x for x in srv.log if x[1].startswith("v1/people/")])
    server.send_message("AAA", "again @[Rahul K]")
    assert len([x for x in srv.log if x[1].startswith("v1/people/")]) == 2 * first  # fetched again, not cached


@pytest.mark.parametrize("profile", [{"name": "", "email": "n@x.com"}, {"name": "   ", "email": ""}])
def test_member_without_a_display_name_refuses_name_mentions(srv, profile):
    _profiles(srv, **{"333": profile})
    with pytest.raises(ValueError, match="no display name"):
        server.send_message("AAA", "hi @[Rahul K]")
    assert not any(x[0] == "POST" for x in srv.log)


# --- identity: one credentials load per tool call
def _grants(monkeypatch):
    import types as t
    servers = {"g1": ChatServer(), "g2": ChatServer(), "g3": ChatServer()}
    loads = []

    def credentials():
        loads.append(1)
        return t.SimpleNamespace(client_id="c", refresh_token=f"g{len(loads)}", token=None)
    monkeypatch.setattr(auth, "credentials", credentials)
    monkeypatch.setattr(chat, "AuthorizedSession", lambda creds: servers[creds.refresh_token])
    monkeypatch.setattr(chat, "time", types.SimpleNamespace(sleep=lambda t: None))
    chat._sessions.clear(), chat._identities.clear()
    return servers, loads


def test_a_tool_call_loads_credentials_once_and_uses_one_grant_throughout(monkeypatch):
    servers, loads = _grants(monkeypatch)
    for s in servers.values():
        s.markers[f"users/{ME}/{SP}/spaceReadState"] = "2026-11-23T10:00:00Z"
        s.messages[f"{SP}/messages/m"] = {"name": f"{SP}/messages/m", "createTime": "2026-11-23T12:00:00Z",
                                           "sender": {"name": "users/222", "type": "HUMAN"}, "thread": {"name": f"{SP}/threads/T"}}
    out = server.mark_read("AAA")  # several requests, me lookup, and pool threads for the thread sweep
    assert len(loads) == 1
    assert servers["g1"].log and not servers["g2"].log and not servers["g3"].log
    assert "marked read" in out


def test_credentials_changing_mid_call_do_not_change_that_calls_identity(monkeypatch):
    servers, loads = _grants(monkeypatch)
    servers["g1"].messages[f"{SP}/messages/mine"] = {"name": f"{SP}/messages/mine", "text": "t", "createTime": "2026-11-23T12:00:00Z",
                                                      "sender": {"name": f"users/{ME}", "type": "HUMAN"}}
    server.delete_message(f"{SP}/messages/mine")  # get, me lookup, delete, read-back: 4+ requests
    assert len(loads) == 1 and not servers["g2"].log


def test_separate_tool_calls_each_read_credentials_again(monkeypatch):
    servers, loads = _grants(monkeypatch)
    servers["g1"].add_reaction(f"{SP}/messages/m", "👍")
    servers["g2"].add_reaction(f"{SP}/messages/m", "🎉")
    server.list_reactions(f"{SP}/messages/m")
    server.list_reactions(f"{SP}/messages/m")
    assert len(loads) == 2 and servers["g1"].log and servers["g2"].log  # a new call may be a different caller


# --- accepted writes never raise
def _broken(srv):
    srv.break_reads = True
    return srv


def test_send_accepted_then_read_back_503_is_not_an_error(srv):
    _broken(srv)
    out = server.send_message("AAA", "hello")
    assert "could not verify" in out and "WAS accepted" in out and "do not repeat" in out


def test_delete_accepted_then_read_back_503_is_not_an_error(srv):
    srv.messages[f"{SP}/messages/mine"] = {"name": f"{SP}/messages/mine", "createTime": "2026-11-23T12:00:00Z",
                                            "sender": {"name": f"users/{ME}", "type": "HUMAN"}}
    _broken(srv)
    out = server.delete_message(f"{SP}/messages/mine")
    assert out.startswith(f"Deleted `{SP}/messages/mine`, but could not verify") and "WAS accepted" in out


def test_reaction_removal_accepted_then_read_back_503_is_not_an_error(srv):
    srv.add_reaction(f"{SP}/messages/m", "👍")
    _broken(srv)
    out = server.react(f"{SP}/messages/m", "👍", remove=True)
    assert "could not verify" in out and "WAS accepted" in out
    assert not srv.reactions


@pytest.mark.parametrize("call", [
    lambda: server.mark_read("AAA", thread_id="T1"),
    lambda: server.mark_read("AAA"),
    lambda: server.mark_unread("AAA", since="2026-11-23T09:00:00Z"),
])
def test_marker_write_accepted_then_read_back_503_is_not_an_error(srv, call):
    srv.markers[f"users/{ME}/{SP}/spaceReadState"] = "2026-11-23T10:00:00Z"
    srv.markers[f"users/{ME}/{SP}/threads/T1/threadReadState"] = "2026-11-23T10:00:00Z"
    srv.break_reads = False
    real = srv.route

    def route(method, path, params, js, body):  # reads fail only after the marker was written
        r = real(method, path, params, js, body)
        if method == "PATCH":
            srv.break_reads = True
        return r
    srv.route = route
    out = call()
    assert "could not verify" in out and "WAS accepted" in out


def test_start_conversation_accepted_then_member_list_503_is_not_an_error(srv):
    _broken(srv)
    out = server.start_conversation([], "Solo test")
    assert "Created `spaces/NEW`" in out and "could not verify" in out and "WAS accepted" in out


# --- a 409 with only someone else's reaction is not mine
def test_409_with_only_another_users_reaction_is_not_reported_as_mine(srv):
    srv.add_reaction(f"{SP}/messages/m", "👍", user="users/222")
    srv.force_409 = True
    with pytest.raises(chat.ApiError, match="409"):
        server.react(f"{SP}/messages/m", "👍")


def test_reaction_check_does_not_trust_a_broken_server_filter(srv):
    srv.add_reaction(f"{SP}/messages/m", "👍", user="users/222")
    srv.ignore_user_filter = True  # list returns everyone's reactions
    srv.force_409 = True
    with pytest.raises(chat.ApiError, match="409"):
        server.react(f"{SP}/messages/m", "👍")


def test_user_filter_is_enforced_by_the_fixture_itself(srv):
    srv.add_reaction(f"{SP}/messages/m", "👍", user="users/222")
    assert chat.reactions(f"{SP}/messages/m", user=f"users/{ME}") == []
    assert len(chat.reactions(f"{SP}/messages/m")) == 1
