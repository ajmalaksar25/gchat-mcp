from gchat_mcp import mentions


def test_bare_all_becomes_a_token():
    assert mentions.normalize("hi @all, standup moved") == "hi @[all], standup moved"
    assert mentions.extract(mentions.normalize("@all please")) == ["all"]


def test_all_inside_an_email_or_word_is_left_alone():
    for t in ["mail me at ops@all.com", "see x@all", "call@all", "@allison hi", "@all.hands"]:
        assert mentions.normalize(t) == t, t


def test_bracket_all_and_everyone_are_all():
    assert mentions.is_all("all") and mentions.is_all(" Everyone ") and not mentions.is_all("Allison")


def test_insert_all():
    assert mentions.insert("@[all] hi", {"all": mentions.ALL}) == "<users/all> hi"


def test_missing_accepts_a_mention_all_annotation():
    msg = {"annotations": [{"type": "USER_MENTION", "userMention": {"user": {"name": "users/all"}, "type": "MENTION_ALL"}}]}
    assert mentions.missing(msg, [mentions.ALL]) == []
    assert mentions.missing({"annotations": []}, [mentions.ALL]) == [mentions.ALL]


def test_missing_accepts_the_live_shape_of_an_all_annotation():
    # Shape read back from Chat on 23 Nov 2026 for "hi @all".
    msg = {"text": "Live test 5: hi @all",
           "annotations": [{"type": "USER_MENTION", "startIndex": 16, "length": 4, "userMention": {"user": {}}}]}
    assert mentions.missing(msg, [mentions.ALL]) == []
    other = {"text": "Live test 5: hi @bob", "annotations": [{"type": "USER_MENTION", "startIndex": 16, "length": 4,
                                                              "userMention": {"user": {}}}]}
    assert mentions.missing(other, [mentions.ALL]) == [mentions.ALL]
