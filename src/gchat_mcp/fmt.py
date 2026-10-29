"""Pure helpers: time/text rendering of Chat Message dicts and the needs-reply rules. No network."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SNIPPET = 200
FORWARD_CAP = 4000
TEXT_CAP = 2000


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_time(s: str) -> str:
    d = parse_ts(s).astimezone(IST)
    return f"{d.day} {d:%b} {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def uid(user) -> str:
    """User id from a User dict or a 'users/{id}' string."""
    name = user.get("name", "") if isinstance(user, dict) else (user or "")
    return name.split("/")[-1] if name.startswith("users/") else ""


def annotation_text(m: dict) -> str:
    """Meet chips, Drive chips and other rich links live in annotations, not text."""
    parts = []
    for a in m.get("annotations", []):
        if a.get("richLinkMetadata", {}).get("uri"):
            parts.append("link: " + a["richLinkMetadata"]["uri"])
        elif a.get("type") == "VIDEO_CALL":
            code = a.get("videoCallMetadata", {}).get("meetingSpace", {}).get("meetingCode")
            parts.append(f"Google Meet: https://meet.google.com/{code}" if code else "Google Meet call link")
        elif a.get("type") not in ("USER_MENTION", "SLASH_COMMAND", "CUSTOM_EMOJI", None):
            # unknown chip type: fish any URL out of the raw annotation
            url = next((v for v in _strings(a) if v.startswith("https://")), "")
            if url:
                parts.append("link: " + url)
    return "\n".join(parts)


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def attachment_lines(atts: list[dict], msg_name: str, quoted: bool = False) -> list[str]:
    """One line per attachment: Drive files get their link, uploaded files a get_attachment call."""
    out = []
    for i, a in enumerate(atts):
        fid = a.get("driveDataRef", {}).get("driveFileId")
        if fid:
            ref = f" https://drive.google.com/file/d/{fid}/view"
        else:
            ref = f' → get_attachment("{msg_name}", {i}' + (", quoted=true" if quoted else "") + ")"
        out.append(f"attachment: {a.get('contentName', 'unnamed')} ({a.get('contentType', 'unknown type')}){ref}")
    return out


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def render_message(m: dict, who, me_id: str, space_label: str = "") -> str:
    """One message as a Markdown bullet with quote/forward/attachment/chip sub-lines.

    `who(user)` maps a User dict or 'users/{id}' string to a display name.
    """
    sender = m.get("sender", {})
    name = who(sender) + (" (me)" if uid(sender) == me_id else "")
    text = _clip(m.get("text", ""), TEXT_CAP).replace("\n", "\n  ")
    head = f"- {fmt_time(m['createTime'])} **{name}**: {text}".rstrip()
    subs = []
    q = m.get("quotedMessageMetadata")
    if q:
        snap = q.get("quotedMessageSnapshot", {})
        # The snapshot sender is the author's display name, not a users/{id}.
        qsender = snap.get("sender", "")
        qname = who(qsender) if qsender.startswith("users/") else (qsender or "unknown")
        if q.get("quoteType") == "FORWARD":
            src = q.get("forwardedMetadata", {}).get("spaceDisplayName")
            subs.append(f"↪ forwarded from {qname}" + (f" in {src}" if src else "") + ": "
                        + _clip(snap.get("text", ""), FORWARD_CAP).replace("\n", "\n    "))
            subs += attachment_lines(snap.get("attachments", []), m["name"], quoted=True)
            ann = annotation_text(snap)
            if ann:
                subs.append(ann)
        else:  # REPLY is the default when quoteType is unset
            subs.append(f"↩ replying to {qname}: " + _clip(snap.get("text", ""), SNIPPET).replace("\n", " "))
            subs += ["  " + line for line in attachment_lines(snap.get("attachments", []), m["name"], quoted=True)]
    subs += attachment_lines(m.get("attachment", []), m["name"])
    ann = annotation_text(m)
    if ann:
        subs.append(ann)
    return "\n".join([head] + ["  " + s for s in subs])


def matches(m: dict, keywords: list[str]) -> bool:
    """True if every keyword appears (case-insensitive) in text, forwarded text or attachment names."""
    snap = m.get("quotedMessageMetadata", {}).get("quotedMessageSnapshot", {})
    atts = m.get("attachment", []) + snap.get("attachments", [])
    hay = " ".join([m.get("text", ""), snap.get("text", "")] + [a.get("contentName", "") for a in atts]).lower()
    return all(k.lower() in hay for k in keywords)


def _human_other(m: dict, me: str) -> bool:
    s = m.get("sender", {})
    return s.get("type") != "BOT" and uid(s) != me


def mentions_me(m: dict, me: str) -> bool:
    return any(a.get("type") == "USER_MENTION" and uid(a.get("userMention", {}).get("user")) == me
               for a in m.get("annotations", []))


def reply_reasons(msgs: list[dict], space_type: str, me: str) -> list[tuple[str, list[dict]]]:
    """Needs-reply decisions for one space; msgs chronological.

    Returns (reason, context) pairs, the trigger message last in context (up to 3 earlier).
    a: DM/group chat whose last message is from someone else.
    b: in a named space, a mention of me after my last message in that thread.
    c: in a named space, a thread I posted in whose last message is from someone else.
    """
    if space_type != "SPACE":
        return [("a", msgs[-4:])] if msgs and _human_other(msgs[-1], me) else []
    threads: dict[str, list[dict]] = {}
    for m in msgs:
        threads.setdefault(m.get("thread", {}).get("name", m["name"]), []).append(m)
    out = []
    for t in threads.values():
        last_mine = max((i for i, m in enumerate(t) if uid(m.get("sender", {})) == me), default=-1)
        ments = [i for i, m in enumerate(t) if i > last_mine and _human_other(m, me) and mentions_me(m, me)]
        if ments:
            out.append(("b", t[max(0, ments[-1] - 3):ments[-1] + 1]))
        elif last_mine >= 0 and _human_other(t[-1], me):
            out.append(("c", t[-4:]))
    return out
