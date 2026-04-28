"""Downstream provider integrations (the 'glue' layer).

Each provider implements the same Provider interface so the orchestrator
doesn't have to care whether it's talking to Okta, Slack, Google
Workspace, GitHub, or an in-house tool. Adding a new SaaS is: implement
the interface, register it in lifecycle.py.
"""

from .base import Provider, ProviderError
from .slack import SlackProvider
from .google import GoogleWorkspaceProvider

__all__ = ["Provider", "ProviderError", "SlackProvider", "GoogleWorkspaceProvider"]
