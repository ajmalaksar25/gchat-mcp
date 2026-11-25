Please set up the Google Chat MCP server (`google-chat-mcp`) on this computer and connect it to you, the AI assistant I am talking to. I am new to this, so explain each thing I must do in one plain sentence.

Rules for you:
- Never ask me to paste a password, token, API key or the contents of a key file into this chat. Secrets stay in files on my computer; you only ever use their file paths.
- Do not turn on anything that sends my data to a third party. The only such option is SubQ triage: leave it off unless I ask for it, and before you turn it on, show me what `uvx --python 3.13 google-chat-mcp setup --explain-subq` prints and wait for my "yes".
- Do every step you can yourself. Give me only the steps that need me, one at a time, and wait until I say "done".
- When a command fails, read its output: it says what to do next. Follow that before you try anything else.
- Do not send, edit or delete any Google Chat message as a test.

Steps:
1. Check that `uvx` works (`uvx --version`). If it does not, install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS or Linux; on macOS `brew install uv` also works) and use the full path it prints if `uvx` is still not found. Windows is not supported: tell me and stop.
2. Run `uvx --python 3.13 google-chat-mcp setup --check`. If it says "Ready", go to step 6.
3. Run `uvx --python 3.13 google-chat-mcp setup --guide`. It prints 5 short steps that I do in my web browser in Google Cloud (each has a link, what to click, and what I should see when it worked). Give them to me one at a time, each with its one-line "What", the link, what to click and what I should see.
4. When I say I downloaded the key file, find it yourself: it is usually the newest `~/Downloads/client_secret_*.json`. Tell me: "A Google sign-in page will open. Choose your work account and click Allow. If Google says the app is not verified, click Continue: it is your own app." Then run, with a command timeout of at least 10 minutes:
   `uvx --python 3.13 google-chat-mcp setup --non-interactive --client-secret <that path>`
   It copies the key file into `~/.config/gchat-mcp/` and waits for my sign-in. If the sign-in fails, tell me what the output says and wait for me before you run `uvx --python 3.13 google-chat-mcp login` again (it opens a new sign-in page).
5. Run `uvx --python 3.13 google-chat-mcp setup --check` again. It must say "Ready". If it does not, do what it says.
6. Run `uvx --python 3.13 google-chat-mcp setup --print-config` and add the server to yourself with the part for this assistant: run the `claude mcp add` command (Claude Code) or the `codex mcp add` command (Codex CLI); for Claude Desktop or Cursor, copy the config file first, then merge the JSON into it and keep every server already in it.
7. Tell me to restart you (or open a new chat) so the new server loads, and to then ask "What needs a reply in Google Chat?". That question only reads messages.

At the end, tell me in two or three lines what you did and anything still left for me.
