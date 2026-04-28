# Architecture

## The problem in one sentence

When someone joins, moves, or leaves, a dozen SaaS systems need to know — reliably, auditably, and without a human clicking through 12 admin UIs.

## The shape of the solution

```
        +---------------------+         +----------------------+
CSV  -->|      Click CLI      |-------->|     Lifecycle        |
HRIS    | (onboard/offboard)  |         |  Orchestrator        |
        +---------------------+         +----+----+-----+------+
                                             |    |     |
                                             v    v     v
                                          +-----+-----+-----+
                                          |Okta |Slack| GWS |  (Provider impls)
                                          +-----+-----+-----+
                                             |    |     |
                                             v    v     v
                                          [ real Okta API ]
                                          [ (mock calls for Slack / GWS) ]
```

A visual version lives in [`architecture.mermaid`](architecture.mermaid). The full OIDC auth-flow diagram for the Flask demo is in the same file.

## Core pieces

### `Lifecycle` orchestrator (`src/lifecycle.py`)

Coordinates per-employee actions across providers. Its contract to the CLI is:

- Takes an `Employee` (or a CSV of them) plus a `dry_run` flag.
- Returns a `BatchReport` — a list of `EmployeeResult`s each containing a list of `ProviderAction`s.
- Never throws past the per-employee boundary. One bad row in a batch of 200 does not kill the run.

Idempotency is the single most important property. An onboard step that re-runs against an already-onboarded user must be a no-op: "user exists → skip create; all desired groups already present → skip adds." This is what lets Ops re-run after a partial failure without thinking hard.

### `OktaClient` (`src/okta_client.py`)

A deliberately thin wrapper around `requests`. What it owns:

- **Auth.** Sets `Authorization: SSWS <token>` once on the Session.
- **Pagination.** Okta uses cursor-style pagination via the `Link: <…>; rel="next"` header; `_paginate()` follows those links.
- **Rate limits.** On 429, read `X-Rate-Limit-Reset`, sleep bounded, raise `OktaRateLimited`; Tenacity retries with exponential backoff.
- **Transient errors.** 5xx and connection errors are retried up to 5 times with jittered backoff.
- **Typed returns.** API blobs become `OktaUser` / `OktaGroup` dataclasses at the boundary so the rest of the code is strongly typed.

What it deliberately does **not** own: business logic. It does not decide "if user is staged, call activate" — that's the orchestrator's call.

### `Provider` interface (`src/providers/base.py`)

Every downstream system implements the same methods: `onboard(employee, dry_run)` and `offboard(email, dry_run)`, returning `List[ProviderAction]`. This is the file an engineer adding GitHub / 1Password / Zoom / Jamf would open first.

Two invariants every Provider must hold: **idempotency** (re-run is safe) and **honest dry-run** (dry_run=True never mutates state).

### Config-as-data (`config/role_mappings.yaml`)

Roles → Okta groups + Slack channels + GWS OU. A teammate adding "quant-researcher" writes three YAML lines and opens a PR; no code review cares about orchestration logic.

### Structured logging (`src/logging_setup.py`)

Every action emits one JSON line with `email`, `action`, `status`, and any relevant IDs. Pipe into SIEM; grep for `email=alan@…` at 2am and the whole story is there.

## Why custom instead of `okta-sdk-python`

The SDK is fine, but hiding the HTTP makes debugging a SaaS integration harder than it needs to be. For a ~200-line wrapper we get: exact visibility into requests/responses, a single place to reason about retries, and no awkward async-only shapes leaking into sync code. When the integration breaks at 2am, there is nothing between us and the wire.

If this were a 50-integration shop I'd reconsider — the cost/benefit tips the other way once you have enough surface area.

## SCIM vs Management API

Okta exposes two styles:

- **Management API** (used here) — Okta as the authoritative store. We call it when *we* are the source of truth.
- **SCIM** — an IETF standard (`/Users`, `/Groups` endpoints) that other IdPs and SaaS apps speak. Relevant when Okta is a *client*, pushing provisioning events to a downstream app.

For HRT-style "HRIS is source of truth, Okta is the hub," this project's direction is correct. If HRT wanted to expose its own SCIM endpoint for a third-party SaaS to push to, you'd add a Flask blueprint implementing the `/scim/v2/` schema on top of `Lifecycle`. The orchestrator doesn't change; the entry point does.

## OIDC vs SAML

Both solve SSO. Different shapes:

| | OIDC | SAML |
|---|---|---|
| Wire format | JWT (JSON) | XML with digital signatures |
| Primary use | Modern web + mobile + APIs | Legacy enterprise webapps |
| Userinfo | ID token + `/userinfo` endpoint | Assertion attributes |
| Metadata | JSON via `.well-known/openid-configuration` | XML via `/metadata` |
| Strengths | Simple, well-specced, PKCE, great mobile support | Ubiquitous in enterprise; some vendors only speak SAML |

Why OIDC is implemented and SAML is documented-only: OIDC covers 95% of new integrations and is enough to demonstrate you know what's going on under the hood. SAML would add a second Python library (`python3-saml`), a second app in Okta, and a second set of metadata exchanges, without teaching the interviewer anything new about this codebase. If HRT wants me to add a SAML SP, it's a `saml_sp.py` module that mirrors `oidc_app.py` — same Flask, same session, different XML at the boundary.

## Failure modes and what happens

| Failure | What happens |
|---|---|
| Okta returns 429 | `X-Rate-Limit-Reset` read; sleep bounded; tenacity retries up to 5x exponential |
| Okta returns 5xx | Same retry path |
| Okta group named in YAML doesn't exist | `OktaError("Unknown Okta group(s): …")` — fails loudly at that employee; batch keeps going |
| Role in CSV not in role_map | Same — per-employee failure, batch continues |
| Partial batch failure (say 180/200 ok) | Report shows 20 failures; fix them, re-run the full batch; idempotency handles the 180 already-done ones |
| Bad API token | First request 401; CLI exits with clear error before touching anything |

## What I'd build next

- **Real Slack + GWS providers** with their native SDKs and rate-limit handling.
- **Okta Event Hooks / System Log consumer** so we can react to external state changes (e.g., someone re-activated in Okta manually → automatically re-add them to the right Slack channels).
- **Self-service "access request" flow** — a small web app where a user asks for a role/group, a manager approves in Slack, the orchestrator applies. Closes the loop from "it already happened"-service in the JD.
- **OpenTelemetry traces** instead of just logs, so one `trace_id` follows a batch across every Provider call.
- **Reconcile command** (`python -m src.cli reconcile --hris hris.csv`) that diffs HRIS truth against Okta state and produces a plan of drift to correct.
