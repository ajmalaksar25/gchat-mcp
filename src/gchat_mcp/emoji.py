"""Pure helpers for reactions: emoji and shortcode parsing."""
import re

KEYCAP_RE = re.compile(r"^([0-9#*])️?⃣$")
NAME_RE = re.compile(r"^:?([A-Za-z0-9_+-]{1,40}):?$")
_NUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
        "eight": "8", "nine": "9", "hash": "#", "asterisk": "*"}
SHORTCODES = {
    "+1": "👍", "thumbsup": "👍", "thumbs_up": "👍", "thumbup": "👍", "plus_one": "👍", "like": "👍",
    "-1": "👎", "thumbsdown": "👎", "thumbs_down": "👎", "minus_one": "👎",
    "eyes": "👀", "heart": "❤️", "tada": "🎉", "party": "🎉", "rocket": "🚀", "fire": "🔥", "100": "💯",
    "white_check_mark": "✅", "check": "✅", "tick": "✅", "heavy_check_mark": "✔️", "x": "❌",
    "pray": "🙏", "folded_hands": "🙏", "thanks": "🙏", "clap": "👏", "raised_hands": "🙌", "ok_hand": "👌",
    "wave": "👋", "muscle": "💪", "handshake": "🤝", "point_up": "☝️",
    "smile": "😄", "smiley": "😃", "slightly_smiling_face": "🙂", "joy": "😂", "laughing": "😆",
    "thinking": "🤔", "thinking_face": "🤔", "sunglasses": "😎", "heart_eyes": "😍", "hugs": "🤗",
    "sob": "😭", "cry": "😢", "pensive": "😔", "facepalm": "🤦", "shrug": "🤷",
    "warning": "⚠️", "question": "❓", "bulb": "💡", "star": "⭐", "sparkles": "✨", "zap": "⚡",
    "trophy": "🏆", "hourglass": "⏳", "memo": "📝", "bug": "🐛", "ten": "🔟",
}


def keycap(ch: str) -> str:
    return f"{ch}️⃣"


def same(a: str, b: str) -> bool:
    """Two emoji are the same when equal ignoring the VS16 variation selector (Chat may drop or add it)."""
    return a.replace("️", "") == b.replace("️", "")


def parse(s: str) -> str:
    """A Unicode emoji from an emoji, a keycap (`1️⃣`, `1`, `:one:`), or a shortcode (`:thumbsup:`, `+1`, `eyes`)."""
    s = (s or "").strip()
    m = KEYCAP_RE.match(s)
    if m:
        return keycap(m.group(1))
    if len(s) == 1 and s in "0123456789#*":
        return keycap(s)
    m = NAME_RE.match(s)
    if m and (m.group(1).lower() in SHORTCODES or m.group(1).lower() in _NUM or m.group(1).lower().startswith("keycap_")):
        name = m.group(1).lower().removeprefix("keycap_")
        if name in _NUM:
            return keycap(_NUM[name])
        if name in "0123456789#*" and len(name) == 1:
            return keycap(name)
        if name in SHORTCODES:
            return SHORTCODES[name]
    if s and len(s) <= 16 and not any(c.isalnum() for c in s):
        return s
    raise ValueError(f"Not a Unicode emoji or known shortcode: {s!r}")
