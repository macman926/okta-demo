# Slack setup (5 minutes, free)

This gets you a real Slack bot token so `SlackProvider` stops running in mock mode. No paid Slack plan required.

## 1. Have a workspace to test against

Use a personal test workspace. If you don't have one, create it at <https://slack.com/create> — takes 60 seconds.

> Do **not** install this bot into a production workspace. Use a throwaway one for the demo so the interviewer sees real calls without any risk.

## 2. Create a Slack app

1. Go to <https://api.slack.com/apps> → **Create New App → From scratch**.
2. Name: `hrt-okta-demo`. Pick your test workspace.

## 3. Add bot scopes

In the app config, go to **OAuth & Permissions → Bot Token Scopes** and add:

| Scope | Why |
|---|---|
| `users:read` | Lookup members |
| `users:read.email` | Resolve users by email (this is the one Okta provisioning keys off) |
| `channels:read` | List public channels to resolve `#general` → channel ID |
| `channels:manage` | Invite/kick in public channels |
| `groups:write` | Same for private channels (optional) |
| `chat:write` | Optional; lets you post a welcome message |

## 4. Install the app to the workspace

Scroll up to **OAuth Tokens** → click **Install to Workspace** → authorize.

You'll get a **Bot User OAuth Token** starting with `xoxb-`. Copy it.

## 5. Invite the bot to your channels

Slack bots can only invite people to channels they themselves are members of. In the workspace, for each channel referenced in `config/role_mappings.yaml` (`general`, `engineering`, `trading`, `research`, `ops`, etc.):

```
/invite @hrt-okta-demo
```

> If you're demoing against only one or two roles, just create the 2-3 channels you need and invite the bot there. Don't try to set all 10+ up.

## 6. Drop the token in `.env`

```bash
SLACK_BOT_TOKEN=xoxb-...
```

Re-run any `onboard` / `offboard` command and you should see the SlackProvider make real calls. The mock-mode log line `slack provider running in MOCK mode` disappears.

## What works on free Slack vs what doesn't

| Action | Free | Pro / Business+ | Enterprise Grid |
|---|:-:|:-:|:-:|
| `users.lookupByEmail` | yes | yes | yes |
| `conversations.invite` / `kick` | yes | yes | yes |
| `users.conversations` (list a user's channels) | yes | yes | yes |
| `admin.users.setInactive` (full workspace deactivation) | no | no | **yes** |
| SCIM endpoint (`/scim/v2/Users`) | no | no | **yes** |

Translation: on free Slack, offboarding can remove a user from every channel and post a goodbye message, but actually deactivating their Slack account is a manual click-through by a workspace admin. The code surfaces this as a `deactivate_workspace: skipped — requires Enterprise Grid` action, which is a useful honest signal in the demo: "here's what the tool can do today, here's what's blocked by licensing."

## Troubleshooting

**`not_in_channel` errors on invite** — the bot isn't a member of that channel. Run `/invite @hrt-okta-demo` in the target channel.

**`missing_scope` errors** — you added scopes but didn't reinstall the app. In **OAuth & Permissions** click **Reinstall to Workspace**.

**`ratelimited` during large batches** — slack-sdk handles this for you (RateLimitErrorRetryHandler). If you're running 500+ invites at once you'll see it pause briefly; that's expected.

**Testing without spamming anyone** — in `config/role_mappings.yaml`, point roles at a single `bot-test` channel for demo runs so you're not pinging 10 real ones.
