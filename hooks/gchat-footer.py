#!/usr/bin/env python3
"""PreToolUse for the local Google Chat MCP: ask before every write; optionally append a client-side footer.

Every write tool asks for confirmation ("ask") with a reason that names the target, so the user sees the final
text, file, reaction, read-state change, new conversation or edit/delete before it happens. Server-side footers
(`inbox_settings` or `GCHAT_MCP_FOOTER`) are the default choice; this hook appends `GCHAT_MCP_HOOK_FOOTER` only
when a user opts into client-side labelling with that variable.
"""
import hashlib
import json
import os
import sys
import time

FOOTER = os.environ.get("GCHAT_MCP_HOOK_FOOTER", "").strip()
FOOTER_ON = bool(FOOTER) and FOOTER.lower() not in ("off", "none")
# Tool name suffix -> argument that carries message text (gets the footer).
TEXT_ARG = {"send_message": "messageText", "send_file": "text", "edit_message": "new_text"}


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
    return "Google Chat send" + (": footer added" if FOOTER_ON else "")


event = json.load(sys.stdin)
tool = str(event.get("tool_name", "")).rsplit("__", 1)[-1]
tool_input = dict(event.get("tool_input") or {})
out = {"hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": reason(tool, tool_input)}
if tool == "send_file" and isinstance(tool_input.get("path"), str):
    # what is approved is what is sent: the resolved path plus the file's identity and hash, re-checked by the
    # server on the descriptor it opens (it also refuses symlinks at the end and hard-linked files)
    bind_file(tool_input)
    out["updatedInput"] = tool_input
    out["permissionDecisionReason"] = reason(tool, {**tool_input, "path": str(event["tool_input"].get("path"))})
arg = TEXT_ARG.get(tool)
if arg:
    text = tool_input.get(arg)
    if text is None and tool == "send_file":
        text = ""  # when footers are on, a file without a caption still carries one
    if isinstance(text, str):
        if FOOTER_ON and not text.rstrip().endswith(FOOTER):
            tool_input[arg] = (f"{text.rstrip()}\n\n" if text.strip() else "") + FOOTER
        out["updatedInput"] = tool_input
    elif tool == "send_message":
        sys.exit(0)  # unchanged legacy behaviour: no text, no decision
print(json.dumps({"hookSpecificOutput": out}))
