"""Google Workspace provider (MOCK).

Real version would call the Admin SDK Directory API to move the user
into the correct OU and apply group memberships. Here we log the intent
so the architecture is visible without requiring GWS credentials.
"""

from __future__ import annotations

import logging
from typing import List

from .base import Employee, Provider, ProviderAction

log = logging.getLogger(__name__)


class GoogleWorkspaceProvider(Provider):
    name = "gws"

    def __init__(self, ou_for_role):
        self._ou_for_role = ou_for_role

    def onboard(self, employee: Employee, *, dry_run: bool) -> List[ProviderAction]:
        ou = self._ou_for_role(employee.role)
        status = "planned" if dry_run else "applied"
        log.info("gws place in OU",
                 extra={"email": employee.email, "ou": ou, "dry_run": dry_run})
        return [ProviderAction(self.name, "set_ou", employee.email, status, detail=ou)]

    def offboard(self, email: str, *, dry_run: bool) -> List[ProviderAction]:
        status = "planned" if dry_run else "applied"
        log.info("gws suspend", extra={"email": email, "dry_run": dry_run})
        return [ProviderAction(self.name, "suspend_account", email, status)]
