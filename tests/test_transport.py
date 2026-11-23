"""Through the real chat._send path against tests/transport.ChatServer."""
import threading
import time

import pytest

from gchat_mcp import auth, chat, server
from tests.transport import ChatServer, ME

SP = "spaces/AAA"


@pytest.fixture
def srv(monkeypatch):
    s = ChatServer()
    monkeypatch.setattr(chat, "_session_for", lambda: (s, "grant-1"))
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
    srv.reactions[(f"{SP}/messages/m", "👍")] = True
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
    monkeypatch.setattr(chat, "person_strict", lambda u: dict(zip(("name", "email"), people[chat.fmt.uid(u)])))
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
    monkeypatch.setattr(chat, "person_strict", pe)
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
    srv.reactions[(f"{SP}/messages/m", "👍")] = True
    srv.reactions[(f"{SP}/messages/m", "🎉")] = True
    assert "CUT OFF" in server.list_reactions(f"{SP}/messages/m")


def test_real_409_on_a_repeat_reaction_is_verified(srv):
    server.react(f"{SP}/messages/m", "👍")
    assert "already have" in server.react(f"{SP}/messages/m", "👍")
