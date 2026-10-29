# gchat-mcp

Read-only local stdio MCP server for Google Chat, using the GA Chat REST API under your own Google OAuth (no service account).
Tools: `find_conversations`, `read_conversation`, `search_messages`, `unread`, `needs_reply`, `get_attachment` (images inline, text files as text, all saved to `~/.cache/gchat-mcp/attachments`; Drive files go to the Google Drive connector), `send_message(conversation_id, messageText, thread_id)` (posts as you with user auth; needs the `chat.messages.create` grant from `gchat-mcp login`; Chat shows the project's Chat app name next to your name).

## Setup
1. Put your OAuth "Desktop app" client at `~/.config/gchat-mcp/client_secret.json`.
2. `uv sync`
3. `uv run gchat-mcp login` (opens a browser once; token saved to `~/.config/gchat-mcp/token.json`, mode 0600)
4. Register (all aliases):
   `env -u CLAUDE_CONFIG_DIR claude mcp add -s user gchat-local -- uv run --directory /Users/ajmalaksar/Documents/projects/gchat-mcp gchat-mcp`

The server never opens a browser; without a valid token every tool says to run `gchat-mcp login`.
Tests: `uv run pytest -q`.
