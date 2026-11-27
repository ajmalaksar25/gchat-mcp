# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- `gchat-mcp setup --gcloud [--project ID]`: the gcloud CLI creates the project and turns on the Chat and People
  APIs (steps 1-2); steps 3-5 open with that project already selected. The interactive wizard offers it when
  gcloud is signed in. The project id is remembered, so a rerun reuses it. A personal Gmail account or an expired
  gcloud sign-in stops before any change, with the command to fix it.
- `setup --guide` links select the remembered project.
- `setup --for claude-code,claude-desktop,claude-web,codex,cursor,other` (and a menu in the wizard): prints only
  the setup for the apps you use. `claude-web` explains the hosted (Cloud Run) option; `other` gives the generic
  command and JSON. The assistant prompt asks where you will use it, with these presets.
- README: team setup (one shared key file from an Internal project, each person signs in as themselves), and the
  assistant prompt asks about a shared key file first and uses gcloud when it is there.

## [0.3.0] - 2026-11-26
### Added
- `gchat-mcp setup`: a guided first run. It explains and links the 5 Google Cloud steps, saves the downloaded
  client file (0600), signs you in, checks the connection with one read-only call, asks about SubQ (with what is
  sent and kept) and the footer, and prints the config for Claude Code, Codex CLI, Claude Desktop and Cursor.
  Every step has a flag for non-interactive use; `--check`, `--guide`, `--print-config`, `--explain-subq`.
- `gchat-mcp setup --agent-prompt`: prints a setup prompt for an AI assistant and copies it to the clipboard.
- `google-chat-mcp` command (same as `gchat-mcp`), so `uvx google-chat-mcp` works.
- README quick start with two paths (assistant-led and manual), a hero illustration and setup diagrams.

### Changed
- The package is published as `google-chat-mcp` (`gchat-mcp` is taken on PyPI); the command stays `gchat-mcp`.
- MIT license.
- Times use the local zone by default (`GCHAT_MCP_TZ`), not a hard-coded zone.
- Optional message footer, off by default (`inbox_settings(footer=...)` or `GCHAT_MCP_FOOTER`).
- `login` stops after 10 minutes and every sign-in failure says what to do next.
- `watch` retries network blips in seconds, not minutes.

## [0.2.0]
### Added
- Inbox triage and watching: `inbox_triage`, `inbox_wait`, `inbox_settings`, the `gchat://inbox/needs-reply`
  resource, and the `gchat-mcp watch` command that prints one JSON line per new item.
- Optional SubQ Snap classification (off unless a SubQ key is configured).
- `send_file`, mentions (including `@all`), read state (`mark_read`, `mark_unread`, thread markers),
  `start_conversation`, `edit_message`, `delete_message`, `react` and `list_reactions`.
- Short message ids in every message line.

### Fixed
- DM and group-chat replies chain under the target message.
- Thread read state and edit read-back.

## [0.1.0]
### Added
- Read tools: `find_conversations`, `read_conversation`, `search_messages`, `unread`, `needs_reply`,
  `get_attachment`; `send_message` and `add_reaction`; `gchat-mcp login`.
