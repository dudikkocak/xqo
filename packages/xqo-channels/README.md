# xqo-channels: Telegram, WhatsApp, Instagram, X, Threads, Gmail, SMS

```bash
pip install xqo-channels
```

Agents can watch and answer messages on social and messaging platforms. Each platform is a **channel** in the
`"channels"` block of agents.json. Each agent's `"channels"` setting says what it may do there:

| Access | Who it serves | What happens |
|---|---|---|
| `none` (default) | nobody | The agent never sees channel messages. |
| `operator` | the people who run this HQ (the channel's `operators` list) | Their messages start tasks, Ask or Steer running ones, and answer questions. The agent's results and `reply_user` messages go back to the chat. The agent also gets `channel_send` (message an operator) and `channel_inbox` (recent traffic). |
| `responder` | everyone else the channel lets in (`allow`) | Their text reaches the agent framed as untrusted data. The task may use only `reply_user`, `ask_user`, `read_notes` and `read_attachment` (plus the agent's `channel_tools`). Replies wait for an operator's **send** unless `approve_replies` is `false`. |

With no agent holding access to a channel, its messages are ignored (and logged as blocked).

```json
"channels": {
  "telegram":  {"token_env": "XQO_TELEGRAM_TOKEN", "operators": ["123456789"]},
  "whatsapp":  {"token_env": "XQO_WA_TOKEN", "app_secret_env": "XQO_META_SECRET", "verify_token_env": "XQO_META_VERIFY",
                "phone_number_id": "1098765432", "operators": ["+447700900123"]},
  "instagram": {"token_env": "XQO_IG_TOKEN", "app_secret_env": "XQO_META_SECRET", "verify_token_env": "XQO_META_VERIFY",
                "account_id": "17841400000000000"},
  "x":         {"token_env": "XQO_X_TOKEN", "refresh_token_env": "XQO_X_REFRESH", "client_id": "abc123", "poll_seconds": 120},
  "threads":   {"token_env": "XQO_THREADS_TOKEN", "app_secret_env": "XQO_THREADS_SECRET",
                "verify_token_env": "XQO_META_VERIFY", "username": "myshop"},
  "gmail":     {"client_id": "1234-abc.apps.googleusercontent.com", "client_secret_env": "XQO_GMAIL_SECRET",
                "refresh_token_env": "XQO_GMAIL_REFRESH", "operators": ["me@myshop.example"]},
  "sms":       {"type": "twilio", "account_sid": "AC0123456789abcdef", "auth_token_env": "XQO_TWILIO_TOKEN",
                "number": "+15551234567", "operators": ["+447700900123"]},
  "webhook":   {"host": "0.0.0.0", "port": 8443}
},
"agents": [
  {"name": "planner", "skills": ["planning"], "channels": "operator"},
  {"name": "support", "skills": ["customer support"], "system": "You answer customers of My Shop: opening hours, prices, orders. ...",
   "channels": {"whatsapp": "responder", "instagram": "responder", "threads": "responder", "x": "responder",
                "gmail": "responder", "sms": "responder"}}
]
```

`"channels"` on an agent is `"none"`, `"operator"`, `"responder"`, or an object of channel name → access (`"*"`
for every other channel), for example `{"*": "operator", "whatsapp": "responder"}`. Secrets are never in the file:
every `…_env` setting names an environment variable.

On startup the server prints `Channels: telegram, whatsapp, …`, and `xqo channels` (or
`GET /api/channels`) shows each channel, its agents, today's sends, errors and the recent traffic. Each message in
or out is a `channel` event, so it shows in the message flow (filter **channel**), in replay and in forks.

## Platforms

| Channel | Gets messages by | Answers | Reply window | Notes |
|---|---|---|---|---|
| `telegram` | long polling (default, no public URL needed) or webhook | private chats (groups with `"groups": true`) | none | Free. Create a bot with @BotFather. Operators are numeric user ids (or `@username`). Questions come with buttons. |
| `whatsapp` | webhook | the WhatsApp Business number | 24 h after their last message | WhatsApp Cloud API (Meta). Replies outside the window are held and sent when they write again; an approved `"template"` can nudge them. Business service replies are billed beyond 1,000 a month per number (from Oct 2026). Meta only allows bots for a specific business purpose with a human handoff, which responder plus approvals gives you. |
| `instagram` | webhook | DMs to the professional account | 24 h | Instagram API with Instagram Login. 1,000 characters a message. The user must write first. |
| `x` | polling `GET /2/dm_events` (default, every `poll_seconds`, at least 15) or Account Activity webhook | DMs | none | Pay per use: about $0.01 per DM read and $0.015 per DM sent, so keep `poll_seconds` and `max_out_per_day` sensible. The first poll only marks the existing DMs as seen. OAuth 2.0 user token with `dm.read dm.write tweet.read users.read offline.access`; with `refresh_token_env` + `client_id` it refreshes itself (kept in `<data>/artifacts/channels/x.tokens.json`, mode 600). |
| `threads` | webhook (`replies` and `mentions` fields) | public replies under the post | none | Replies are **public**, so Threads is for responders only, and approvals stay on by default. 500 characters. One conversation per person and post. |
| `gmail` | polling the Gmail API (every `poll_seconds`, default 60) | a reply in the same thread (`Re:`, `In-Reply-To`) | none | Free. One conversation per sender address. Quoted earlier mails are cut off. Automatic, bulk and mailing-list mail (`Auto-Submitted`, `Precedence`, `List-Id`, `List-Unsubscribe`, no-reply senders) is ignored, and our replies carry `Auto-Submitted: auto-replied`, so two robots never ping-pong. Senders are easy to forge, so **a mail counts as an operator's only when Gmail saw it pass DMARC or DKIM** (`verify_operators`, default `true`); a forged one is treated as a member of the public. Commands work in the subject or the first line. |
| `sms` (`"type": "twilio"`) | polling Twilio's message list (default, every `poll_seconds`, 20) or a signed webhook | SMS from your Twilio number | none | Twilio bills per SMS segment, in and out (country rates), plus the number's monthly fee. 1,600 characters a message (Twilio splits it into segments). Picture messages are noted but not read. Operators are `+E.164` numbers. Caller ids can be spoofed on some networks, so keep risky operator actions to Telegram or the dashboard. Twilio answers `STOP` by itself. |

Two bots of the same platform: give the channels any name and set `"type"`, e.g.
`"tg_support": {"type": "telegram", ...}`.

### Setting up each platform

- **Telegram**: talk to @BotFather → `/newbot` → put the token in `XQO_TELEGRAM_TOKEN`. Send your bot a message and
  read your numeric id from `xqo channels` (blocked senders are listed), then add it to `operators`. For
  webhooks set `"mode": "webhook"`, `"webhook_secret_env"` and `"webhook_url"` (the public URL of
  `/api/channels/telegram/webhook`); the server registers it.
- **WhatsApp**: Meta developer app → WhatsApp → API setup: phone number id and a permanent (system user) token. Webhook
  callback `https://<host>/api/channels/whatsapp/webhook`, verify token = the value of `verify_token_env`, subscribe
  to `messages`. App secret from App settings → Basic.
- **Instagram**: the same Meta app with the Instagram product (Instagram Login), a professional account, permissions
  `instagram_business_basic` and `instagram_business_manage_messages`. Webhook `…/api/channels/instagram/webhook`,
  field `messages`. Accounts you don't own need App Review (Advanced Access).
- **X**: developer console → app with OAuth 2.0 (confidential or public client) → user access token with the scopes
  above. Polling needs nothing else. For webhooks set `"mode": "webhook"`, `consumer_secret_env`, and register
  `…/api/channels/x/webhook` (it answers the CRC check).
- **Gmail**: Google Cloud console → enable the Gmail API → OAuth consent screen → OAuth client (Desktop or Web).
  Get a refresh token once for the mailbox with the scopes `https://www.googleapis.com/auth/gmail.readonly` and
  `https://www.googleapis.com/auth/gmail.send` (add `gmail.modify` for `"mark_read": true`), e.g. with Google's OAuth
  Playground using your own client. Put the client secret and refresh token in environment variables. While the
  consent screen is in "Testing", Google expires refresh tokens after 7 days: publish it (internal for Workspace) for a
  lasting one. `query` narrows what is read (default `in:inbox -category:promotions -category:social`).
- **SMS (Twilio)**: buy a number in the Twilio console; account SID and auth token from the console home (or an API
  key with `api_key_sid` + `api_key_secret_env`). Polling needs nothing else. For webhooks set `"mode": "webhook"` and
  `"webhook_url"` to the exact public URL of `/api/channels/sms/webhook`, and paste the same URL as the number's
  "A message comes in" webhook (HTTP POST). Requests are checked against `X-Twilio-Signature`. In the US, sending
  from a 10-digit number needs A2P 10DLC registration.
- **Threads**: a Meta app with the Threads use case, permissions `threads_basic`, `threads_read_replies`,
  `threads_manage_replies`, `threads_content_publish` (and `threads_manage_mentions` for mentions). Webhook
  `…/api/channels/threads/webhook`, fields `replies` and `mentions`. Set `username` to the account's handle.

## Webhooks and the internet

Webhook platforms need a public HTTPS URL. **Don't expose the dashboard port**: it has no login. Instead:

- set `"webhook": {"host": "0.0.0.0", "port": 8443}`. A second listener then serves **only**
  `/api/channels/<name>/webhook`. Put TLS in front of it (Caddy, nginx, Cloudflare Tunnel, ngrok); or
- proxy just the path `/api/channels/*/webhook` of the main port.

Each request is checked before anything is read: Meta's `X-Hub-Signature-256` (app secret), X's
`x-twitter-webhooks-signature` and CRC (consumer secret), Telegram's secret-token header, Twilio's
`X-Twilio-Signature` (auth token). Unsigned or wrongly signed calls get 403. Telegram, X, Gmail and SMS poll by
default, with no public URL at all.

## Operators: driving the HQ from your phone

Any message from an operator starts a task on the channel's operator agent (`operator_agent`, or the first agent
with operator access) unless the chat's task is still running, in which case it is an **Ask** to it. Commands:

| Command | Does |
|---|---|
| `/new [@agent] text` | a new task (the agent must have operator access) |
| `/ask [@agent] text` | a question to the chat's task; a finished task is reopened |
| `/steer [@agent] text` | change the running task's instructions |
| `/status`, `/cancel`, `/pause`, `/resume` | as in the dashboard |
| `/answer N text` | answer question N (buttons do the same) |
| `/help` | the list |

Questions from agents (`ask_user`, budgets, time limits…) come to the chat with buttons. The final result comes
back too, cut at `max_reply_chars` with a pointer to the task in the dashboard. Photos and documents sent to the bot
become task attachments, within the normal attachment limits.

## Responders: answering the public safely

A message from anyone who isn't an operator (and passes `allow` / `block`, the rate limit and the new-conversation
cap) starts a task on the responder agent, titled `<Platform> · <name>`. Later messages from the same person go to
the same task as Asks, and reopen it when it has finished. After `new_task_after_hours` of quiet, a new task starts.

Defences, from the outside in:

1. **Who**: `allow` (`"*"` or a list), `block`, `groups: false`, per-sender `rate` (6 a minute, 200 a day), and
   `max_new_per_hour` (30) new conversations per channel. Duplicate deliveries are dropped by message id.
2. **Framing**: the text reaches the agent between random `<untrusted-…>` markers, with fixed instructions that it is
   data to answer, not instructions to follow.
3. **Capabilities**: the task (and anything it delegates) can use only `reply_user`, `ask_user`, `read_notes` and
   `read_attachment`. No code, files, delegation, peers, MCP servers or channel tools, unless the agent lists them in
   `"channel_tools"` (names or patterns, `"mcp:<server>"` for an MCP server). Even a successful injection can then
   only produce a bad reply.
4. **Approval**: with `approve_replies` (default `true`) every reply becomes an **approval** request: the dashboard's
   attention panel, plus a message with **send / skip** buttons to the operators (`notify`). Answer `send`, `skip`, or
   type the reply you want sent instead. With no answer within `approval_timeout` (1 h), it is skipped.
5. **Last check**: a reply containing a configured secret (any `…_env` value, `ANTHROPIC_API_KEY`, `XQO_GIT_TOKEN`)
   or something shaped like a key or token is not sent, and the operators are told.
6. **Volume**: `max_out_per_day` (500) messages per channel; platform reply windows are respected.

Give responder agents a narrow `system` prompt about your business and what they may promise. That is also what
WhatsApp's policy asks for.

## Settings reference

Every channel accepts these (shared defaults can go in `"channels": {"defaults": {...}}`):

| Setting | Default | Meaning |
|---|---|---|
| `type` | the channel's name | `telegram`, `whatsapp`, `instagram`, `x`, `threads`, `gmail`, `twilio` (or `sms`) (`console`: in-memory, for tests) |
| `enabled` | `true` | `false` keeps the settings but stops the channel |
| `operators` | `[]` | ids, `@usernames` or `+phone` numbers of the people who run this HQ |
| `allow` / `block` | `"*"` / `[]` | who else may reach responders / who is never answered |
| `operator_agent` / `responder_agent` | first agent with that access | entry agent for each kind of sender |
| `approve_replies` | `true` | responder replies wait for an operator |
| `approval_timeout` | `3600` | seconds before an unanswered approval skips the reply |
| `notify` | `true` | send operators the questions and approvals of responder tasks |
| `new_task_after_hours` | `24` | quiet time after which a responder conversation starts a new task |
| `max_inbound_chars` | `4000` | longer messages are cut |
| `max_reply_chars` | `3000` | longest reply (operators get a pointer to the dashboard beyond it) |
| `rate` | `{"per_minute": 6, "per_day": 200}` | per sender, not for operators |
| `max_new_per_hour` | `30` | new responder conversations per channel |
| `max_out_per_day` | `500` | messages sent per channel per day |
| `groups` | `false` | answer in group chats |

Platform settings: Telegram `token_env`, `mode` (`poll`/`webhook`), `webhook_secret_env`, `webhook_url`,
`poll_timeout`. WhatsApp `token_env`, `app_secret_env`, `verify_token_env`, `phone_number_id`, `template`
(`{"name", "language"}`), `api_base`. Instagram the same three secrets, `account_id`, `api_base`. X `token_env`,
`refresh_token_env`, `client_id`, `client_secret_env`, `user_id` (looked up when left out), `mode`,
`consumer_secret_env`, `poll_seconds`, `max_results`, `api_base`. Threads the three secrets, `username`,
`publish_delay` (seconds between creating and publishing a reply, default 2), `api_base`. Gmail `client_id`,
`client_secret_env`, `refresh_token_env`, `address` (looked up when left out), `display_name`, `subject` (for mail
that starts a thread), `query`, `poll_seconds`, `max_results`, `mark_read`, `verify_operators`. Twilio
`account_sid`, `auth_token_env`, `api_key_sid` + `api_key_secret_env`, `number`, `messaging_service_sid`, `mode`
(`poll`/`webhook`), `webhook_url`, `poll_seconds`, `max_results`.

Agent settings: `channels` (access, above) and `channel_tools` (extra tools in public tasks).

State (conversations, cursors, recent traffic) lives in `<data>/artifacts/channels/state.json`, so a restart picks
up where it left off. Approvals still pending at shutdown expire like other attention requests.
