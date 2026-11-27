#!/usr/bin/env python3
"""Claude Code hook for the local Google Chat MCP: approval for writes, and an optional client-side footer.

Register it for PreToolUse and PostToolUse (README, "Approval"). `GCHAT_MCP_HOOK_ASK` sets when it asks:
- `session` (default): once per session per tool. In modes that show Claude Code's own prompt, the hook makes no
  decision, so that prompt and its "don't ask again for this session" choice work. In bypassPermissions mode,
  which shows no prompt, the hook asks the first time a session uses a write tool; once that asked-for call has
  run (PostToolUse), the session uses that tool without asking again. Only calls this hook asked about count, and
  each MCP server's tools are approved separately. In auto mode a call the hook rewrote (footer, file binding)
  is shown for approval, since auto mode judged the original form.
- `always`: ask before every write (a hook "ask" overrides any session allow).
- `off`: no decision; Claude Code's permission settings alone decide.
`delete_message` asks every time in every setting. A "reason" names the target of each ask.

The footer: server-side footers (`inbox_settings` or `GCHAT_MCP_FOOTER`) are the default choice; this hook appends
`GCHAT_MCP_HOOK_FOOTER` only when a user opts into client-side labelling with that variable, and never to a call
that passes `footer: false` (the user asked to send that one without it).
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

FOOTER = os.environ.get("GCHAT_MCP_HOOK_FOOTER", "").strip()
FOOTER_ON = bool(FOOTER) and FOOTER.lower() != "off"
# Tool name suffix -> argument that carries message text (gets the footer).
TEXT_ARG = {"send_message": "messageText", "send_file": "text", "edit_message": "new_text"}
ASK = os.environ.get("GCHAT_MCP_HOOK_ASK", "session").strip().lower()
ALWAYS_ASK = {"delete_message"}
APPROVALS = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "gchat-mcp" / "hook-approvals"
KEEP_SECONDS = 3 * 86400


def _approval_file(session: str) -> Path:
    return APPROVALS / (hashlib.sha256(session.encode()).hexdigest()[:24] + ".json")


def _load(session: str) -> dict:
    try:
        d = json.loads(_approval_file(session).read_text())
        return {"approved": set(d.get("approved", [])), "pending": set(d.get("pending", []))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"approved": set(), "pending": set()}


def _save(session: str, d: dict) -> None:
    APPROVALS.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for f in APPROVALS.glob("*.json"):  # old sessions' files
        try:
            if now - f.stat().st_mtime > KEEP_SECONDS:
                f.unlink()
        except OSError:
            pass
    path = _approval_file(session)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: sorted(v) for k, v in d.items()}))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def approved(session: str, tool_name: str) -> bool:
    return tool_name in _load(session)["approved"]


def mark_asked(session: str, tool_name: str) -> None:
    """This hook is about to ask for `tool_name` in this session; PostToolUse turns that into an approval."""
    d = _load(session)
    d["pending"].add(tool_name)
    _save(session, d)


def remember(session: str, tool_name: str) -> None:
    """The call ran. It counts as a session approval only when this hook asked for it (bypassPermissions);
    a call that ran without the hook asking (another mode, an allow rule) approves nothing."""
    d = _load(session)
    if tool_name in d["pending"]:
        d["pending"].discard(tool_name)
        d["approved"].add(tool_name)
        _save(session, d)


def decide(event: dict, tool: str, changed: bool) -> str | None:
    """"ask", "allow" or None (no decision: Claude Code's own permission flow decides). Approvals are kept per
    full tool name, so each Chat server (gchat-local, a second account...) is approved on its own."""
    if tool in ALWAYS_ASK or ASK == "always":
        return "ask"
    mode = event.get("permission_mode")
    if ASK == "off":
        return None
    if mode == "auto":  # auto mode may refuse a rewritten call it judged in its original form: show it instead
        return "ask" if changed else None
    if mode != "bypassPermissions":
        return None
    return "allow" if approved(str(event.get("session_id", "")), str(event.get("tool_name", ""))) else "ask"


def bind_file(tool_input: dict) -> None:
    """Record the approved file's identity in the tool input: send_file refuses anything that differs.

    expect_stat = dev:ino:size:mtime_ns of the resolved path; expect_sha256 = SHA-256 of its bytes (files up to
    200 MB, within a time budget; left empty otherwise, the stat binding still applies).
    """
    real = os.path.realpath(os.path.expanduser(tool_input["path"]))
    tool_input["path"] = real
    tool_input["expect_stat"] = tool_input["expect_sha256"] = ""
    try:
        st = os.stat(real)
        tool_input["expect_stat"] = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"
        if 0 < st.st_size <= 200 << 20:
            h, end = hashlib.sha256(), time.monotonic() + 6
            with open(real, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
                    if time.monotonic() > end:
                        return
            tool_input["expect_sha256"] = h.hexdigest()
    except OSError:
        pass  # the server refuses an unreadable or missing file itself


def _bytes_note(a: dict) -> str:
    sha, st = a.get("expect_sha256"), a.get("expect_stat")
    size = st.split(":")[2] if st else "?"
    return f", {size} bytes, sha256 {sha[:16]}..." if sha else f", {size} bytes, no sha256"


def reason(tool: str, a: dict) -> str:
    if tool == "add_reaction":
        return f'Google Chat reaction: {a.get("emoji") or "👍"} on {a.get("message_id")}'
    if tool == "react":
        verb = "remove reaction" if a.get("remove") else "reaction"
        return f'Google Chat {verb}: {a.get("emoji") or "👍"} on {a.get("target")} {a.get("conversation_id") or ""}'.rstrip()
    if tool == "send_file":
        given = str(a.get("path"))
        real = os.path.realpath(os.path.expanduser(given))
        note = "" if real == given else f" (you gave {given}; it resolves to the path shown)"
        return f'Google Chat file: {real}{note}{_bytes_note(a)} to {a.get("conversation_id")}'
    if tool == "mark_read":
        return f'Google Chat mark read: {a.get("conversation_id")} {a.get("thread_id") or ""}'.rstrip()
    if tool == "mark_unread":
        return (f'Google Chat mark unread: {a.get("conversation_id")} from '
                f'{a.get("since") or a.get("message_id")} {a.get("thread_id") or ""}').rstrip()
    if tool == "start_conversation":
        return f'Google Chat start conversation: {a.get("emails")} {a.get("name") or ""}'.rstrip()
    if tool == "edit_message":
        return f'Google Chat edit: {a.get("message_id")}' + (" (footer added)" if FOOTER_ON else "")
    if tool == "delete_message":
        return f'Google Chat DELETE: {a.get("message_id")}'
    if tool == "set_notifications":
        return f'Google Chat notifications for {a.get("conversation_id")}: notify={a.get("notify") or "unchanged"}, mute={a.get("mute") or "unchanged"}'
    return "Google Chat send" + (": footer added" if FOOTER_ON else "")


event = json.load(sys.stdin)
tool = str(event.get("tool_name", "")).rsplit("__", 1)[-1]
session, tool_name = str(event.get("session_id", "")), str(event.get("tool_name", ""))
if event.get("hook_event_name") == "PostToolUse":
    if ASK == "session" and tool not in ALWAYS_ASK and session:
        remember(session, tool_name)
    sys.exit(0)
tool_input = dict(event.get("tool_input") or {})
out = {"hookEventName": "PreToolUse"}
changed = False
if tool == "send_file" and isinstance(tool_input.get("path"), str):
    # what is approved is what is sent: the resolved path plus the file's identity and hash, re-checked by the
    # server on the descriptor it opens (it also refuses symlinks at the end and hard-linked files)
    bind_file(tool_input)
    changed = True
arg = TEXT_ARG.get(tool)
if arg and tool_input.get("footer") is not False:
    text = tool_input.get(arg)
    if text is None and tool == "send_file":
        text = ""  # when footers are on, a file without a caption still carries one
    if isinstance(text, str) and FOOTER_ON and not text.rstrip().endswith(FOOTER):
        tool_input[arg] = (f"{text.rstrip()}\n\n" if text.strip() else "") + FOOTER
        tool_input["client_footer"] = FOOTER  # so the server's read-back reports the footer this hook added
        changed = True
if changed:
    out["updatedInput"] = tool_input
decision = decide(event, tool, changed)
if (decision == "ask" and ASK == "session" and tool not in ALWAYS_ASK and session
        and event.get("permission_mode") == "bypassPermissions"):
    mark_asked(session, tool_name)
if decision:
    shown = {**tool_input, "path": str(event["tool_input"].get("path"))} if tool == "send_file" else tool_input
    why = reason(tool, shown)
    if decision == "ask" and ASK == "session" and tool not in ALWAYS_ASK:
        why += ". Approving lets this session use this tool without asking again."
    out.update(permissionDecision=decision, permissionDecisionReason=why)
if decision or changed:
    print(json.dumps({"hookSpecificOutput": out}))
