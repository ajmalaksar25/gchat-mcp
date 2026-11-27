"""Pure helpers for @mentions: find `@[Name or email]` tokens, resolve each to a space member, insert `<users/ID>`.

Chat renders `<users/{id}>` in message text as a mention (docs: format-messages, "Mention users in a text message").
Raw `<users/...>` already in the text is left alone.
"""
import re

from . import fmt

TOKEN_RE = re.compile(r"@\[([^\[\]\n]{1,100})\]")
# `@all` (bare, not inside an email or word) and `@[all]`/`@[everyone]` mention everyone: Chat's `<users/all>`.
BARE_ALL_RE = re.compile(r"(?<![\w.@])@all\b(?![@.]\w)")
ALL = "users/all"
# A bare `@Name` (not `@[`, not inside an email or word, not escaped as `\@`) followed by a letter.
BARE_RE = re.compile(r"(?<![\w.@\\<])@(?=[^\W\d_])")
ALL_WORDS = {"all", "everyone"}


def is_all(query: str) -> bool:
    return query.strip().casefold() in ALL_WORDS


class MentionError(ValueError):
    pass


class NoMatch(MentionError):
    pass


class NotInSpace(MentionError):
    pass


class Ambiguous(MentionError):
    pass


def normalize(text: str) -> str:
    """Turn a bare `@all` into the `@[all]` token form, so it is resolved and verified like any mention."""
    return BARE_ALL_RE.sub("@[all]", text)


def has_bare(text: str) -> bool:
    return bool(BARE_RE.search(text))


def convert_bare(text: str, names: list[str]) -> str:
    """Rewrite each bare `@Full Name` that is exactly a member's full name (longest match, whole words, any case)
    into the `@[Full Name]` token, so it becomes a real mention; NoMatch (nothing is sent) for a bare `@Word` that
    is no member's full name, since posting it would look like a mention and notify nobody. `\\@` is a literal @."""
    out, i = [], 0
    for m in BARE_RE.finditer(text):
        p, rest = m.start(), text[m.start() + 1:]
        fits = [n for n in names if n and rest.casefold().startswith(n.casefold())
                and not (len(rest) > len(n) and (rest[len(n)].isalnum() or rest[len(n)] == "_"))]
        if not fits:
            word = re.match(r"[^\W\d_][\w'’-]*", rest).group(0)
            close = sorted({n for n in names if n.split() and n.split()[0].casefold() == word.casefold()})
            hint = f" Did you mean {' or '.join(f'@[{n}]' for n in close)}?" if close else ""
            raise NoMatch(f"'@{word}' is not a mention: no member of this space is called that. Write "
                          f"@[Full Name] or @[email] to mention someone, or \\@ for a plain @.{hint} Nothing was sent.")
        n = max(fits, key=len)
        out += [text[i:p], f"@[{rest[:len(n)]}]"]
        i = p + 1 + len(n)
    return "".join(out) + text[i:]


def unescape(text: str) -> str:
    return text.replace("\\@", "@")


def extract(text: str) -> list[str]:
    """Distinct mention queries in order of first use (call on normalized text)."""
    return list(dict.fromkeys(q.strip() for q in TOKEN_RE.findall(text) if q.strip()))


def _label(c: dict) -> str:
    return f"{c['name'] or '(no name)'} <{c['emails'][0]}>" if c.get("emails") else (c.get("name") or c["id"])


def _words_match(query: str, name: str) -> bool:
    words = name.casefold().split()
    return all(any(w.startswith(q) for w in words) for q in query.casefold().split())


def resolve(query: str, candidates: list[dict], member_ids) -> str:
    """The `users/{id}` for `query` among `candidates` ({'id','name','emails'}) who are in `member_ids`.

    An email matches exactly (case-insensitive); a name matches exactly, else every word as a name-word prefix.
    NoMatch: nobody found. NotInSpace: found, but not a member. Ambiguous: several members match.
    """
    q = query.strip()
    if "@" in q:
        found = [c for c in candidates if q.casefold() in [e.casefold() for e in c.get("emails", [])]]
    else:
        found = [c for c in candidates if c["name"].casefold() == q.casefold()] or [
            c for c in candidates if _words_match(q, c["name"])]
    if not found:
        raise NoMatch(f"No one matches {q!r} in the directory or this space.")
    inside = [c for c in found if c["id"] in member_ids]
    if not inside:
        raise NotInSpace(f"{', '.join(_label(c) for c in found)} is not a member of this space.")
    if len(inside) > 1:
        raise Ambiguous(f"{q!r} matches {len(inside)} members: {'; '.join(_label(c) for c in inside)}. "
                        "Use the full email address.")
    return f"users/{inside[0]['id']}"


def insert(text: str, resolved: dict[str, str]) -> str:
    """Replace each `@[query]` token with `<users/ID>` using `resolved` {query: 'users/ID'}."""
    return TOKEN_RE.sub(lambda m: f"<{resolved[m.group(1).strip()]}>" if m.group(1).strip() else m.group(0), text)


def missing(message: dict, expected: list[str]) -> list[str]:
    """Expected `users/ID`s with no USER_MENTION annotation in the read-back message."""
    seen = {fmt.uid(a.get("userMention", {}).get("user")) for a in message.get("annotations", [])
            if a.get("type") == "USER_MENTION"}
    text = message.get("text", "")

    def is_all_annotation(a: dict) -> bool:
        # Live read-back (23 Nov 2026): @all comes back as USER_MENTION with an empty `user`, over the text "@all".
        um = a.get("userMention", {})
        if um.get("type") == "MENTION_ALL":
            return True
        i, n = a.get("startIndex"), a.get("length")
        return not um.get("user") and isinstance(i, int) and isinstance(n, int) and text[i:i + n] == "@all"

    if any(a.get("type") == "USER_MENTION" and is_all_annotation(a) for a in message.get("annotations", [])):
        seen.add(fmt.uid(ALL))
    return [u for u in expected if fmt.uid(u) not in seen]
