"""Configuration loader.

Loads environment variables from a .env file and parses the YAML
role-mapping config that drives provisioning behavior. Kept tiny and
dependency-light on purpose — this is the single place that knows how
to turn "environment" into "typed, validated settings".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Runtime settings, populated from env. Frozen so nothing mutates it."""

    okta_org_url: str
    okta_api_token: str
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_issuer: str = ""
    oidc_redirect_uri: str = "http://localhost:5000/authorization-code/callback"
    flask_secret_key: str = "dev-only-change-me"
    log_level: str = "INFO"
    # Optional; when empty the SlackProvider runs in mock mode.
    slack_bot_token: str = ""

    @classmethod
    def from_env(cls, dotenv_path: str | None = None) -> "Settings":
        load_dotenv(dotenv_path=dotenv_path, override=False)
        org_url = os.environ.get("OKTA_ORG_URL", "").rstrip("/")
        token = os.environ.get("OKTA_API_TOKEN", "")
        if not org_url or not token:
            raise RuntimeError(
                "OKTA_ORG_URL and OKTA_API_TOKEN must be set. "
                "Copy .env.example to .env and fill them in."
            )
        return cls(
            okta_org_url=org_url,
            okta_api_token=token,
            oidc_client_id=os.environ.get("OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET", ""),
            oidc_issuer=os.environ.get("OIDC_ISSUER", ""),
            oidc_redirect_uri=os.environ.get(
                "OIDC_REDIRECT_URI",
                "http://localhost:5000/authorization-code/callback",
            ),
            flask_secret_key=os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
        )


@dataclass(frozen=True)
class RoleMap:
    """Declarative role -> downstream-state mapping.

    A role says "software-engineer" and this tells us which Okta groups
    they need, which Slack channels to invite them to, which Google
    Workspace OU they belong in, etc. Treating the mapping as config
    (not code) is the point: a teammate can add a new role with a PR
    to a YAML file instead of touching the orchestrator.
    """

    okta_groups: Dict[str, List[str]] = field(default_factory=dict)
    slack_channels: Dict[str, List[str]] = field(default_factory=dict)
    google_ou: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RoleMap":
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        okta = {r: list(v.get("okta_groups", [])) for r, v in data.get("roles", {}).items()}
        slack = {r: list(v.get("slack_channels", [])) for r, v in data.get("roles", {}).items()}
        gws = {r: v.get("google_ou", "/") for r, v in data.get("roles", {}).items()}
        return cls(okta_groups=okta, slack_channels=slack, google_ou=gws)

    def okta_groups_for(self, role: str) -> List[str]:
        if role not in self.okta_groups:
            raise KeyError(f"Role {role!r} not defined in role_mappings.yaml")
        return list(self.okta_groups[role])

    def slack_channels_for(self, role: str) -> List[str]:
        return list(self.slack_channels.get(role, []))

    def google_ou_for(self, role: str) -> str:
        return self.google_ou.get(role, "/")
