# Remote mode: connect by URL (Claude custom connectors)

`gchat-mcp serve` runs the same tools over **Streamable HTTP** at `/mcp`, behind OAuth, so a client that connects
by URL (Claude on the web, desktop and mobile through *Settings > Connectors > Add custom connector*, or any MCP
client that speaks remote servers) can use it. Each person who connects signs in with their own Google account;
every Google call runs under that person's grant.

## How it works

```
Claude ──(1) register (DCR) ──▶ gchat-mcp ──(2) redirect ──▶ Google consent (Chat scopes)
       ◀─(4) our code ────────            ◀─(3) Google code ─
       ──(5) PKCE token request ▶  gchat-mcp exchanges the Google code, checks the allowlist, issues its own tokens
       ──(6) /mcp + Bearer ─────▶  token decrypted ─▶ that user's Google credentials ─▶ Chat API
```

- The server is the OAuth 2.1 authorization server Claude talks to: metadata at
  `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource/mcp`, dynamic client
  registration at `/register`, PKCE (S256) required, refresh tokens, revocation at `/revoke`. A request without a
  token gets `401` with a `WWW-Authenticate` header that points at the metadata.
- **No token database.** The client id, authorization code, access and refresh tokens and the `state` sent to
  Google are all encrypted, authenticated blobs (Fernet, key derived from `GCHAT_MCP_SECRET`). The Google refresh
  token lives only inside the refresh token held by Claude.
- **Single use.** An authorization code works once, and a refresh token stops working once it has been exchanged
  (rotation), so a copied token is refused. Those claims are kept in the process by default, which is only
  correct for **one instance** (`--max-instances 1`), and a restart forgets which refresh tokens were replaced.
  To run several instances, or to keep the claims across restarts, set `GCHAT_MCP_FIRESTORE_PROJECT`: each claim
  is then one Firestore document created only if it does not exist, shared by every instance. If Firestore cannot
  answer, the token is refused, never let through.
- **Who may use it:** `GCHAT_MCP_ALLOWED_USERS` is checked at sign-in, on every request and on every refresh, so
  removing someone takes effect within the hour. Client redirect URIs are limited to Claude's callbacks
  (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`), loopback addresses
  (Claude Code, MCP Inspector) and any you add in `GCHAT_MCP_REDIRECT_URIS`.
- **Revoking:** disconnecting the connector calls `/revoke`, which revokes the Google grant. You can also remove
  access at myaccount.google.com > Security > Third-party connections; the next refresh then fails and Claude
  asks to sign in again. Rotating `GCHAT_MCP_SECRET`: put the new value first and keep the old one after a comma;
  everything keeps working. Dropping the old value ends every connection made before the new one was added
  (their client registrations were sealed with it): the token endpoint answers `401`, and each person removes and
  adds the connector again, then signs in. Drop it when you need that, e.g. after a leak.
- **Differences from stdio:** `send_file` is not offered (it reads a path on the server's own disk).
  `get_attachment` saves nothing: images and text come back inline as before, other files (PDFs and the like) up
  to about 3.5 MB come back as an embedded resource with their bytes, and larger ones must be opened in Chat. Watch state (`inbox_triage(only_new=true)` cursor,
  quiet hours, muted spaces, footer) is kept on the instance's disk and is lost when the instance is replaced;
  persistent state needs a `state.Store` over a database (Firestore), not built yet.

## Configuration

| Variable | Required | What |
|---|---|---|
| `GCHAT_MCP_BASE_URL` | yes | Public https origin of the server, no path (e.g. `https://gchat-mcp-123.asia-south1.run.app`) |
| `GCHAT_MCP_GOOGLE_CLIENT_ID` | yes | OAuth client of type **Web application** (not the Desktop one `login` uses) |
| `GCHAT_MCP_GOOGLE_CLIENT_SECRET` | yes | Its secret |
| `GCHAT_MCP_SECRET` | yes | 32+ random characters: `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` |
| `GCHAT_MCP_ALLOWED_USERS` | yes | `you@company.com`, `@company.com` or both, comma-separated |
| `GCHAT_MCP_REDIRECT_URIS` | no | Extra exact redirect URIs for other MCP clients |
| `GCHAT_MCP_FIRESTORE_PROJECT` | for 2+ instances | Project of the Firestore database that keeps single-use claims |
| `GCHAT_MCP_FIRESTORE_DATABASE` | no | Firestore database id (default `(default)`) |
| `PORT`, `HOST` | no | Listen address (default `0.0.0.0:8080`) |

The other variables in the README (`GCHAT_MCP_TZ`, `GCHAT_MCP_FOOTER`, `SUBQ_API_KEY`) work as in stdio mode and
apply to everyone using the instance. `GCHAT_MCP_TZ` sets the zone shown in times: set it to yours
(e.g. `Asia/Kolkata`), since a server's zone is usually UTC.

## Set up (Google Cloud Run)

Cloud Run fits best: it runs the container as is, scales to zero, allows request timeouts up to 60 minutes (so
`inbox_wait` long-polls work), keeps secrets in Secret Manager, and lives in the same Google Cloud project as the
OAuth client. Personal use normally stays inside the free tier.

1. **OAuth client.** In the Google Cloud project you already use for gchat-mcp, open
   [Clients](https://console.cloud.google.com/auth/clients) > **Create client** > type **Web application**.
   Leave the redirect URIs empty for now, **Create**, and copy the client id and secret. Keep the sign-in screen
   Audience on **Internal** (external testing-mode grants expire after 7 days).
2. **Secrets.**
   ```sh
   gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com
   printf %s 'THE_GOOGLE_CLIENT_SECRET' | gcloud secrets create gchat-mcp-google-secret --data-file=-
   python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | gcloud secrets create gchat-mcp-secret --data-file=-
   ```
   Give the Cloud Run service account (by default `PROJECT_NUMBER-compute@developer.gserviceaccount.com`) the
   *Secret Manager Secret Accessor* role on both secrets.
3. **Deploy** from the repository root (builds the `Dockerfile`):
   ```sh
   gcloud run deploy gchat-mcp --source . --region asia-south1 \
     --allow-unauthenticated --timeout 3600 --max-instances 1 \
     --set-env-vars GCHAT_MCP_GOOGLE_CLIENT_ID=THE_CLIENT_ID,GCHAT_MCP_ALLOWED_USERS=you@company.com,GCHAT_MCP_TZ=Asia/Kolkata,GCHAT_MCP_BASE_URL=https://placeholder.invalid \
     --set-secrets GCHAT_MCP_GOOGLE_CLIENT_SECRET=gchat-mcp-google-secret:latest,GCHAT_MCP_SECRET=gchat-mcp-secret:latest
   ```
   `--allow-unauthenticated` is required: Claude must reach the URL, and the server does its own OAuth.
   `--max-instances 1` keeps the single-use checks correct without Firestore (see "Single use" above). For more
   instances: create a Firestore database in Native mode (`gcloud firestore databases create --location=asia-south1`),
   give the service account the *Cloud Datastore User* role, add
   `GCHAT_MCP_FIRESTORE_PROJECT=YOUR_PROJECT_ID` to `--set-env-vars`, and optionally add a TTL policy on the
   field `expireAt` of collection group `gchat_mcp_spent` so old claims are deleted.
4. **Point the server at its own URL.** Take the service URL that the deploy printed and run
   ```sh
   gcloud run services update gchat-mcp --region asia-south1 --update-env-vars GCHAT_MCP_BASE_URL=https://THE-SERVICE-URL
   ```
   Then in the OAuth client from step 1 add the authorized redirect URI `https://THE-SERVICE-URL/oauth/google/callback`
   and **Save**. Check `https://THE-SERVICE-URL/healthz` answers `ok`.
5. **Connect Claude.** *Settings > Connectors > Add custom connector*, name `Google Chat`, URL
   `https://THE-SERVICE-URL/mcp`, leave the OAuth fields empty (the server registers Claude itself), **Add**,
   then **Connect**: sign in with your work account and click **Allow**. For Claude Code:
   `claude mcp add --transport http gchat https://THE-SERVICE-URL/mcp`, then `/mcp` to sign in.

## Other hosts

- **Fly.io or any VM/container host:** the same image. A Fly volume mounted at `/home/app/.config/gchat-mcp` also
  keeps the watch state across restarts.
- **Vercel:** possible, since the server is stateless. Add `api/index.py` with
  `from gchat_mcp.server import http_app; app = http_app()`, route everything to it, set the variables above, and
  raise the function's `maxDuration`, because `inbox_wait` holds a request open for up to `timeout_seconds`
  (keep that below the limit). Watch state does not survive between invocations there.
- **Cloudflare Workers:** not a fit; the server depends on `google-auth` and `requests`, which do not run on
  Python Workers.

## Run it locally

```sh
uv sync --extra remote
GCHAT_MCP_BASE_URL=http://localhost:8080 GCHAT_MCP_GOOGLE_CLIENT_ID=... GCHAT_MCP_GOOGLE_CLIENT_SECRET=... \
GCHAT_MCP_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))') \
GCHAT_MCP_ALLOWED_USERS=you@company.com uv run gchat-mcp serve
```
Add `http://localhost:8080/oauth/google/callback` to the Web client's redirect URIs and try it with
`npx @modelcontextprotocol/inspector` against `http://localhost:8080/mcp`.
