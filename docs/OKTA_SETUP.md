# Okta free-tier setup (5 minutes)

Everything below works on the free **Okta Developer Edition** (formerly Okta Starter Developer). No credit card required.

## 1. Create the tenant

1. Go to <https://developer.okta.com/signup/>.
2. Sign up with email. You'll receive a temporary password and a URL like `https://dev-12345678.okta.com`. That's your **org URL**.
3. Log in, then set a permanent password + MFA.

Save:

```
OKTA_ORG_URL=https://integrator-5133269.okta.com
```

## 2. Create an API token (for the CLI)

1. In the Admin dashboard: **Security → API → Tokens → Create Token**.
2. Name it `hrt-okta-demo-cli`.
3. Copy the token value — Okta only shows it once.

Save:

```
OKTA_API_TOKEN=00k3bsY82ERl_GRnvCZeLR8jJPCzntFlKDHmJ7wuA3
```

> Tokens inherit the permissions of the user that created them. For the demo, create them under the super-admin you signed up as. In production you'd use a dedicated automation service account with the narrowest scope that works.

## 3. Create some groups the role mapping expects

The sample `config/role_mappings.yaml` references these groups. Create them under **Directory → Groups → Add Group**:

- `all-staff`
- `engineering`
- `vpn-users`
- `github-org`
- `trading-floor`
- `market-data`
- `research`
- `operations`

(You only need the ones for roles you're going to demo.)

## 4. (Optional) Create an OIDC application for the Flask login demo

1. **Applications → Applications → Create App Integration**.
2. Sign-in method: **OIDC - OpenID Connect**.
3. Application type: **Web Application**.
4. Name: `hrt-okta-demo-web`.
5. Sign-in redirect URI: `http://localhost:5000/authorization-code/callback`.
6. Sign-out redirect URI: `http://localhost:5000/`.
7. Assignments: pick "Allow everyone in your organization" for the demo (tighten in prod).

From the resulting app page, copy:

```
OIDC_CLIENT_ID=0oa126z6pn9kXfN5n698
OIDC_CLIENT_SECRET=KUueEYPsRtDZX165jpNH_03YjAQizMD3S647shyUTRX15qYsGXe-4kOYjEf6070M
OIDC_ISSUER=https://integrator-5133269.okta.com/oauth2/default
```

The issuer `…/oauth2/default` refers to Okta's default authorization server, which every developer tenant ships with. It already exposes:

```
https://dev-XXXXXXXX.okta.com/oauth2/default/.well-known/openid-configuration
```

Authlib's client hits that URL once to discover endpoints + JWKS.

## 5. Fill in `.env`

```bash
cp .env.example .env
# edit .env with the values above
```

You're ready to run `python -m src.cli onboard --csv samples/new_hires.csv`.
