"""Provider interface.

A Provider knows how to onboard/offboard a user in one downstream
system. The orchestrator treats all providers uniformly.

Two invariants every Provider implementation MUST hold:

1. Idempotency
   Onboarding a user who's already fully onboarded must be a no-op
   (and succeed). Offboarding someone already gone must be a no-op.
   This is what lets us re-run a failed batch safely.

2. Honest dry-run
   When dry_run=True, the provider must not mutate anything and must
   report what it *would* do. This is how a reviewer audits a plan
   before hitting enter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List


class ProviderError(RuntimeError):
    """Raised when a provider operation fails in a non-retryable way."""


@dataclass
class ProviderAction:
    """One discrete thing a provider did (or would do in dry-run)."""

    provider: str
    action: str              # e.g. "create_user", "add_to_channel", "suspend"
    target: str              # natural identifier, e.g. email or channel name
    status: str              # "applied", "skipped", "planned", "failed"
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.provider:<6}] {self.status:<8} {self.action:<20} {self.target} {self.detail}"


@dataclass
class Employee:
    first_name: str
    last_name: str
    email: str
    role: str
    manager_email: str = ""

    @property
    def login(self) -> str:
        return self.email

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Provider(ABC):
    """Abstract interface every downstream integration implements."""

    name: str = "provider"

    @abstractmethod
    def onboard(self, employee: Employee, *, dry_run: bool) -> List[ProviderAction]:
        ...

    @abstractmethod
    def offboard(self, email: str, *, dry_run: bool) -> List[ProviderAction]:
        ...

    def health_check(self) -> Dict[str, Any]:
        """Lightweight connectivity/credential probe. Override as needed."""
        return {"provider": self.name, "status": "ok"}
