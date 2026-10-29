from gchat_mcp import fmt

ME = "111"
NAMES = {"111": "Ajmal Aksar", "222": "Priya", "333": "Rahul"}


def who(u):
    return NAMES.get(fmt.uid(u), "Unknown")


def msg(i, sender, text, ts="2026-10-29T09:03:12.123456Z", thread="spaces/AAA/threads/T1", **kw):
    return {"name": f"spaces/AAA/messages/{i}", "sender": {"name": f"users/{sender}", "type": "HUMAN"},
            "createTime": ts, "text": text, "thread": {"name": thread}, **kw}


def test_time_is_ist():
    assert fmt.fmt_time("2026-10-29T09:03:12Z") == "29 Oct 2:33 PM"


def test_forward_expands_snapshot_and_source():
    m = msg("m1", "222", "", quotedMessageMetadata={
        "name": "spaces/BBB/messages/x", "lastUpdateTime": "2026-10-29T08:00:00Z", "quoteType": "FORWARD",
        "quotedMessageSnapshot": {"sender": "users/333", "text": "Budget is approved\nship it",
                                  "attachments": [{"contentName": "plan.pdf", "contentType": "application/pdf",
                                                   "driveDataRef": {"driveFileId": "F1"}}]},
        "forwardedMetadata": {"space": "spaces/BBB", "spaceDisplayName": "Finance"}})
    out = fmt.render_message(m, who, ME)
    assert "↪ forwarded from Rahul in Finance: Budget is approved" in out
    assert "ship it" in out
    assert "plan.pdf (application/pdf) https://drive.google.com/file/d/F1/view" in out


def test_reply_quote_and_me_marker():
    m = msg("m2", ME, "on it", quotedMessageMetadata={
        "name": "spaces/AAA/messages/m1", "lastUpdateTime": "2026-10-29T08:00:00Z",
        "quotedMessageSnapshot": {"sender": "Riya Dhawan", "text": "can you check?"}})
    out = fmt.render_message(m, who, ME)
    assert "**Ajmal Aksar (me)**: on it" in out
    assert "↩ replying to Riya Dhawan: can you check?" in out


def test_meet_and_rich_link_annotations():
    m = msg("m3", "222", "join", annotations=[
        {"type": "VIDEO_CALL", "videoCallMetadata": {"meetingSpace": {"meetingCode": "abc-defg-hij"}}},
        {"type": "RICH_LINK", "richLinkMetadata": {"uri": "https://docs.google.com/document/d/1"}}])
    out = fmt.render_message(m, who, ME)
    assert "https://meet.google.com/abc-defg-hij" in out
    assert "https://docs.google.com/document/d/1" in out


def test_keyword_match_covers_forward_and_attachment_names():
    m = msg("m4", "222", "see this", attachment=[{"contentName": "Q3-Invoice.xlsx"}], quotedMessageMetadata={
        "quoteType": "FORWARD", "quotedMessageSnapshot": {"text": "Payment due Friday"}})
    assert fmt.matches(m, ["invoice", "PAYMENT"])
    assert not fmt.matches(m, ["invoice", "refund"])


def test_needs_reply_dm_last_message_from_them():
    msgs = [msg("1", ME, "hi"), msg("2", "222", "ping?")]
    assert [r for r, _ in fmt.reply_reasons(msgs, "DIRECT_MESSAGE", ME)] == ["a"]
    assert fmt.reply_reasons(msgs[::-1], "DIRECT_MESSAGE", ME) == []


def test_needs_reply_ignores_bots():
    bot = msg("2", "999", "deploy done")
    bot["sender"]["type"] = "BOT"
    assert fmt.reply_reasons([msg("1", ME, "hi"), bot], "GROUP_CHAT", ME) == []


def mention(me=ME):
    return [{"type": "USER_MENTION", "userMention": {"user": {"name": f"users/{me}", "type": "HUMAN"}, "type": "MENTION"}}]


def test_space_mention_after_my_last_message_is_b():
    msgs = [msg("1", "222", "q"), msg("2", ME, "a"), msg("3", "333", "@Ajmal thoughts?", annotations=mention())]
    (reason, ctx), = fmt.reply_reasons(msgs, "SPACE", ME)
    assert reason == "b" and ctx[-1]["name"].endswith("/3")


def test_space_mention_already_answered_is_not_flagged():
    msgs = [msg("1", "333", "@Ajmal?", annotations=mention()), msg("2", ME, "done")]
    assert fmt.reply_reasons(msgs, "SPACE", ME) == []


def test_space_thread_i_posted_in_answered_by_other_is_c():
    msgs = [msg("1", ME, "starting"), msg("2", "222", "ok great")]
    (reason, _), = fmt.reply_reasons(msgs, "SPACE", ME)
    assert reason == "c"


def test_space_unrelated_thread_not_flagged():
    assert fmt.reply_reasons([msg("1", "222", "fyi"), msg("2", "333", "ack")], "SPACE", ME) == []


def test_uploaded_attachment_omits_signed_url():
    m = msg("m9", "222", "", attachment=[{"contentName": "log.txt", "contentType": "text/plain",
                                           "downloadUri": "https://chat.google.com/api/get_attachment_url?attachment_token=X"}])
    out = fmt.render_message(m, who, ME)
    assert 'attachment: log.txt (text/plain) → get_attachment("spaces/AAA/messages/m9", 0)' in out
    assert "attachment_token" not in out


def test_reply_to_image_shows_quoted_attachment():
    m = msg("m10", "222", "what is this?", quotedMessageMetadata={
        "name": "spaces/AAA/messages/m1", "lastUpdateTime": "2026-10-29T08:00:00Z",
        "quotedMessageSnapshot": {"sender": "Ajmal Aksar", "text": "",
                                  "attachments": [{"contentName": "card.png", "contentType": "image/png",
                                                   "attachmentDataRef": {"resourceName": "R"}}]}})
    out = fmt.render_message(m, who, ME)
    assert '↩ replying to Ajmal Aksar' in out
    assert 'card.png (image/png) → get_attachment("spaces/AAA/messages/m10", 0, quoted=true)' in out
