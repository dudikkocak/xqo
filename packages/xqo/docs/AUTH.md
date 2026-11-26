# Login (optional)

The dashboard has no login by default, and then it must stay on `127.0.0.1`. Turn login on to reach it from other
machines: people sign in with **Google** or **Telegram**, and only identities on an allowlist get in. Scripts and the
`xqo` command line use an **API token**.

Nothing changes until you add an `"auth"` block to `agents.json` or set `XQO_AUTH`.

## Turn it on

```json
"auth": {
  "mode": ["google", "telegram"],
  "allow": ["me@gmail.com", "*@company.com", "tg:123456789"],
  "public_url": "https://hq.example.com",
  "session_hours": 168,
  "linking": true
}
```

| Setting | Environment variable | Meaning |
|---|---|---|
| `mode` | `XQO_AUTH` | `off` (default), `any` (Google + Telegram), or a list of `google`, `telegram`, `token` |
| `allow` | `XQO_AUTH_ALLOW` (comma-separated) | who may sign in: a Google email, `*@domain`, or `tg:<numeric Telegram user id>` |
| `public_url` | `XQO_PUBLIC_URL` | the address people open, e.g. `https://hq.example.com`; needed for Google off localhost; `https` makes cookies `Secure` |
| `session_hours` | | how long a sign-in lasts (default 168, one week) |
| `linking` | | signed-in people may link their other account (default true) |
| `public_paths` | | extra path prefixes that need no sign-in (default none) |
| `api_token_env` | | the variable that holds the API token (default `XQO_API_TOKEN`) |
| `google.client_id_env`, `google.client_secret_env` | `XQO_GOOGLE_CLIENT_ID`, `XQO_GOOGLE_CLIENT_SECRET` | the Google OAuth client |
| `telegram.bot_token_env`, `telegram.bot_username` | `XQO_TELEGRAM_LOGIN_BOT_TOKEN` | the login bot (the username is looked up when not given) |
| | `XQO_SESSION_SECRET` | key that signs session cookies; without it one is generated in `<data>/auth/secret` |
| | `XQO_API_TOKEN` | bearer token for the CLI, scripts and other HQs' office relay |

The server refuses to start when login is on but the allowlist is empty, or a provider's variables are not set.
Taking someone off the allowlist ends their sessions at their next request (after a restart or reload of the config).

## Google

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create a project, set up the OAuth
   consent screen (type External, status Testing is fine for a few people; add yourself as a test user).
2. Create an **OAuth client ID** of type **Web application**. Add the authorized redirect URI
   `<public_url>/auth/google/callback`, for example `https://hq.example.com/auth/google/callback`. For local use,
   `http://127.0.0.1:8000/auth/google/callback` works without a domain.
3. Set `XQO_GOOGLE_CLIENT_ID` and `XQO_GOOGLE_CLIENT_SECRET`.

xqo asks only for `openid email profile`, never for Gmail access. It uses the authorization code flow with PKCE,
`state` and `nonce`, and checks the ID token's issuer, audience, expiry, nonce and `email_verified`. Use a separate
OAuth client from the Gmail channel's.

## Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and set `XQO_TELEGRAM_LOGIN_BOT_TOKEN`.
2. Find your numeric user id (for example with [@userinfobot](https://t.me/userinfobot)) and add `tg:<id>` to `allow`.

Signing in: press **Sign in with Telegram**, open the `t.me/<bot>?start=…` link, tap **Start**, then **Approve**. The
message shows the address and browser that asked, so approve only your own request. The link works once, for 3
minutes, and only the browser that asked for it gets the session.

The bot polls Telegram only while a sign-in is waiting, so no public domain or webhook is needed and it works on
localhost. Use a **separate bot** from the one `xqo-channels` uses: Telegram gives a bot's updates to one reader only.

## Linking accounts

When both providers are on, the 👤 menu in the header offers **Link Google** or **Link Telegram**. After linking, either
account signs in as the same person, and only the account you signed in with first needs to be on the allowlist.
**Unlink** removes a link and ends that account's sessions. Links are kept in `<data>/auth/links.json`. Set
`"linking": false` to turn linking off.

## API token and the command line

With `XQO_API_TOKEN` set, every request with `Authorization: Bearer <token>` is let in, and the `xqo` command sends
it by itself. Add `token` to `mode` to also offer a token field on the sign-in page (handy as a fallback). Ten wrong
tokens from one address within 10 minutes are answered with 429.

```bash
export XQO_API_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
curl -H "Authorization: Bearer $XQO_API_TOKEN" https://hq.example.com/api/tasks
```

Other HQs' office relay: give the entry in `office.hqs` a `token_env` with that HQ's token (otherwise this server's
own `XQO_API_TOKEN` is sent).

## Running it remotely

Put the dashboard behind a TLS reverse proxy and set `public_url` to the `https://` address. Caddy:

```
hq.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Keep xqo itself on `127.0.0.1` (or the Docker mapping `127.0.0.1:8000:8000`) so only the proxy reaches it.

## What is protected

- Every page, `/static/*`, every `/api/*` route (plugins' too) and the live event stream need a session or the token.
  Pages redirect to `/login`; API calls answer 401.
- Open without sign-in: `/login`, `/auth/*`, `/api/auth/me`, channel webhooks (`/api/channels/<name>/webhook`, each
  checks its own secret), `POST /api/feeds/<name>/events` (token or signature per feed), and `/api/health`, which then
  answers only `{"ok": true}` so Docker's health check keeps working.
- The peer listener (`peering.listen`) is a separate server with its own tokens and is not affected.
- Requests that change something with a session cookie must come from this site (`Origin` or `Referer`), which blocks
  cross-site requests. Requests with the API token skip this check.
- Session cookies are signed, `HttpOnly`, `SameSite=Lax`, and `Secure` with an `https` public URL. Their name is
  unique per HQ, so several HQs on one host do not sign each other out. **Sign out everywhere** ends every session.
- Sign-ins, refusals, links and sign-outs are written to `<data>/auth/audit.log`.
- Everyone on the allowlist has full control: there are no roles.

## Docker

`docker-compose.yml` passes `XQO_AUTH`, `XQO_AUTH_ALLOW`, `XQO_PUBLIC_URL`, `XQO_SESSION_SECRET`, `XQO_API_TOKEN`,
`XQO_GOOGLE_CLIENT_ID`, `XQO_GOOGLE_CLIENT_SECRET` and `XQO_TELEGRAM_LOGIN_BOT_TOKEN` from `.env`. Login state lives in
`data/auth/`.
