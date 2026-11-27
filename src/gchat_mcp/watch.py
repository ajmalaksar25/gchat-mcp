"""`gchat-mcp watch`: a long-running process that prints one JSON line per new item.

Two sources, in one process: the inbox (the inbox_wait loop: items waiting on me, triaged) and followed
conversations (`--chat`, every new message from someone else). Meant to run under a background monitor that wakes
an agent on each line. Both keep their cursors in the per-user watch state shared with the MCP tools, so a watcher
that is restarted resumes where the last one stopped. Read-only on Chat; never sends anything.
"""
import argparse
import contextvars
import json
import signal
import sys
import time

import anyio

from . import auth, chat, follow, server

KINDS = ("reply_needed", "worth_a_look", "no_reply_needed", "unclassified")
BACKOFF_MAX = 30  # seconds; longest wait after repeated API errors (blips are short; a late message costs more)
POLL_DEADLINE = 120  # seconds; a --chat read that takes longer is abandoned and counted as a failure
DEGRADED_AFTER = 3  # consecutive failed polls before a `degraded` line goes to stdout (one per outage)
TEXT_CAP = 160


def line(i) -> str:
    return json.dumps({
        "event": "inbox", "category": i.category or "unclassified", "classified_by": i.classified_by,
        "sender": i.sender, "conversation": i.conversation, "conversation_id": i.conversation_id, "message_id": i.id,
        "link": i.conversation_url, "text": " ".join(i.message.split())[:TEXT_CAP], "sent_at_local": i.sent_at_local,
    }, ensure_ascii=False)


def emit(d: dict, out) -> None:
    print(json.dumps(d, ensure_ascii=False), file=out, flush=True)


async def _in_thread(fn, abandon: bool = False):
    def run():
        with chat.call():
            return fn()
    return await anyio.to_thread.run_sync(contextvars.copy_context().run, run, abandon_on_cancel=abandon)


async def run(only: set[str], poll: int, since: str = "", min_age: int = 0, polls: int | None = None,
              out=sys.stdout, chats: list[str] | None = None, inbox: bool = True,
              include_mine: bool = False) -> int:
    """Poll forever (or `polls` times). Inbox: print the new items whose kind is in `only`; items of other kinds
    that the server classified are recorded quietly, unclassified ones are left for an MCP client. Chats: print
    every new message in them. Returns an exit code."""
    w, fails, n, down_since = server.Watch(), 0, 0, None
    f = follow.Follow(chats, since, include_mine) if chats else None

    def keep(items):
        return [i for i in items if (i.category or "unclassified") in only]

    while polls is None or n < polls:
        n += 1
        errors = []
        try:  # the chats and the inbox fail independently: one that cannot be read never silences the other
            if f:
                try:  # the read may be abandoned at the deadline; cursors move only in the commit after it
                    with anyio.fail_after(POLL_DEADLINE):
                        fetched, events = await _in_thread(f.fetch, abandon=True)
                    for e in events + await _in_thread(lambda: f.commit(fetched)):
                        emit(e, out)
                except auth.AuthError:
                    raise
                except Exception as e:
                    errors.append(e)
            if inbox:
                try:
                    result = await server.poll(None, w, since, min_age, keep)
                    for i in result.items if result else []:
                        print(line(i), file=out, flush=True)
                except auth.AuthError:
                    raise
                except Exception as e:
                    errors.append(e)
        except auth.AuthError as e:
            print(f"gchat-mcp watch: {e}", file=sys.stderr, flush=True)
            emit({"event": "stopped", "error": str(e)[:300]}, out)
            return 2
        if errors:  # Chat or network error, or a read past the deadline: back off and keep watching
            e = errors[0]
            fails += 1
            err = "poll took longer than %d s" % POLL_DEADLINE if isinstance(e, TimeoutError) else \
                f"{type(e).__name__}: {str(e)[:200]}"
            wait = min(5 * fails, BACKOFF_MAX)
            print(f"gchat-mcp watch: {err}; retrying in {wait} s", file=sys.stderr, flush=True)
            if fails == DEGRADED_AFTER:
                down_since = time.monotonic()
                emit({"event": "degraded", "error": err, "failed_polls": fails,
                      "text": "Google Chat is not answering; still retrying, nothing is lost"}, out)
            await anyio.sleep(wait)
            continue
        if down_since is not None:
            emit({"event": "recovered", "down_seconds": round(time.monotonic() - down_since)}, out)
            down_since = None
        fails = 0
        if polls is None or n < polls:
            await anyio.sleep(poll)
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="gchat-mcp watch",
        description="Print one JSON line per new Chat item. Without --chat: items waiting on me (inbox triage). With "
                    "--chat: every new message from others in those conversations (add --inbox for both). "
                    "Restarting resumes where the last watcher stopped. Read-only. Stops cleanly on SIGTERM or Ctrl-C.",
        epilog="Lines carry \"event\": inbox | message | note | degraded | recovered | stopped.")
    p.add_argument("--chat", action="append", default=[], metavar="ID",
                   help="conversation to follow: spaces/X, X, or a Google Chat link; repeat or comma-separate")
    p.add_argument("--inbox", action="store_true", help="with --chat, also watch the inbox")
    p.add_argument("--include-mine", action="store_true", help="with --chat, also print my own messages")
    p.add_argument("--only", default="reply_needed,unclassified",
                   help=f"inbox kinds to print, from {', '.join(KINDS)} (default: %(default)s)")
    p.add_argument("--poll", type=int, default=30, help=f"seconds between polls, min {server.POLL_MIN} (default: 30)")
    p.add_argument("--since", default="",
                   help="ISO-8601 start of the first scan (inbox default: 3 days ago; chats default: now)")
    p.add_argument("--min-age", type=int, default=0, help="inbox: skip triggers newer than this many minutes")
    a = p.parse_args(argv)
    only = {k.strip() for k in a.only.split(",") if k.strip()}
    if not only or only - set(KINDS):
        p.error(f"--only takes {', '.join(KINDS)}")
    chats = [c.strip() for v in a.chat for c in v.split(",") if c.strip()]
    try:
        for c in chats:
            follow.conversation(c)
    except ValueError as e:
        p.error(str(e))
    code = 0

    async def go():
        nonlocal code
        async with anyio.create_task_group() as tg:
            async def stop():
                with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as sigs:
                    async for _ in sigs:
                        tg.cancel_scope.cancel()
                        return
            tg.start_soon(stop)
            code = await run(only, max(server.POLL_MIN, a.poll), a.since, a.min_age, chats=chats,
                             inbox=a.inbox or not chats, include_mine=a.include_mine)
            tg.cancel_scope.cancel()

    anyio.run(go)
    return code
