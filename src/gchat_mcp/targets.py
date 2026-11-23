"""Pure helpers: conversation/message id parsing and the sender-is-me check."""
import re

SPACE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]+$")
MESSAGE_RE = re.compile(r"^spaces/[A-Za-z0-9_-]+/messages/[A-Za-z0-9_.-]+$")
SHORT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def space_id(x: str) -> str:
    """`spaces/X` from `spaces/X` or a bare id; ValueError if it is not a conversation id."""
    x = x.strip()
    space = x if x.startswith("spaces/") else f"spaces/{x}"
    if not SPACE_RE.match(space):
        raise ValueError(f"Not a conversation id: {x!r}")
    return space


def message_ref(target: str, conversation_id: str = "") -> str:
    """Full `spaces/X/messages/Y` from the full name, or from a short id plus `conversation_id`."""
    t = target.strip()
    if MESSAGE_RE.match(t):
        if conversation_id and not t.startswith(space_id(conversation_id) + "/"):
            raise ValueError(f"{t!r} is not in {conversation_id!r}.")
        return t
    if conversation_id and SHORT_RE.match(t):
        return f"{space_id(conversation_id)}/messages/{t}"
    raise ValueError(f"Not a message id: {target!r} (expected spaces/X/messages/Y, or a short id with conversation_id).")


def sender_is_me(message: dict, me: str) -> bool:
    s = message.get("sender", {})
    return s.get("type") != "BOT" and s.get("name", "") == f"users/{me}"
