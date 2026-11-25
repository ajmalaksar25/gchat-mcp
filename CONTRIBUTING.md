# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Develop
```sh
uv sync
uv run ruff check .
uv run python -m pytest -q
```
CI runs the same two commands on Ubuntu and macOS for every pull request.

## Rules for changes
- **One concern per pull request**, with tests for the behaviour it changes, including the failure path.
- **Tests never call Google or SubQ.** Use the in-memory Chat server in `tests/transport.py`; a recorded vendor
  response goes in `tests/fixtures/` with made-up names and text.
- **No real data**: no real names, emails, space ids, message text or tokens in code, tests, fixtures, issues or
  pull requests.
- **Writes stay safe**: a new write tool acts only on an explicit target, checks what it can before the API call,
  reads back the result, and never returns a success message for a failed or unverified write.
- **Check the current docs** of the Google API you call and link them in the pull request.
- Update `README.md` (tools, configuration, data that leaves the machine) and `CHANGELOG.md` in the same change.

## Reporting bugs
Open an issue with the template. For security problems, follow `SECURITY.md` instead.
