"""Follow chosen conversations: every new message in them, from someone other than me, as one JSON line each.

`gchat-mcp watch --chat X --chat Y` uses this. Each followed conversation keeps a cursor (the create time of the
last message reported) in the per-user watch state, so a watcher that is stopped and started again (a background
monitor that expires and is re-armed) resumes where the last one stopped: nothing in the gap is lost and nothing
is printed twice, even when two watchers follow the same conversation.
"""
import re
from datetime import datetime, timedelta, timezone

from . import chat, fmt, state, targets, triage

FETCH_CAP = 200  # newest messages read per conversation per poll
TEXT_CAP = 400
CATCH_UP = timedelta(hours=6)  # a cursor older than this restarts at "now" instead of replaying the backlog
URL_RE = re.compile(r"chat\.google\.com/(?:u/\d+/)?(?:dm|room|space|app/chat)/([A-Za-z0-9_-]+)"
                    r"|mail\.google\.com/chat/.*#chat/(?:dm|space)/([A-Za-z0-9_-]+)")


def now() -> datetime:
    return datetime.now(timezone.utc)


def conversation(ref: str) -> str:
    """`spaces/X` from `spaces/X`, a bare id, or a Google Chat link; ValueError otherwise."""
    m = URL_RE.search(ref)
    return targets.space_id(next(g for g in m.groups() if g) if m else ref)


def line(space: dict, label: str, m: dict) -> dict:
    thread = m.get("thread", {}).get("name", "")
    return {"event": "message", "conversation": label, "conversation_id": space["name"],
            "sender": chat.who(m.get("sender", {})), "message_id": m["name"],
            "thread_id": thread.split("/")[-1] if thread else "", "reply": bool(m.get("threadReply")),
            "link": space.get("spaceUri", ""),
            "text": " ".join(triage._text(m, chat.who, TEXT_CAP).split()),
            "sent_at_local": fmt.fmt_time(m["createTime"])}


class Follow:
    """The followed conversations of one watcher, with their space records (read once)."""

    def __init__(self, refs: list[str], since: str = "", include_mine: bool = False):
        self.spaces = list(dict.fromkeys(conversation(r) for r in refs))
        self.since = since
        self.include_mine = include_mine
        self.info: dict[str, tuple[dict, str]] = {}
        self.failing: set[str] = set()  # conversations whose last read failed (reported once per outage)
        self.read_at: datetime | None = None

    def _space(self, name: str) -> tuple[dict, str]:
        if name not in self.info:
            sp = chat.get(name, "spaces.get")
            self.info[name] = (sp, chat.label(sp))
        return self.info[name]

    def _start(self, now: datetime) -> str:
        return fmt.rfc3339(fmt.parse_ts(self.since)) if self.since else fmt.rfc3339(now)

    def fetch(self, at: datetime | None = None) -> tuple[dict, list[dict]]:
        """Read the messages after each cursor (no cursor moves yet, so an abandoned fetch loses nothing).
        Returns ({conversation: messages oldest first}, notes). A conversation that cannot be read is reported
        once and skipped while the others go on; only when none can be read does this raise."""
        at = at or now()
        notes: list[dict] = []
        with state.store().transact(chat.me_id()) as st:
            cur = dict(st["follow"])
            for s in self.spaces:
                c = cur.get(s)
                looked = st["follow_checked"].get(s) or c  # last successful read; older state has only the cursor
                if c is None:
                    st["follow"][s] = cur[s] = self._start(at)
                elif fmt.parse_ts(looked) < at - CATCH_UP and not self.since:
                    st["follow"][s] = cur[s] = fmt.rfc3339(at)
                    notes.append({"event": "note", "conversation_id": s,
                                  "text": f"last looked {fmt.fmt_time(looked)}; older messages were not replayed "
                                          "(read_conversation shows them)"})
        fetched, error = {}, None
        for s in self.spaces:
            try:
                msgs, more = chat.messages(s, since=cur[s], cap=FETCH_CAP)
                if msgs:
                    self._space(s)  # read the label now: once cursors move, nothing may fail before printing
            except Exception as e:  # this conversation only; the others are still read
                error = e
                if s not in self.failing:
                    self.failing.add(s)
                    notes.append({"event": "note", "conversation_id": s,
                                  "text": f"cannot read this conversation ({type(e).__name__}: {str(e)[:160]}); "
                                          "still trying, the others are watched"})
                continue
            if s in self.failing:
                self.failing.discard(s)
                notes.append({"event": "note", "conversation_id": s, "text": "readable again"})
            fetched[s] = sorted(msgs, key=lambda m: fmt.parse_ts(m["createTime"]))
            if more:
                notes.append({"event": "note", "conversation_id": s,
                              "text": f"more than {FETCH_CAP} new messages; only the newest {FETCH_CAP} are shown"})
        if error is not None and not fetched:
            raise error
        self.read_at = at
        return fetched, notes

    def commit(self, fetched: dict) -> list[dict]:
        """Move the cursors past `fetched` in one state transaction and return the messages to report, oldest
        first. A message a concurrent watcher already reported is behind the cursor and is skipped."""
        me, out = chat.me_id(), []
        looked = fmt.rfc3339(self.read_at or now())
        with state.store().transact(me) as st:
            for s, msgs in fetched.items():
                st["follow_checked"][s] = looked
                c = st["follow"].get(s)
                new = [m for m in msgs if c is None or fmt.parse_ts(m["createTime"]) > fmt.parse_ts(c)]
                if not new:
                    continue
                st["follow"][s] = new[-1]["createTime"]
                out += [(s, m) for m in new if self.include_mine or not targets.sender_is_me(m, me)]
        return [line(*self._space(s), m) for s, m in out]  # labels were read in fetch: no API call can fail here

    def step(self, at: datetime | None = None) -> list[dict]:
        fetched, notes = self.fetch(at)
        return notes + self.commit(fetched)
