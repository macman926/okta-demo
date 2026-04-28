# Interview notes — talking points for hrt-okta-demo

Tomorrow's interview is for **Systems Engineer, Enterprise Technology** at HRT. The role is Python + SaaS automation + identity + "glue a bunch of systems together." This doc is your cheat sheet: the questions most likely to come up, your one-liner answers, and the deeper follow-ups you should be ready for.

Use this as rehearsal material. Read it out loud once or twice before the call.

---

## How to open the conversation

> "I only had a day, so I scoped this deliberately: enterprise-tech work at your scale is mostly lifecycle automation and SSO plumbing, so I built a Python CLI that fans out onboard/offboard across Okta and a pluggable set of downstream providers, plus a small OIDC login demo so the protocol mechanics are visible. Okta is real; Slack and Google Workspace are mocked against a Provider interface so the architecture is honest but the setup is short. Happy to walk through it code-first or design-first — which would you prefer?"

Then let them steer. If they don't pick, start with the `README.md` section *"How it maps to the job description"* and drop into `lifecycle.py`.

---

## The five things to say unprompted

Even if they ask vague questions, get these points in:

1. **"Dry-run is the default."** Every mutating command refuses to touch Okta unless you pass `--apply`. Why: in enterprise work you're often paste-configuring from a Slack message at 5pm on a Friday, and the cost of a typo is "I just disabled the VP of Trading."

2. **"The orchestrator is idempotent."** A partial failure mid-batch is fine — you fix the bad row and re-run the whole batch. Every step checks current state before acting.

3. **"Access lives in YAML, not Python."** Adding a new role is a config PR any Ops person can review.

4. **"Provider is an interface."** Okta is real; Slack/GWS are mocks that implement the same interface. Adding GitHub or Jamf is "write one class, register it in `cli.py`." That's the "glue" bullet from the JD.

5. **"Structured JSON audit logs."** Everything the tool does produces one JSON line per action. Ready to be shipped to SIEM / ELK / Datadog.

---

## Likely questions — and what to say

### "Walk me through the onboarding flow."

> CSV hits `cli.py`, which builds a `Lifecycle` with a real `OktaClient` and the mock providers. For each row:
> 1. `Lifecycle._okta_onboard` looks up the user by login — if they exist and aren't deprovisioned, it skips create and moves to group reconciliation.
> 2. Otherwise it resolves the role's target groups from `role_mappings.yaml`, converts group names → IDs via `resolve_group_names`, then calls `create_user` with `activate=true` and `groupIds` set so the activation email and group adds happen atomically in one API call.
> 3. Downstream providers (Slack, GWS) run after Okta. Okta is the source of truth; if Okta fails, we don't half-onboard someone in Slack.
> 4. Everything is captured as `ProviderAction` dataclasses and rendered as a Rich table + emitted as JSON logs.

### "What happens on a 429 from Okta?"

> `OktaClient._request` catches it, reads `X-Rate-Limit-Reset`, sleeps bounded (capped at 30s so a stuck reset header doesn't hang the CLI), and raises `OktaRateLimited`. Tenacity catches that and retries up to 5 times with exponential backoff from 1s to 16s. 5xx and connection errors flow the same path via `OktaTransientError`.

### "How do you handle pagination?"

> Okta uses `Link: <…>; rel="next"` cursor pagination, not offset. `_paginate` walks pages by following those headers until there's no next link. First request uses relative path + params; subsequent requests use the absolute URL Okta returns, so we don't double up params.

### "Why not use the Okta Python SDK?"

> The SDK's fine, but hiding the HTTP makes debugging worse. For ~200 lines we own exactly what's on the wire, we have one place to reason about retries, and the interface is sync which matches how the orchestrator is called. If this were a 50-integration team I'd probably flip — the cost/benefit changes with surface area.

### "Walk me through the OIDC login flow."

> `/login` generates a random nonce, stashes it in the Flask session, and redirects the browser to Okta's `/authorize` with `scope=openid profile email`, the nonce, and a state parameter (Authlib handles state automatically).
> Okta shows the login UI, the user authenticates, Okta redirects back to `/authorization-code/callback?code=…`.
> Flask does `authorize_access_token()` — that's the back-channel POST to `/token` with the client secret, exchanging the code for an id_token and access_token.
> Authlib fetches `/.well-known/jwks.json` to validate the id_token's RS256 signature, then checks iss / aud / exp / nonce.
> If everything passes we put the validated claims in the session and redirect home.

### "What's the difference between OIDC and SAML?"

> Same problem, different wire format. OIDC is JSON + JWT, discovery via `.well-known/openid-configuration`, nice for mobile/API. SAML is XML with digitally signed assertions, ubiquitous in legacy enterprise. For new work OIDC is almost always right; for SAML-only vendors you write an SP. They're peers, not a progression.

### "What's SCIM, and where would it fit here?"

> SCIM is an IETF spec for `/Users` and `/Groups` REST endpoints, used for provisioning between IdPs and SaaS apps. In this project the direction is "we drive Okta via its Management API" — so Okta is our authoritative hub. If we wanted *third-party* SaaS apps to provision *into* us, we'd stand up a SCIM server implementing the SCIM schema. The `Lifecycle` class stays the same; you just wire it behind a `/scim/v2/Users` blueprint instead of a CLI.

### "How would you extend this to a new system, say GitHub?"

> New file `providers/github.py` with a `GitHubProvider(Provider)`. Implement `onboard`/`offboard`. Register it in `cli.py`. Add a `github_org` field in `role_mappings.yaml`. That's it — the orchestrator doesn't change.

### "How do you handle secrets?"

> `.env` for local dev. In production you'd want them in AWS Secrets Manager / Vault, with the CLI fetching at startup. The code is already structured for it — `Settings.from_env` is the single mutation point; you'd swap in a `Settings.from_secrets_manager()` constructor.

### "How do you test this without a real Okta tenant?"

> Hand-rolled `FakeOktaClient` in `tests/test_lifecycle.py` that implements the same interface the orchestrator consumes. I prefer a fake over `responses`/`requests-mock` because it fails at import time when the real interface drifts, instead of silently passing against a stale mock.

### "Where would this break at scale?"

> The single-process batch loop is synchronous. At 10k users you'd want to (a) page the CSV into chunks, (b) use `concurrent.futures.ThreadPoolExecutor` around the per-employee loop (Okta rate limits are per-token, not per-connection), and (c) persist progress so a crash halfway doesn't mean re-running from zero — probably a tiny SQLite state file keyed on email.

### "What's the blast radius if someone's Okta API token gets stolen?"

> Full tenant control, because this demo uses a super-admin token. Mitigations for production:
> - Dedicated service account with the smallest role that works (Group Admin / User Admin, not Super Admin).
> - Token rotated on a schedule via Okta's Token Management API.
> - Network allow-list on Okta so the token only works from specific IPs (bastion / CI runners).
> - CloudTrail-style audit — Okta's System Log captures every API action, piped to SIEM and alerted on anomalies.

### "What would you build next?"

> Four things, roughly in order of value:
> 1. A **reconcile** command that diffs HRIS truth against live Okta state and prints a drift plan.
> 2. An **Okta Event Hook consumer** so we react to external state changes (e.g., someone manually re-activated → re-join the right Slack channels).
> 3. A **self-service access request flow** — user asks for a role/group, manager approves in Slack, orchestrator applies. This is the "'it already happened'-service" bullet from your JD.
> 4. Real Slack + GWS providers to replace the mocks.

### "What would you do differently if you had a week?"

> - OpenTelemetry traces so one `trace_id` follows a batch across every provider call.
> - Use asyncio + `httpx` to get concurrent per-employee fanout without the thread pool awkwardness.
> - A small Postgres state table so we can answer "when was user X last reconciled?" without re-scanning Okta.
> - Parametrize dry-run vs apply with a Slack approval step for anything above N users — nobody should be able to offboard 50 people from a CLI without a human in the loop.

### "What's the hardest real-world problem you expect to hit in a role like this?"

> **Drift between sources of truth.** HRIS says Alice is a Researcher; Okta says Engineering; Slack has her in neither. Getting everyone to agree on one authoritative source, then automating the reconciliation without stepping on manual emergency changes Ops makes at 3am, is the whole game.

---

## Questions to ask them

(You want these ready — the interviewer always asks "do you have questions for us?")

1. "What's the current state of your identity plumbing? Is Okta the hub today, or is HRIS direct-integrating with each SaaS?"
2. "What's the most painful recurring manual task the enterprise eng team is doing today? I'd love to know what 'toil' looks like at HRT in 2026."
3. "You mentioned SCIM, config management, and self-service interfaces in the JD — which of those is most under-developed right now and would be the biggest lever for a new hire to pull?"
4. "How does the enterprise eng team interact with the quant/SRE side of HRT? Do you embed, consult, or mostly ship shared tooling?"
5. "What's a project the team shipped in the last year that you're proudest of, and what made it go well?"

---

## Final rehearsal checklist

- [ ] Can you explain the Okta pagination in 30 seconds without looking at the code?
- [ ] Can you explain the OIDC authorization code flow end-to-end in 60 seconds?
- [ ] Can you explain why SAML is documented-but-not-built without it sounding like a cop-out?
- [ ] Can you explain the dry-run default as a safety property, not just a feature?
- [ ] Can you answer "where would this break at scale?" without getting defensive?

If you can do those five things, you're ready.
