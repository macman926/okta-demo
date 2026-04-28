# Going live: real Slack + Google Workspace

Status today:

- **Okta Management API** — real. `OktaClient` hits the tenant directly.
- **OIDC login demo** — real. Flask app exchanges real tokens.
- **Slack** — real when `SLACK_BOT_TOKEN` is set, falls back to mock when absent. See [`SLACK_SETUP.md`](SLACK_SETUP.md).
- **Google Workspace** — mocked. The provider logs intent but makes no API calls. Reasoning and the path to real integration are below.

## Why GWS is still mocked

Three practical reasons:

1. **Not free.** Google Workspace starts at $6 / user / month and there is no free developer tier for Admin SDK. To demo real calls you need a paid tenant.
2. **Service-account setup is ~2–4 hours.** GCP project + Admin SDK API enablement + service account + JSON key + domain-wide delegation configured in Workspace admin + super-admin to impersonate + correct scopes. Any of those going sideways burns an afternoon on `403 insufficientPermissions`.
3. **The mock-plus-interface pattern is already the right story.** The orchestrator doesn't care whether `GoogleWorkspaceProvider.onboard` is a one-line log or a full Admin SDK call — which is exactly the point of the Provider abstraction. Interviewers who understand this will recognize it; the ones who don't are the reason you document it explicitly.

## What the real `GoogleWorkspaceProvider` would look like

Sketch — not committed to the repo because it won't run without credentials, but this is the shape:

```python
# src/providers/google.py  (real version, reference only)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import Employee, Provider, ProviderAction, ProviderError

_SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/admin.directory.group.member",
]


class GoogleWorkspaceProvider(Provider):
    name = "gws"

    def __init__(self, ou_for_role, service_account_file: str, admin_email: str):
        creds = service_account.Credentials.from_service_account_file(
            service_account_file, scopes=_SCOPES,
        ).with_subject(admin_email)  # domain-wide delegation: impersonate admin
        self._svc = build("admin", "directory_v1", credentials=creds,
                          cache_discovery=False)
        self._ou_for_role = ou_for_role

    def onboard(self, employee: Employee, *, dry_run: bool):
        ou = self._ou_for_role(employee.role)
        target = employee.email
        # 1. Create the user if they don't exist.
        existing = self._try_get(target)
        if existing is None:
            body = {
                "primaryEmail": target,
                "name": {"givenName": employee.first_name,
                         "familyName": employee.last_name},
                "password": _generate_random_password(),
                "changePasswordAtNextLogin": True,
                "orgUnitPath": ou,
            }
            if dry_run:
                return [ProviderAction(self.name, "create_user", target,
                                       "planned", detail=f"ou={ou}")]
            self._svc.users().insert(body=body).execute()
            return [ProviderAction(self.name, "create_user", target,
                                   "applied", detail=f"ou={ou}")]
        # 2. User exists; ensure OU is correct (idempotent reconcile).
        if existing.get("orgUnitPath") == ou:
            return [ProviderAction(self.name, "set_ou", target,
                                   "skipped", detail="already in OU")]
        if dry_run:
            return [ProviderAction(self.name, "set_ou", target,
                                   "planned", detail=f"{existing['orgUnitPath']} -> {ou}")]
        self._svc.users().update(
            userKey=target, body={"orgUnitPath": ou}
        ).execute()
        return [ProviderAction(self.name, "set_ou", target,
                               "applied", detail=f"-> {ou}")]

    def offboard(self, email: str, *, dry_run: bool):
        if dry_run:
            return [ProviderAction(self.name, "suspend", email, "planned")]
        try:
            self._svc.users().update(
                userKey=email, body={"suspended": True}
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                return [ProviderAction(self.name, "suspend", email,
                                       "skipped", detail="user not found")]
            raise ProviderError(f"gws update failed: {e}") from e
        return [ProviderAction(self.name, "suspend", email, "applied")]

    def _try_get(self, email: str):
        try:
            return self._svc.users().get(userKey=email).execute()
        except HttpError as e:
            if e.resp.status == 404:
                return None
            raise
```

Plus in `requirements.txt`:

```
google-api-python-client>=2.120.0
google-auth>=2.29.0
```

Plus in `.env`:

```
GOOGLE_SERVICE_ACCOUNT_FILE=/abs/path/to/svc-account.json
GOOGLE_ADMIN_EMAIL=admin@your-workspace.com
```

Plus in `cli.py`:

```python
GoogleWorkspaceProvider(
    role_map.google_ou_for,
    service_account_file=settings.google_service_account_file,
    admin_email=settings.google_admin_email,
)
```

## GCP + Workspace setup (the 2–4 hours I told you not to spend tonight)

1. **Create a GCP project.** `console.cloud.google.com` → **New Project**.
2. **Enable the Admin SDK API.** APIs & Services → Library → search "Admin SDK API" → Enable.
3. **Create a service account.** IAM & Admin → Service Accounts → Create. No roles required at the project level.
4. **Generate a JSON key** for the service account. Save it somewhere safe. Never commit it.
5. **Note the service account's unique ID** (a long number, sometimes called the "client ID" for the service account).
6. **Domain-wide delegation.** In Google Workspace admin (admin.google.com, as a super admin): Security → Access and data control → API controls → Domain-wide delegation → Add new. Paste the service account's client ID. In "OAuth scopes" paste exactly:
   ```
   https://www.googleapis.com/auth/admin.directory.user,
   https://www.googleapis.com/auth/admin.directory.group.member
   ```
7. **Pick an admin to impersonate.** The service account does nothing on its own — it has to `with_subject(admin_email)` to act as a real super-admin user. Create a dedicated `automation@` account for this and give it Super Admin; that way audit logs show actions attributed to `automation@`, not to your personal admin.
8. **Test end to end** with something read-only first:
   ```python
   svc.users().list(customer="my_customer", maxResults=5).execute()
   ```
   If that returns users, you're wired up. If you get `403`, it's almost always a domain-wide delegation scope mismatch — the scopes in the admin console must match the scopes the code requests, character for character.

## What to say about GWS in the interview

> "Google Workspace is mocked today because the real integration needs a paid Workspace tenant plus domain-wide delegation setup, which isn't free and isn't fast. The code and design are set up so real GWS is a one-file change — the `GoogleWorkspaceProvider` implementation swaps in `google.oauth2.service_account.Credentials.from_service_account_file(...).with_subject(admin_email)` against the Admin SDK Directory API. The orchestrator and role-mapping layer don't change. I have a sketch in `docs/GOING_LIVE.md` if you want to see the shape."

That's honest, confident, and moves the conversation to design rather than setup trivia.
