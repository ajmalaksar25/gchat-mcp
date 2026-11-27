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
3. Ask me one question: "Did your company or a teammate give you a gchat-mcp key file (client_secret_....json)?" If yes, find it (usually in `~/Downloads`) and go to step 4 with it: the Google Cloud steps are already done for me.
   Otherwise, if `gcloud --version` works, run `uvx --python 3.13 google-chat-mcp setup --gcloud --non-interactive`. It creates a Google Cloud project, turns on the two APIs, and prints the 3 steps left for me, with links that already select that project. If it says gcloud is not signed in or uses a personal Gmail account, tell me: "A Google page will open: choose your work account." Then run `gcloud auth login` and run the setup command again. If it says my organisation does not allow new projects, ask me for a project id I may use and add `--project <id>`. If you do not have gcloud, do not install it for this: run `uvx --python 3.13 google-chat-mcp setup --guide` instead, which prints all 5 steps.
   Give me the printed steps one at a time, each with its one-line "What", the link, what to click and what I should see.
4. When I say I downloaded the key file, find it yourself: it is usually the newest `~/Downloads/client_secret_*.json`. Tell me: "A Google sign-in page will open. Choose your work account and click Allow. If Google says the app is not verified, click Continue: it is your own app." Then run, with a command timeout of at least 10 minutes:
   `uvx --python 3.13 google-chat-mcp setup --non-interactive --client-secret <that path>`
   It copies the key file into `~/.config/gchat-mcp/` and waits for my sign-in. If the sign-in fails, tell me what the output says and wait for me before you run `uvx --python 3.13 google-chat-mcp login` again (it opens a new sign-in page).
5. Run `uvx --python 3.13 google-chat-mcp setup --check` again. It must say "Ready". If it does not, do what it says.
6. Run `uvx --python 3.13 google-chat-mcp setup --print-config` and add the server to yourself with the part for this assistant: run the `claude mcp add` command (Claude Code) or the `codex mcp add` command (Codex CLI); for Claude Desktop or Cursor, copy the config file first, then merge the JSON into it and keep every server already in it.
7. Tell me to restart you (or open a new chat) so the new server loads, and to then ask "What needs a reply in Google Chat?". That question only reads messages.

At the end, tell me in two or three lines what you did and anything still left for me.
