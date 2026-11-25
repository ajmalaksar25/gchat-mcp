"""`gchat-mcp watch`: the inbox_wait loop as a long-running process that prints one JSON line per new item.

Meant to run under a background monitor that wakes an agent on each line. It shares the per-user watch state with
the MCP tools, so a trigger is reported once across both. Read-only on Chat; never sends anything.
"""
import argparse
import json
import signal
import sys

import anyio

from . import auth, server

KINDS = ("reply_needed", "worth_a_look", "no_reply_needed", "unclassified")
BACKOFF_MAX = 900  # seconds; longest wait after repeated API errors
TEXT_CAP = 160


def line(i) -> str:
    return json.dumps({
        "category": i.category or "unclassified", "classified_by": i.classified_by, "sender": i.sender,
        "conversation": i.conversation, "conversation_id": i.conversation_id, "message_id": i.id,
        "link": i.conversation_url, "text": " ".join(i.message.split())[:TEXT_CAP], "sent_at_local": i.sent_at_local,
    }, ensure_ascii=False)


async def run(only: set[str], poll: int, since: str = "", min_age: int = 0, polls: int | None = None,
              out=sys.stdout) -> int:
    """Poll forever (or `polls` times); print the new items whose kind is in `only`. Items of other kinds that the
    server classified are recorded quietly; unclassified ones are left for an MCP client. Returns an exit code."""
    w, fails, n = server.Watch(), 0, 0

    def keep(items):
        return [i for i in items if (i.category or "unclassified") in only]

    while polls is None or n < polls:
        n += 1
        try:
            result = await server.poll(None, w, since, min_age, keep)
        except auth.AuthError as e:
            print(f"gchat-mcp watch: {e}", file=sys.stderr, flush=True)
            return 2
        except Exception as e:  # Chat or network error: back off and keep watching
            fails += 1
            wait = min(poll * 2 ** fails, BACKOFF_MAX)
            print(f"gchat-mcp watch: {type(e).__name__}: {str(e)[:200]}; retrying in {wait} s",
                  file=sys.stderr, flush=True)
            await anyio.sleep(wait)
            continue
        fails = 0
        for i in result.items if result else []:
            print(line(i), file=out, flush=True)
        if polls is None or n < polls:
            await anyio.sleep(poll)
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="gchat-mcp watch", description="Print one JSON line per new Chat item "
                                "waiting on me. Read-only. Stops cleanly on SIGTERM or Ctrl-C.")
    p.add_argument("--only", default="reply_needed,unclassified",
                   help=f"comma-separated kinds to print, from {', '.join(KINDS)} (default: %(default)s)")
    p.add_argument("--poll", type=int, default=30, help=f"seconds between polls, min {server.POLL_MIN} (default: 30)")
    p.add_argument("--since", default="", help="ISO-8601 start of the first scan (default: 3 days ago)")
    p.add_argument("--min-age", type=int, default=0, help="skip triggers newer than this many minutes")
    a = p.parse_args(argv)
    only = {k.strip() for k in a.only.split(",") if k.strip()}
    if not only or only - set(KINDS):
        p.error(f"--only takes {', '.join(KINDS)}")
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
            code = await run(only, max(server.POLL_MIN, a.poll), a.since, a.min_age)
            tg.cancel_scope.cancel()

    anyio.run(go)
    return code
