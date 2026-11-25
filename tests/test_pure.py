from datetime import datetime, timedelta, timezone

import pytest

from gchat_mcp import chat, emoji, fmt, mentions, readstate, targets

PRIYA = {"id": "222", "name": "Priya Shah", "emails": ["priya@x.com"]}
PRIYA2 = {"id": "333", "name": "Priya Nair", "emails": ["priya.n@x.com"]}
RAHUL = {"id": "444", "name": "Rahul K", "emails": ["rahul@x.com"]}
ALL = [PRIYA, PRIYA2, RAHUL]


# --- mentions
def test_extract_finds_distinct_tokens_in_order():
    assert mentions.extract("hi @[Priya Shah], cc @[rahul@x.com] and @[Priya Shah]. a@b.com is not one") == [
        "Priya Shah", "rahul@x.com"]


def test_resolve_by_email_exact_name_and_name_prefix():
    members = {"222", "333", "444"}
    assert mentions.resolve("PRIYA@x.com", ALL, members) == "users/222"
    assert mentions.resolve("priya nair", ALL, members) == "users/333"
    assert mentions.resolve("rah", ALL, members) == "users/444"


def test_ambiguous_name_lists_the_candidates():
    with pytest.raises(mentions.Ambiguous, match=r"Priya Shah <priya@x.com>; Priya Nair <priya.n@x.com>"):
        mentions.resolve("Priya", ALL, {"222", "333"})


def test_space_membership_disambiguates_and_is_required():
    assert mentions.resolve("Priya", ALL, {"333", "444"}) == "users/333"
    with pytest.raises(mentions.NotInSpace, match="not a member"):
        mentions.resolve("rahul@x.com", ALL, {"222"})


def test_unknown_person_is_no_match():
    with pytest.raises(mentions.NoMatch):
        mentions.resolve("zed@x.com", ALL, {"222"})


def test_insert_replaces_tokens_and_leaves_raw_syntax_alone():
    out = mentions.insert("hi @[Priya Shah] and <users/9>", {"Priya Shah": "users/222"})
    assert out == "hi <users/222> and <users/9>"


def test_missing_reads_annotations_from_the_read_back():
    m = {"annotations": [{"type": "USER_MENTION", "userMention": {"user": {"name": "users/222"}, "type": "MENTION"}},
                         {"type": "RICH_LINK"}]}
    assert mentions.missing(m, ["users/222", "users/444"]) == ["users/444"]


# --- emoji
@pytest.mark.parametrize("given,want", [
    ("👍", "👍"), (":thumbsup:", "👍"), ("+1", "👍"), (":+1:", "👍"), ("-1", "👎"), (":eyes:", "👀"),
    ("white_check_mark", "✅"), ("1️⃣", "1️⃣"), ("1⃣", "1️⃣"), ("3", "3️⃣"), (":two:", "2️⃣"),
    ("keycap_9", "9️⃣"), (":ten:", "🔟"), ("#", "#️⃣"), ("👍🏽", "👍🏽"), (" 🎉 ", "🎉")])
def test_emoji_parse(given, want):
    assert emoji.parse(given) == want


@pytest.mark.parametrize("bad", ["", "ok", ":nonsense:", "hello", "a" * 30])
def test_emoji_parse_refuses_text(bad):
    with pytest.raises(ValueError, match="Not a Unicode emoji"):
        emoji.parse(bad)


def test_emoji_same_ignores_variation_selector():
    assert emoji.same("❤", "❤️") and not emoji.same("❤️", "👍")


# --- targets
def test_message_ref_forms():
    full = "spaces/AAA/messages/m1.m1"
    assert targets.message_ref(full) == full
    assert targets.message_ref("m1.m1", "AAA") == full
    assert targets.message_ref("m1.m1", "spaces/AAA") == full
    assert targets.message_ref(full, "spaces/AAA") == full
    with pytest.raises(ValueError, match="not in"):
        targets.message_ref(full, "spaces/BBB")
    for bad in ["m1.m1", "spaces/AAA", "spaces/AAA/messages/"]:
        with pytest.raises(ValueError, match="Not a message id"):
            targets.message_ref(bad)


def test_space_id_validates():
    assert targets.space_id("AAA") == "spaces/AAA"
    with pytest.raises(ValueError):
        targets.space_id("spaces/AA/../x")


def test_sender_is_me():
    assert targets.sender_is_me({"sender": {"name": "users/111", "type": "HUMAN"}}, "111")
    assert not targets.sender_is_me({"sender": {"name": "users/222", "type": "HUMAN"}}, "111")
    assert not targets.sender_is_me({"sender": {"name": "users/111", "type": "BOT"}}, "111")
    assert not targets.sender_is_me({}, "111")


# --- read state
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)  # 20:30 IST


def test_parse_when_clock_is_today_in_ist():
    assert readstate.parse_when("19:00", NOW) == datetime(2026, 11, 23, 19, 0, tzinfo=fmt.LOCAL)
    with pytest.raises(ValueError, match="future"):
        readstate.parse_when("21:00", NOW)


def test_parse_when_iso_naive_is_ist_and_aware_kept():
    assert readstate.parse_when("2026-11-23T19:00") == datetime(2026, 11, 23, 19, 0, tzinfo=fmt.LOCAL)
    assert readstate.parse_when("2026-11-23T19:00:00Z").utcoffset() == timedelta(0)
    assert readstate.parse_when("2026-11-23").hour == 0


def test_unread_from_sets_marker_just_before_the_target():
    target = datetime(2026, 11, 23, 19, 0, tzinfo=fmt.LOCAL)  # 13:30:00 UTC
    assert readstate.unread_from(target, "2026-11-23T14:00:00Z") == "2026-11-23T13:29:59.999000Z"


def test_unread_from_never_moves_the_marker_forward():
    target = datetime(2026, 11, 23, 19, 0, tzinfo=fmt.LOCAL)
    assert readstate.unread_from(target, "2026-11-23T10:00:00Z") is None
    assert readstate.unread_from(target, "") == "2026-11-23T13:29:59.999000Z"


def test_unread_from_message_time_keeps_microseconds_below_the_message():
    t = fmt.parse_ts("2026-11-23T13:30:00.123456Z")
    new = readstate.unread_from(t, "2026-11-23T14:00:00Z")
    assert fmt.parse_ts(new) < t and t - fmt.parse_ts(new) == timedelta(milliseconds=1)


# --- multipart upload body
def test_multipart_body_frames_metadata_file_and_has_exact_length():
    import io
    f = io.BytesIO(b"hello")
    mp = chat.Multipart({"filename": "a.txt"}, "text/plain", f, 5, boundary="B")
    body = mp.read()
    assert body == (b'--B\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{"filename": "a.txt"}\r\n'
                    b"--B\r\nContent-Type: text/plain\r\n\r\nhello\r\n--B--\r\n")
    assert len(mp) == len(body) and mp.content_type == "multipart/related; boundary=B"


def test_multipart_reads_in_small_chunks():
    import io
    mp = chat.Multipart({"filename": "a"}, "x/y", io.BytesIO(b"0123456789"), 10, boundary="B")
    out, n = b"", 0
    while chunk := mp.read(7):
        out += chunk
        n += 1
    assert n > 3 and len(out) == len(mp)


def test_local_zone_follows_setting_and_refuses_a_bad_one(monkeypatch):
    monkeypatch.setenv("GCHAT_MCP_TZ", "Europe/Berlin")
    assert fmt.local_zone().key == "Europe/Berlin"
    monkeypatch.setenv("GCHAT_MCP_TZ", "Mars/Olympus")
    with pytest.raises(ValueError, match="GCHAT_MCP_TZ"):
        fmt.local_zone()
    monkeypatch.delenv("GCHAT_MCP_TZ")
    monkeypatch.setenv("TZ", "America/Chicago")
    assert fmt.local_zone().key == "America/Chicago"
