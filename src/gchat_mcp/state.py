"""Per-user watch state: which trigger messages were already reported, the cursor of each followed conversation
(`gchat-mcp watch --chat`), quiet hours, per-space mute/importance.

The state holds message ids, timestamps and settings only: never message text, tokens or credentials.
Every record is keyed by the user's Google account id (from the OAuth grant), so one server process can serve
several users without one user's state being readable by another.

`Store` is the pluggable backend. `FileStore` (default) keeps one JSON file per user under
`~/.config/gchat-mcp/state/` (or `$GCHAT_MCP_STATE_DIR`), written atomically under an exclusive file lock, so
several local MCP processes (one per client profile) share it safely. A hosted deployment implements `Store`
over a database keyed by the same user key (e.g. Firestore document per user, updated in a transaction).
"""
import contextlib
import copy
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import fmt

try:
    import fcntl
except ImportError:  # pragma: no cover (Windows)
    fcntl = None

VERSION = 1
KEEP_DAYS = 30  # reported ids older than this are forgotten (triggers that old are outside any `since`)
KEEP_MAX = 5000
DEFAULT_TZ = fmt.LOCAL.key  # until the user sets one with inbox_settings
QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
SPACE_PREFS = ("mute", "important")


def empty() -> dict:
    return {"version": VERSION, "reported": {}, "follow": {}, "follow_checked": {},
            "settings": {"quiet_hours": "", "tz": DEFAULT_TZ, "spaces": {}, "footer": ""}}


def user_key(user_id: str) -> str:
    """Opaque key for a Google account id; the store never sees the raw id in a path."""
    if not user_id:
        raise ValueError("no user id: cannot key watch state")
    return hashlib.sha256(f"gchat-mcp-state\0{user_id}".encode()).hexdigest()[:32]


# --- settings (pure)
def parse_quiet(s: str) -> tuple[int, int] | None:
    """'22:00-08:00' -> (start, end) minutes after midnight; '' or 'off' -> None; ValueError otherwise."""
    if not s or s.strip().lower() in ("off", "none"):
        return None
    m = QUIET_RE.match(s)
    if not m:
        raise ValueError(f"quiet hours must look like 22:00-08:00 (or 'off'), got {s!r}")
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError(f"quiet hours out of range: {s!r}")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise ValueError("quiet hours start and end are the same")
    return start, end


def tz_of(settings: dict) -> ZoneInfo:
    try:
        return ZoneInfo(settings.get("tz") or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def quiet_until(settings: dict, now: datetime) -> datetime | None:
    """When the current quiet period ends, or None when it is not quiet now. Handles periods across midnight."""
    q = parse_quiet(settings.get("quiet_hours", ""))
    if not q:
        return None
    start, end = q
    local = now.astimezone(tz_of(settings))
    mins = local.hour * 60 + local.minute
    inside = start <= mins < end if start < end else (mins >= start or mins < end)
    if not inside:
        return None
    end_dt = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if end_dt <= local:
        end_dt += timedelta(days=1)
    return end_dt


def space_pref(settings: dict, space: str) -> str:
    return settings.get("spaces", {}).get(space, "")


# --- reported ids (pure, on a state dict)
def is_reported(state: dict, msg: str) -> bool:
    return msg in state["reported"]


def record(state: dict, msgs, now: datetime) -> None:
    stamp = now.astimezone(timezone.utc).isoformat()
    for m in msgs:
        state["reported"].setdefault(m, stamp)
    prune(state, now)


def prune(state: dict, now: datetime) -> None:
    cut = now - timedelta(days=KEEP_DAYS)
    rep = {k: v for k, v in state["reported"].items() if datetime.fromisoformat(v) >= cut}
    if len(rep) > KEEP_MAX:
        rep = dict(sorted(rep.items(), key=lambda kv: kv[1])[-KEEP_MAX:])
    state["reported"] = rep


def _stamp_ok(v) -> bool:
    try:
        return isinstance(v, str) and datetime.fromisoformat(v).tzinfo is not None
    except ValueError:
        return False


def _normalise(d) -> dict:
    out = empty()
    if not isinstance(d, dict):
        return out
    rep = d.get("reported")
    if isinstance(rep, dict):
        out["reported"] = {str(k): v for k, v in rep.items() if _stamp_ok(v)}
    fol = d.get("follow")  # followed conversation -> create time of the last message reported
    if isinstance(fol, dict):
        out["follow"] = {str(k): v for k, v in fol.items() if _stamp_ok(v)}
    chk = d.get("follow_checked")  # followed conversation -> when it was last read successfully
    if isinstance(chk, dict):
        out["follow_checked"] = {str(k): v for k, v in chk.items() if _stamp_ok(v)}
    st = d.get("settings")
    if isinstance(st, dict):
        if isinstance(st.get("tz"), str):
            out["settings"]["tz"] = st["tz"]
        if isinstance(st.get("quiet_hours"), str):
            try:
                parse_quiet(st["quiet_hours"])
                out["settings"]["quiet_hours"] = st["quiet_hours"]
            except ValueError:
                pass  # a bad stored value means no quiet hours, never a broken watch
        if isinstance(st.get("spaces"), dict):
            out["settings"]["spaces"] = {str(k): v for k, v in st["spaces"].items() if v in SPACE_PREFS}
        if isinstance(st.get("footer"), str):
            out["settings"]["footer"] = st["footer"]
    return out


# --- stores
class Store:
    """Backend interface: `transact(user_id)` yields that user's state dict and saves it if it changed."""

    def load(self, key: str) -> dict:
        raise NotImplementedError

    def save(self, key: str, state: dict) -> None:
        raise NotImplementedError

    @contextlib.contextmanager
    def lock(self, key: str):
        yield

    @contextlib.contextmanager
    def transact(self, user_id: str):
        key = user_key(user_id)
        with self.lock(key):
            state = _normalise(self.load(key))
            before = copy.deepcopy(state)
            yield state
            if state != before:
                self.save(key, state)


class MemoryStore(Store):
    """In-process store (tests, or a hosted instance that keeps state elsewhere)."""

    def __init__(self):
        self._data: dict[str, dict] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def load(self, key):
        return copy.deepcopy(self._data.get(key, empty()))

    def save(self, key, state):
        self._data[key] = copy.deepcopy(state)

    @contextlib.contextmanager
    def lock(self, key):
        with self._guard:
            lk = self._locks.setdefault(key, threading.Lock())
        with lk:
            yield


class FileStore(Store):
    """One JSON file per user key, mode 0600 in a 0700 directory; atomic replace under an flock."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._tlock = threading.Lock()  # flock is per process+file; serialise threads too

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", key):
            raise ValueError("bad state key")
        return self.root / f"{key}.json"

    def _mkdir(self):
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def load(self, key):
        p = self._path(key)
        try:
            return json.loads(p.read_text())
        except FileNotFoundError:
            return empty()
        except (json.JSONDecodeError, UnicodeDecodeError):
            p.replace(p.with_suffix(".corrupt"))  # keep it for inspection; start clean
            return empty()

    def save(self, key, state):
        self._mkdir()
        p = self._path(key)
        tmp = p.with_suffix(f".tmp{os.getpid()}.{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    @contextlib.contextmanager
    def lock(self, key):
        self._mkdir()
        with self._tlock:
            fd = os.open(self.root / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # closing releases the flock


def default_root() -> Path:
    env = os.environ.get("GCHAT_MCP_STATE_DIR")
    return Path(env).expanduser() if env else Path.home() / ".config" / "gchat-mcp" / "state"


_store: Store | None = None
_store_lock = threading.Lock()


def store() -> Store:
    """The configured backend: $GCHAT_MCP_STATE = 'file' (default) or 'memory'."""
    global _store
    with _store_lock:
        return _store or _make()


def _make() -> Store:
    global _store
    kind = os.environ.get("GCHAT_MCP_STATE", "file")
    if kind == "memory":
        _store = MemoryStore()
    elif kind == "file":
        _store = FileStore(default_root())
    else:
        raise ValueError(f"unknown GCHAT_MCP_STATE {kind!r} (file | memory)")
    return _store


def reset() -> None:
    global _store
    _store = None
