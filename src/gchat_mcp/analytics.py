"""Opt-in usage analytics through PostHog MCP Analytics. Off unless `GCHAT_MCP_POSTHOG_KEY` is set.

When on, each tool call sends one `$mcp_tool_call` event to the PostHog project of that key: the tool name, how
long it took, whether it failed (error type and message), the client and model names, and a short intent line the
calling model writes. Tool arguments and results (message text, names, ids) are removed before anything is sent.
Events are tied to the signed-in Google account's email, so the project owner can tell people apart.
"""
import os
import sys
from importlib import metadata

import anyio

KEY_ENV, HOST_ENV, DEBUG_ENV = "GCHAT_MCP_POSTHOG_KEY", "GCHAT_MCP_POSTHOG_HOST", "GCHAT_MCP_POSTHOG_DEBUG"
DEFAULT_HOST = "https://us.i.posthog.com"
DROP = ("$mcp_parameters", "$mcp_response")  # arguments and results carry Chat content: never sent


def key() -> str:
    k = os.environ.get(KEY_ENV, "").strip()
    return "" if k.lower() in ("", "off") else k


def before_send(event: dict) -> dict | None:
    props = event.get("properties")
    if not isinstance(props, dict):
        return None  # an event shape this code does not know is dropped, never sent unfiltered
    for k in DROP:
        props.pop(k, None)
    return event


def _email() -> str:
    from . import chat
    try:
        return chat.me_email()
    except Exception:
        return ""


async def identify(request, extra):
    from posthog.mcp.types import UserIdentity
    email = await anyio.to_thread.run_sync(_email)
    return UserIdentity(distinct_id=email, properties={"email": email}) if email else None


def _version() -> str:
    try:
        return metadata.version("google-chat-mcp")
    except metadata.PackageNotFoundError:
        return "dev"


def enable(server):
    """Instrument `server` when a key is set; returns the PostHog client (call `.shutdown()` on exit) or None."""
    k = key()
    if not k:
        return None
    try:
        from posthog import Posthog
        from posthog.mcp import instrument
        from posthog.mcp.types import MCPAnalyticsOptions
    except ImportError:
        print(f"gchat-mcp: {KEY_ENV} is set but the analytics extra is not installed; run it as "
              "`uvx --from 'google-chat-mcp[analytics]' gchat-mcp` (or `uv sync --extra analytics`). "
              "Analytics stay off.", file=sys.stderr, flush=True)
        return None
    client = Posthog(k, host=os.environ.get(HOST_ENV, "").strip() or DEFAULT_HOST)
    instrument(server, client, MCPAnalyticsOptions(
        identify=identify, before_send=before_send, enable_exception_autocapture=False,
        server_build=_version(),
        logger=(lambda m: print(f"posthog: {m}", file=sys.stderr, flush=True)) if os.environ.get(DEBUG_ENV) else None))
    return client
