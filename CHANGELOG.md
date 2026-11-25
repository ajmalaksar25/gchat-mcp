# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
