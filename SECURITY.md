# Security policy

## Reporting a vulnerability
Please report security problems privately through GitHub: **Security → Report a vulnerability** on this
repository. Do not open a public issue, and do not paste tokens, `client_secret.json`, `token.json` or real chat
messages into a report; describe the steps instead.

You can expect a first answer within 7 days. Fixes land on `main`; there are no maintained older versions.

## Scope
In scope: anything in this repository, for example a way for chat text to make the server write without the
user asking, `send_file` reading a file it should refuse, a token or secret written to logs or tool output, or
data sent somewhere the README does not list.

Out of scope: Google's APIs and OAuth, your MCP client and its model, and SubQ's service.

## How the server limits damage
- Every write tool acts as you, only on the target the user named; tool descriptions tell the model that text
  read from Chat is untrusted data, never instructions.
- Approval of writes belongs to the MCP client (see the README's approval hook).
- `send_file` opens the file once with `O_NOFOLLOW` and refuses secrets locations and symlinks.
- Tokens are stored with mode 0600 and never printed.
