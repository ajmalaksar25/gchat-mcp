"""Pure helpers: when to move a space/thread `lastReadTime` so messages show as unread."""
import re
from datetime import datetime, timedelta, timezone

from . import fmt

CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
GAP = timedelta(milliseconds=1)


def parse_when(s: str, now: datetime | None = None) -> datetime:
    """A moment from `HH:MM` (today, local time) or an ISO-8601 date/datetime (naive = local time)."""
    now = now or datetime.now(timezone.utc)
    s = s.strip()
    m = CLOCK_RE.match(s)
    if m:
        d = now.astimezone(fmt.LOCAL).replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
        if d > now:
            raise ValueError(f"{s} today ({fmt.LOCAL.key}) is in the future: give a date and time.")
        return d
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=fmt.LOCAL)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def unread_from(target: datetime, current: str = "") -> str | None:
    """New `lastReadTime` so everything created at or after `target` is unread; None when the current marker
    is already that early (moving it forward would mark messages read)."""
    new = target.astimezone(timezone.utc) - GAP
    if current and fmt.parse_ts(current) <= new:
        return None
    return stamp(new)


def just_before(target: datetime) -> str:
    """The `lastReadTime` that leaves a message created at `target` unread (same value `unread_from` writes)."""
    return stamp(target.astimezone(timezone.utc) - GAP)
