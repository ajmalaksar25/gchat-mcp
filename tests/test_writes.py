"""Write tools against a fake Chat API: what is called, what is refused before any call, what the read-back says."""
import pytest

from gchat_mcp import auth, chat, server

SP = "spaces/AAA"
ME = "111"
PEOPLE = {"111": ("Ajmal", "ajmal@x.com"), "222": ("Priya Shah", "priya@x.com"), "333": ("Priya Nair", "priya.n@x.com")}


class Fake:
    def __init__(self):
        self.calls, self.msgs, self.members = [], {}, {"111", "222"}
        self.read = {}  # path -> lastReadTime
        self.threading = "THREADED_MESSAGES"

    def post(self, path, call, params, body):
        self.calls.append(("POST", path, call, body, params))
        if call == "messages.create":
            name = f"{SP}/messages/new1"
            text = body.get("text", "")
            ann = [{"type": "USER_MENTION", "userMention": {"user": {"name": f"users/{i}"}, "type": "MENTION"}}
                   for i in {t[7:-1] for t in text.split() if t.startswith("<users/")}]
            self.msgs[name] = {"name": name, "sender": {"name": f"users/{ME}", "type": "HUMAN"}, "text": text,
                               "createTime": "2026-11-23T14:00:00.500000Z", "thread": {"name": f"{SP}/threads/T1"},
                               "annotations": ann, "attachment": [
                                   {"contentName": "r.txt", "contentType": "text/plain"}] if body.get("attachment") else []}
            return {"name": name}
        if call == "spaces.setup":
            sp = {"name": "spaces/NEW", "spaceType": body["space"]["spaceType"], "displayName": body["space"].get("displayName", ""),
                  "spaceUri": "https://chat.google.com/room/NEW"}
            self.members = {ME} | {m["member"]["name"][6:] for m in body["memberships"]}
            return sp
        raise AssertionError(call)

    def patch(self, path, call, params, body):
        self.calls.append(("PATCH", path, call, body, params))
        if call == "messages.patch":
            self.msgs[path]["text"] = body["text"]
            self.msgs[path]["lastUpdateTime"] = "2026-11-23T14:05:00Z"
        else:
            self.read[path] = body["lastReadTime"]
        return {}

    def delete(self, path, call, params=None):
        self.calls.append(("DELETE", path, call, None, params))
        self.msgs[path]["deleteTime"] = "2026-11-23T14:06:00Z"

    def get(self, path, call, params=None):
        if call == "messages.get":
            return self.msgs[path]
        if call == "spaces.get":
            return {"name": path, "displayName": "Team", "spaceType": "SPACE", "spaceThreadingState": self.threading}
        if call == "users.spaces.getReadState":
            return {"lastReadTime": self.read.get(path, "")}
        if call == "spaces.findDirectMessage":
            raise chat.ApiError(call, 404, "none")
        raise AssertionError(call)


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    f.msgs[f"{SP}/messages/mine"] = {"name": f"{SP}/messages/mine", "sender": {"name": f"users/{ME}", "type": "HUMAN"},
                                     "text": "old", "createTime": "2026-11-23T13:30:00.250000Z",
                                     "thread": {"name": f"{SP}/threads/T1"}}
    f.msgs[f"{SP}/messages/theirs"] = {"name": f"{SP}/messages/theirs", "sender": {"name": "users/222", "type": "HUMAN",
                                       "displayName": "Priya Shah"}, "text": "hello", "createTime": "2026-11-23T13:00:00Z"}
    for n in ("post", "patch", "delete", "get"):
        monkeypatch.setattr(chat, n, getattr(f, n))
    monkeypatch.setattr(chat, "me_id", lambda: ME)
    monkeypatch.setattr(chat, "me_email", lambda: "ajmal@x.com")
    monkeypatch.setattr(chat, "who", lambda u: PEOPLE.get(chat.fmt.uid(u), ("Unknown",))[0] if not (isinstance(u, dict) and u.get("displayName")) else u["displayName"])
    monkeypatch.setattr(chat, "label", lambda s: s.get("displayName", "x"))
    monkeypatch.setattr(chat, "person", lambda u: dict(zip(("name", "email"), PEOPLE.get(chat.fmt.uid(u), ("Unknown", "")))))
    monkeypatch.setattr(chat, "messages", lambda space, since="", thread="", cap=500: ([{"name": "m"}] if thread else [], False))
    monkeypatch.setattr(chat, "profile_fresh", lambda u: {"name": PEOPLE.get(chat.fmt.uid(u), ("", ""))[0],
                                                          "email": PEOPLE.get(chat.fmt.uid(u), ("", ""))[1]})
    monkeypatch.setattr(chat, "member_ids", lambda space, cap=1000: set(f.members))
    monkeypatch.setattr(chat, "directory_search", lambda q, cap=20: [
        {"id": i, "name": n, "emails": [e]} for i, (n, e) in PEOPLE.items()])
    return f


# --- mentions in send_message
def test_mention_by_email_is_inserted_and_confirmed(fake):
    out = server.send_message("AAA", "ping @[priya@x.com] please look")
    body = fake.calls[0][3]
    assert body["text"] == "ping <users/222> please look"
    assert "Mentions confirmed: Priya Shah" in out


def test_ambiguous_and_absent_mentions_send_nothing(fake):
    fake.members = {"111", "222", "333"}
    with pytest.raises(ValueError, match="matches 2 members"):
        server.send_message("AAA", "hi @[Priya]")
    fake.members = {"111"}
    with pytest.raises(ValueError, match="not a member"):
        server.send_message("AAA", "hi @[priya@x.com]")
    assert fake.calls == []


def test_missing_annotation_in_read_back_is_flagged(fake):
    real = fake.post

    def post(path, call, params, body):
        r = real(path, call, params, body)
        fake.msgs[r["name"]]["annotations"] = []
        return r
    chat.post = post
    assert "WARNING: the read-back has no mention annotation" in server.send_message("AAA", "hi @[priya@x.com]")


def test_plain_send_still_reports_sender(fake):
    out = server.send_message("AAA", "hello")
    assert "Sender: Ajmal (HUMAN, me)" in out and "Mentions" not in out


def test_scope_403_on_send_has_the_login_hint(fake, monkeypatch):
    def denied(*a):
        raise chat.ApiError("messages.create", 403, "Request had insufficient authentication scopes.")
    monkeypatch.setattr(chat, "post", denied)
    with pytest.raises(auth.AuthError, match="chat.messages.*gchat-mcp login"):
        server.send_message("AAA", "hello")


# --- send_file
@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.mark.parametrize("make", ["missing", "empty", "env", "dir"])
def test_send_file_refuses_before_any_call(fake, home, monkeypatch, make):
    monkeypatch.setattr(chat, "upload", lambda *a: pytest.fail("uploaded"))
    p = {"missing": home / "nope.txt", "empty": home / "e.txt", "env": home / ".env.prod", "dir": home}[make]
    if make in ("empty", "env"):
        p.write_text("" if make == "empty" else "SECRET=1")
    with pytest.raises(ValueError):
        server.send_file("AAA", str(p))
    assert fake.calls == []


@pytest.mark.parametrize("rel", [".config/gchat-mcp/token.json", ".ssh/anything", ".aws/credentials", ".kube/config",
                                 ".docker/config.json", ".netrc", "docs/server.pem", "docs/my.key", "docs/id_rsa",
                                 "docs/token_old.json", "docs/credentials-prod.json", "docs/client_secret_1.json",
                                 "Library/Keychains/login.keychain-db", ".config/anything.txt",
                                 "Library/Application Support/Google/Chrome/Default/Cookies"])
def test_send_file_denylist(fake, home, monkeypatch, rel):
    monkeypatch.setattr(chat, "upload", lambda *a: pytest.fail("uploaded"))
    f = home / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    with pytest.raises(ValueError, match="Refusing"):
        server.send_file("AAA", str(f))
    assert fake.calls == []


def test_send_file_refuses_a_symlink_final_component(fake, home, monkeypatch):
    monkeypatch.setattr(chat, "upload", lambda *a: pytest.fail("uploaded"))
    (home / "ok.txt").write_text("fine")
    (home / "link.txt").symlink_to(home / "ok.txt")  # harmless target: still refused, a swap is the danger
    with pytest.raises(ValueError, match="symbolic link"):
        server.send_file("AAA", str(home / "link.txt"))


def test_send_file_checks_the_real_path_of_the_opened_file(fake, home, monkeypatch):
    monkeypatch.setattr(chat, "upload", lambda *a: pytest.fail("uploaded"))
    (home / ".ssh").mkdir()
    (home / ".ssh" / "notes.txt").write_text("x")
    (home / "pub").symlink_to(home / ".ssh")  # directory symlink in the middle of the path
    with pytest.raises(ValueError, match="inside ~/.ssh"):
        server.send_file("AAA", str(home / "pub" / "notes.txt"))
    with pytest.raises(ValueError, match="inside ~/.ssh"):
        server.send_file("AAA", str(home / "pub" / ".." / ".ssh" / "notes.txt"))


def test_send_file_uploads_the_opened_descriptor(fake, home, monkeypatch):
    f = home / "report.txt"
    f.write_text("hello")
    seen = {}

    def upload(space, filename, mimetype, fh, size):
        seen.update(filename=filename, data=fh.read(), size=size)
        return {"attachmentUploadToken": "tok"}
    monkeypatch.setattr(chat, "upload", upload)
    out = server.send_file("AAA", str(f), "see @[priya@x.com]")
    assert seen == {"filename": "report.txt", "data": b"hello", "size": 5}
    assert fake.calls[0][3]["attachment"] == [{"attachmentDataRef": {"attachmentUploadToken": "tok"}}]
    assert "r.txt (text/plain)" in out and "5 bytes" in out


def test_send_file_over_200mb_is_refused(fake, home, monkeypatch):
    f = home / "big.bin"
    f.write_bytes(b"xx")
    monkeypatch.setattr(server, "UPLOAD_CAP", 1)
    with pytest.raises(ValueError, match="Chat accepts"):
        server.send_file("AAA", str(f))


# --- edit verification is separate for text and mentions; accepted writes are never raised away
def _after_patch(fake, **changes):
    real = fake.patch

    def patch(path, call, params, body):
        real(path, call, params, body)
        fake.msgs[path].update(changes)
        return {}
    chat.patch = patch


def test_edit_whose_read_back_fails_says_it_was_accepted(fake, monkeypatch):
    real_get = fake.get
    state = {"patched": False}
    real_patch = fake.patch

    def patch(*a):
        state["patched"] = True
        return real_patch(*a)

    def get(path, call, params=None):
        if state["patched"] and call == "messages.get":
            raise chat.ApiError("messages.get", 503, "down")
        return real_get(path, call, params)
    monkeypatch.setattr(chat, "patch", patch)
    monkeypatch.setattr(chat, "get", get)
    out = server.edit_message(f"{SP}/messages/mine", "new words")
    assert "could not verify" in out and "WAS accepted" in out and "do not" in out


def test_edit_text_mismatch_is_not_reported_as_verified(fake):
    _after_patch(fake, text="something else")
    out = server.edit_message(f"{SP}/messages/mine", "new words")
    assert "could not verify" in out and "text in the read-back differs" in out and "WAS accepted" in out


def test_edit_mention_annotation_missing_is_reported_separately(fake):
    _after_patch(fake, annotations=[], formattedText="hi <users/222>")
    out = server.edit_message(f"{SP}/messages/mine", "hi @[priya@x.com]")
    assert "could not verify" in out and "no mention annotation for Priya Shah" in out
    assert "text in the read-back differs" not in out  # formattedText carries the mention markup


def test_edit_with_mention_verifies_text_and_annotation(fake):
    _after_patch(fake, text="hi @Priya Shah", formattedText="hi <users/222>",
                 annotations=[{"type": "USER_MENTION", "userMention": {"user": {"name": "users/222"}}}])
    out = server.edit_message(f"{SP}/messages/mine", "hi @[priya@x.com]")
    assert "text verified, mentions confirmed: Priya Shah" in out


def test_send_whose_read_back_fails_says_it_was_posted(fake, monkeypatch):
    real_get = fake.get
    state = {"sent": False}
    real_post = fake.post

    def post(*a):
        state["sent"] = True
        return real_post(*a)

    def get(path, call, params=None):
        if state["sent"] and call == "messages.get":
            raise chat.ApiError("messages.get", 503, "down")
        return real_get(path, call, params)
    monkeypatch.setattr(chat, "post", post)
    monkeypatch.setattr(chat, "get", get)
    out = server.send_message("AAA", "hello")
    assert "could not verify" in out and "WAS accepted" in out and "do not repeat" in out


# --- start_conversation: an existing space returned by setup is "opened", not "created"
def test_setup_returning_an_older_space_is_reported_as_opened(fake, monkeypatch):
    real = fake.post

    def post(path, call, params, body):
        sp = real(path, call, params, body)
        return {**sp, "createTime": "2020-01-01T00:00:00Z"}
    monkeypatch.setattr(chat, "post", post)
    out = server.start_conversation(["priya@x.com"])
    assert out.startswith("Opened the existing") and "Created" not in out


def test_new_space_is_reported_as_created(fake, monkeypatch):
    real = fake.post
    monkeypatch.setattr(chat, "post", lambda path, call, params, body: {
        **real(path, call, params, body), "createTime": server.datetime.now(server.timezone.utc).isoformat()})
    assert server.start_conversation([], "Solo").startswith("Created")


# --- edit / delete
def test_edit_my_message_patches_text_and_reads_back(fake):
    out = server.edit_message(f"{SP}/messages/mine", "new words")
    assert fake.calls[0][:3] == ("PATCH", f"{SP}/messages/mine", "messages.patch")
    assert fake.calls[0][3:] == ({"text": "new words"}, {"updateMask": "text"})
    assert "text verified" in out


def test_edit_and_delete_refuse_someone_elses_message(fake):
    with pytest.raises(PermissionError, match="not by me"):
        server.edit_message(f"{SP}/messages/theirs", "x")
    with pytest.raises(PermissionError, match="not by me"):
        server.delete_message("theirs", conversation_id="AAA")
    assert fake.calls == []


def test_delete_my_message_and_confirm(fake):
    out = server.delete_message(f"{SP}/messages/mine")
    assert fake.calls[0][:3] == ("DELETE", f"{SP}/messages/mine", "messages.delete")
    assert "shows it deleted" in out
    with pytest.raises(ValueError, match="already deleted"):
        server.delete_message(f"{SP}/messages/mine")


def test_edit_empty_text_is_refused(fake):
    with pytest.raises(ValueError, match="delete_message"):
        server.edit_message(f"{SP}/messages/mine", "  ")


# --- read state
def test_mark_unread_since_moves_marker_back_and_reads_it(fake):
    fake.read[f"users/{ME}/{SP}/spaceReadState"] = "2026-11-23T14:30:00Z"
    out = server.mark_unread("AAA", since="2026-11-23T19:00")  # 13:30 UTC
    c = fake.calls[0]
    assert c[:3] == ("PATCH", f"users/{ME}/{SP}/spaceReadState", "users.spaces.updateReadState")
    assert c[3] == {"lastReadTime": "2026-11-23T13:29:59.999000Z"} and c[4] == {"updateMask": "lastReadTime"}
    assert "13:29:59.999000Z" in out


def test_mark_unread_does_not_move_the_marker_forward(fake):
    fake.read[f"users/{ME}/{SP}/spaceReadState"] = "2026-11-23T10:00:00Z"
    assert "left alone" in server.mark_unread("AAA", since="2026-11-23T19:00")
    assert fake.calls == []


def test_mark_unread_from_a_message_in_a_threaded_space_moves_both_markers(fake):
    fake.read[f"users/{ME}/{SP}/spaceReadState"] = "2026-11-23T14:30:00Z"
    fake.read[f"users/{ME}/{SP}/threads/T1/threadReadState"] = "2026-11-23T14:30:00Z"
    server.mark_unread("AAA", message_id="mine")
    assert [c[1] for c in fake.calls] == [f"users/{ME}/{SP}/spaceReadState", f"users/{ME}/{SP}/threads/T1/threadReadState"]
    assert fake.calls[0][3]["lastReadTime"] == "2026-11-23T13:30:00.249000Z"


def test_mark_unread_one_thread_only(fake):
    server.mark_unread("AAA", since="2026-11-23T19:00", thread_id="T1")
    assert [c[1] for c in fake.calls] == [f"users/{ME}/{SP}/threads/T1/threadReadState"]


def test_mark_unread_needs_exactly_one_anchor(fake):
    for kw in ({}, {"since": "19:00", "message_id": "mine"}):
        with pytest.raises(ValueError, match="exactly one"):
            server.mark_unread("AAA", **kw)


def test_mark_read_sets_now_and_covers_threads(fake, monkeypatch):
    fake.read[f"users/{ME}/{SP}/spaceReadState"] = "2026-11-23T12:00:00Z"
    monkeypatch.setattr(chat, "messages", lambda *a, **k: ([{"name": "m", "thread": {"name": f"{SP}/threads/T9"}}], False))
    out = server.mark_read("AAA")
    assert [c[1] for c in fake.calls] == [f"users/{ME}/{SP}/spaceReadState", f"users/{ME}/{SP}/threads/T9/threadReadState"]
    assert "Also marked 1 of 1 thread" in out


def test_read_state_scope_403_has_the_login_hint(fake, monkeypatch):
    def denied(*a):
        raise chat.ApiError("x", 403, "Request had insufficient authentication scopes.", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    monkeypatch.setattr(chat, "patch", denied)
    with pytest.raises(auth.AuthError, match="chat.users.readstate"):
        server.mark_read("AAA", thread_id="T1")


# --- start_conversation
def test_one_email_finds_dm_first_then_creates(fake):
    out = server.start_conversation(["Priya@X.com"])
    assert fake.calls[0][:3] == ("POST", "spaces:setup", "spaces.setup")
    assert fake.calls[0][3]["space"] == {"spaceType": "DIRECT_MESSAGE", "singleUserBotDm": False}
    assert fake.calls[0][3]["memberships"] == [{"member": {"name": "users/priya@x.com", "type": "HUMAN"}}]
    assert "Created `spaces/NEW`" in out and "https://chat.google.com/room/NEW" in out


def test_existing_dm_is_returned_not_duplicated(fake, monkeypatch):
    real = fake.get
    monkeypatch.setattr(chat, "get", lambda p, c, params=None: {"name": "spaces/DM1", "spaceType": "DIRECT_MESSAGE"}
                        if c == "spaces.findDirectMessage" else real(p, c, params))
    out = server.start_conversation(["priya@x.com"])
    assert fake.calls == [] and "Opened the existing `spaces/DM1`" in out and "Created" not in out


def test_name_or_several_emails_make_a_space_or_group(fake):
    server.start_conversation(["priya@x.com", "priya.n@x.com"], "Launch")
    assert fake.calls[0][3]["space"] == {"spaceType": "SPACE", "displayName": "Launch"}
    server.start_conversation([], "Solo test")
    assert fake.calls[1][3]["memberships"] == [] and fake.calls[1][3]["space"]["displayName"] == "Solo test"
    server.start_conversation(["priya@x.com", "priya.n@x.com"])
    assert fake.calls[2][3]["space"] == {"spaceType": "GROUP_CHAT"}


def test_members_chat_did_not_add_are_reported(fake):
    out = server.start_conversation(["priya@x.com", "priya.n@x.com"], "Launch")
    assert "WARNING: not in the space after setup: " in out and "priya.n@x.com" not in out.split("WARNING")[0]


@pytest.mark.parametrize("emails,name", [([], ""), (["not-an-email"], ""), (["a@b.co"] * 1 + ["x"], "N")])
def test_start_conversation_validates_input(fake, emails, name):
    with pytest.raises(ValueError):
        server.start_conversation(emails, name)
    assert fake.calls == []


# --- last review round: hard links and approval binding
import hashlib
import os

from gchat_mcp import safefile


def _stat(p):
    return safefile.fingerprint(os.stat(p))


def _no_upload(monkeypatch):
    monkeypatch.setattr(chat, "upload", lambda *a: pytest.fail("uploaded"))


def test_send_file_refuses_a_hard_linked_file(fake, home, monkeypatch):
    _no_upload(monkeypatch)
    (home / "a.txt").write_text("hello")
    os.link(home / "a.txt", home / "b.txt")
    for name in ("a.txt", "b.txt"):
        with pytest.raises(ValueError, match="hard links"):
            server.send_file("AAA", str(home / name))
    assert fake.calls == []


def test_send_file_accepts_the_approved_stat_and_refuses_a_changed_file(fake, home, monkeypatch):
    f = home / "a.txt"
    f.write_text("hello")
    approved = _stat(f)
    seen = []
    monkeypatch.setattr(chat, "upload", lambda space, name, mime, fh, size: seen.append(fh.read()) or {"attachmentUploadToken": "t"})
    server.send_file("AAA", str(f), expect_stat=approved)
    assert seen == [b"hello"]
    f.write_text("hello, and now more")  # edited after approval
    with pytest.raises(ValueError, match="not the file that was approved"):
        server.send_file("AAA", str(f), expect_stat=approved)


def test_send_file_refuses_a_different_file_swapped_in_at_the_same_path(fake, home, monkeypatch):
    _no_upload(monkeypatch)
    f = home / "a.txt"
    f.write_text("hello")
    approved = _stat(f)
    other = home / "other.txt"
    other.write_text("hello")  # same bytes and size, new inode
    os.replace(other, f)
    with pytest.raises(ValueError, match="not the file that was approved"):
        server.send_file("AAA", str(f), expect_stat=approved)


def test_send_file_checks_the_bytes_against_the_approved_sha256(fake, home, monkeypatch):
    f = home / "a.txt"
    f.write_bytes(b"AAAA")
    sha = hashlib.sha256(b"AAAA").hexdigest()
    seen = []
    monkeypatch.setattr(chat, "upload", lambda space, name, mime, fh, size: seen.append(fh.read()) or {"attachmentUploadToken": "t"})
    server.send_file("AAA", str(f), expect_sha256=sha)
    assert seen == [b"AAAA"]  # the descriptor was rewound after hashing
    st = os.stat(f)
    f.write_bytes(b"BBBB")  # same size, same inode, mtime put back: only the bytes differ
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    _no_upload(monkeypatch)
    sent = len(fake.calls)
    with pytest.raises(ValueError, match="not the ones that were approved"):
        server.send_file("AAA", str(f), expect_stat=_stat(f), expect_sha256=sha)
    assert len(fake.calls) == sent


def test_installed_hook_binds_the_approval_to_the_bytes_the_server_checks(fake, home, monkeypatch):
    import json
    import subprocess
    import sys
    from pathlib import Path
    f = home / "a.txt"
    f.write_text("hello")
    hook = Path(__file__).resolve().parents[1] / "hooks" / "gchat-footer.py"
    ev = {"tool_name": "mcp__gchat-local__send_file",
          "tool_input": {"path": str(f), "conversation_id": "AAA", "expect_sha256": "caller-supplied-nonsense"}}
    out = json.loads(subprocess.run([sys.executable, str(hook)], input=json.dumps(ev), capture_output=True, text=True, check=True).stdout)
    approved = out["hookSpecificOutput"]["updatedInput"]
    assert approved["expect_sha256"] == hashlib.sha256(b"hello").hexdigest()  # the hook overrides the caller's value
    assert "sha256 2cf24dba5fb0a30e" in out["hookSpecificOutput"]["permissionDecisionReason"]
    monkeypatch.setattr(chat, "upload", lambda space, name, mime, fh, size: {"attachmentUploadToken": "t"})
    server.send_file(**{k: v for k, v in approved.items()})
    f.write_text("HELLO")  # changed after approval
    with pytest.raises(ValueError, match="approved"):
        server.send_file(**approved)
