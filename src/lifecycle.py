"""Lifecycle orchestrator.

This is the real 'enterprise engineering' bit. Given an employee record
(from an HRIS export in production, a CSV here) it fans out the onboard
or offboard flow across every registered Provider, collects the
resulting ProviderActions, and returns a plan/report.

Design goals:
  * Idempotent — safe to re-run a batch after a partial failure.
  * Dry-run first — every command supports --dry-run; nothing mutates
    state unless the caller explicitly asks.
  * Fail loudly on data errors (unknown role, unknown group) but keep
    going past individual employee failures so one bad row doesn't
    hold up the whole batch.
  * Structured output — all actions are logged as JSON and returned as
    dataclasses so they can be piped into a report, a Slack message,
    or a ticket.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import RoleMap
from .okta_client import OktaClient, OktaError, OktaUser
from .providers.base import Employee, Provider, ProviderAction

log = logging.getLogger(__name__)


@dataclass
class EmployeeResult:
    email: str
    ok: bool
    actions: List[ProviderAction] = field(default_factory=list)
    error: str = ""


@dataclass
class BatchReport:
    results: List[EmployeeResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def failed(self) -> List[EmployeeResult]:
        return [r for r in self.results if not r.ok]


class Lifecycle:
    """Coordinates Okta + downstream providers for one employee or a batch."""

    def __init__(
        self,
        okta: OktaClient,
        role_map: RoleMap,
        providers: Iterable[Provider] = (),
    ) -> None:
        self._okta = okta
        self._role_map = role_map
        self._providers = list(providers)

    # ------------------------------------------------------------------
    # onboarding
    # ------------------------------------------------------------------

    def onboard(self, emp: Employee, *, dry_run: bool = True) -> EmployeeResult:
        actions: List[ProviderAction] = []
        try:
            actions.extend(self._okta_onboard(emp, dry_run=dry_run))
            for p in self._providers:
                actions.extend(p.onboard(emp, dry_run=dry_run))
            return EmployeeResult(email=emp.email, ok=True, actions=actions)
        except Exception as e:  # catch-all on purpose: one row shouldn't kill the batch
            log.exception("onboard failed", extra={"email": emp.email})
            return EmployeeResult(email=emp.email, ok=False, actions=actions, error=str(e))

    def _okta_onboard(self, emp: Employee, *, dry_run: bool) -> List[ProviderAction]:
        actions: List[ProviderAction] = []
        group_names = self._role_map.okta_groups_for(emp.role)

        existing = self._okta.find_user_by_login(emp.email)
        if existing and existing.status not in ("DEPROVISIONED",):
            actions.append(ProviderAction("okta", "create_user", emp.email,
                                          "skipped", detail="user already exists"))
            # Still ensure group membership is correct.
            actions.extend(self._ensure_groups(existing, group_names, dry_run=dry_run))
            return actions

        if dry_run:
            actions.append(ProviderAction("okta", "create_user", emp.email,
                                          "planned",
                                          detail=f"role={emp.role} groups={group_names}"))
            return actions

        group_ids = list(self._okta.resolve_group_names(group_names).values())
        user = self._okta.create_user(
            email=emp.email,
            first_name=emp.first_name,
            last_name=emp.last_name,
            activate=True,
            group_ids=group_ids,
        )
        log.info("okta created user",
                 extra={"email": emp.email, "user_id": user.id,
                        "role": emp.role, "groups": group_names})
        actions.append(ProviderAction("okta", "create_user", emp.email,
                                      "applied", detail=f"id={user.id}"))
        for n in group_names:
            actions.append(ProviderAction("okta", "add_to_group",
                                          f"{emp.email}->{n}", "applied"))
        return actions

    def _ensure_groups(
        self, user: OktaUser, desired_names: List[str], *, dry_run: bool
    ) -> List[ProviderAction]:
        actions: List[ProviderAction] = []
        current = {g.name: g for g in self._okta.list_user_groups(user.id)}
        to_add = [n for n in desired_names if n not in current]
        if not to_add:
            return actions

        if dry_run:
            for n in to_add:
                actions.append(ProviderAction("okta", "add_to_group",
                                              f"{user.email}->{n}", "planned"))
            return actions

        resolved = self._okta.resolve_group_names(to_add)
        for name, gid in resolved.items():
            self._okta.add_user_to_group(user.id, gid)
            actions.append(ProviderAction("okta", "add_to_group",
                                          f"{user.email}->{name}", "applied"))
        return actions

    def onboard_csv(self, csv_path: str, *, dry_run: bool = True) -> BatchReport:
        report = BatchReport()
        for emp in _read_employees(csv_path):
            report.results.append(self.onboard(emp, dry_run=dry_run))
        return report

    # ------------------------------------------------------------------
    # offboarding
    # ------------------------------------------------------------------

    def offboard(self, email: str, *, dry_run: bool = True) -> EmployeeResult:
        actions: List[ProviderAction] = []
        try:
            actions.extend(self._okta_offboard(email, dry_run=dry_run))
            for p in self._providers:
                actions.extend(p.offboard(email, dry_run=dry_run))
            return EmployeeResult(email=email, ok=True, actions=actions)
        except Exception as e:
            log.exception("offboard failed", extra={"email": email})
            return EmployeeResult(email=email, ok=False, actions=actions, error=str(e))

    def _okta_offboard(self, email: str, *, dry_run: bool) -> List[ProviderAction]:
        user = self._okta.find_user_by_login(email)
        if user is None:
            return [ProviderAction("okta", "suspend_user", email,
                                   "skipped", detail="user not found")]
        if user.status == "SUSPENDED":
            return [ProviderAction("okta", "suspend_user", email,
                                   "skipped", detail="already suspended")]
        if dry_run:
            return [ProviderAction("okta", "suspend_user", email, "planned",
                                   detail=f"id={user.id} status={user.status}")]
        self._okta.suspend_user(user.id)
        log.info("okta suspended user",
                 extra={"email": email, "user_id": user.id})
        return [ProviderAction("okta", "suspend_user", email, "applied",
                               detail=f"id={user.id}")]

    def offboard_csv(self, csv_path: str, *, dry_run: bool = True) -> BatchReport:
        report = BatchReport()
        with open(csv_path, "r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                email = row["email"].strip()
                report.results.append(self.offboard(email, dry_run=dry_run))
        return report

    # ------------------------------------------------------------------
    # audits
    # ------------------------------------------------------------------

    def audit_by_group(self, group_name: str) -> List[OktaUser]:
        """List everyone currently in a (usually privileged) group."""
        group = self._okta.find_group_by_name(group_name)
        if group is None:
            raise OktaError(404, f"Group {group_name!r} not found")
        return list(self._okta.list_group_members(group.id))

    def audit_status(self, status: str = "PROVISIONED") -> List[OktaUser]:
        """Users stuck in a given Okta status — classic 'pending activation' hunt."""
        return list(self._okta.list_users(status_filter=status))


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _read_employees(csv_path: str) -> Iterable[Employee]:
    with open(csv_path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            yield Employee(
                first_name=row["first_name"].strip(),
                last_name=row["last_name"].strip(),
                email=row["email"].strip(),
                role=row["role"].strip(),
                manager_email=row.get("manager_email", "").strip(),
            )
