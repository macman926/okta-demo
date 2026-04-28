"""Unit tests for the lifecycle orchestrator.

These exercise the decision logic without hitting a real Okta tenant.
We use a hand-rolled FakeOktaClient because:
  * It's 50 lines.
  * It makes the assertions in tests read like English
    ("after offboarding, the user is SUSPENDED").
  * When the real OktaClient interface changes, tests fail at import
    time instead of silently passing against a stale mock.

Run:
    pip install -r requirements.txt pytest
    pytest tests/
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional

import pytest

from src.config import RoleMap
from src.lifecycle import Lifecycle
from src.okta_client import OktaGroup, OktaUser
from src.providers.base import Employee


class FakeOktaClient:
    """In-memory stand-in for OktaClient."""

    def __init__(self) -> None:
        self.users: Dict[str, OktaUser] = {}          # keyed by login
        self.groups: Dict[str, OktaGroup] = {}        # keyed by name
        self.memberships: Dict[str, set] = {}         # user_id -> {group_id}
        self._next_id = 0

    # test setup helpers
    def seed_groups(self, names: List[str]) -> None:
        for n in names:
            self._next_id += 1
            self.groups[n] = OktaGroup(id=f"grp{self._next_id}", name=n)

    def seed_user(self, email: str, status: str = "ACTIVE") -> OktaUser:
        self._next_id += 1
        u = OktaUser(id=f"usr{self._next_id}", status=status, email=email,
                     first_name="Seed", last_name="User", login=email)
        self.users[email] = u
        self.memberships[u.id] = set()
        return u

    # OktaClient interface
    def find_user_by_login(self, login: str) -> Optional[OktaUser]:
        return self.users.get(login)

    def create_user(self, *, email, first_name, last_name, login=None,
                    activate=True, group_ids=None) -> OktaUser:
        self._next_id += 1
        u = OktaUser(id=f"usr{self._next_id}", status="ACTIVE", email=email,
                     first_name=first_name, last_name=last_name,
                     login=login or email)
        self.users[email] = u
        self.memberships[u.id] = set(group_ids or [])
        return u

    def suspend_user(self, user_id: str) -> None:
        for u in self.users.values():
            if u.id == user_id:
                self.users[u.email] = OktaUser(
                    id=u.id, status="SUSPENDED", email=u.email,
                    first_name=u.first_name, last_name=u.last_name, login=u.login
                )
                return
        raise KeyError(user_id)

    def find_group_by_name(self, name: str) -> Optional[OktaGroup]:
        return self.groups.get(name)

    def resolve_group_names(self, names) -> Dict[str, str]:
        out = {}
        for n in names:
            if n not in self.groups:
                raise RuntimeError(f"unknown group {n}")
            out[n] = self.groups[n].id
        return out

    def add_user_to_group(self, user_id: str, group_id: str) -> None:
        self.memberships.setdefault(user_id, set()).add(group_id)

    def list_user_groups(self, user_id: str) -> List[OktaGroup]:
        ids = self.memberships.get(user_id, set())
        return [g for g in self.groups.values() if g.id in ids]

    def list_users(self, *, status_filter=None) -> Iterator[OktaUser]:
        for u in self.users.values():
            if status_filter is None or u.status == status_filter:
                yield u

    def list_group_members(self, group_id: str) -> Iterator[OktaUser]:
        for u in self.users.values():
            if group_id in self.memberships.get(u.id, set()):
                yield u


@pytest.fixture
def role_map() -> RoleMap:
    return RoleMap(
        okta_groups={"software-engineer": ["all-staff", "engineering"]},
        slack_channels={"software-engineer": []},
        google_ou={"software-engineer": "/Eng"},
    )


@pytest.fixture
def okta() -> FakeOktaClient:
    c = FakeOktaClient()
    c.seed_groups(["all-staff", "engineering", "ops"])
    return c


@pytest.fixture
def ada() -> Employee:
    return Employee(first_name="Ada", last_name="Lovelace",
                    email="ada@example.com", role="software-engineer")


def test_onboard_dry_run_does_not_mutate(okta, role_map, ada):
    lc = Lifecycle(okta, role_map, providers=[])
    result = lc.onboard(ada, dry_run=True)
    assert result.ok
    assert "ada@example.com" not in okta.users
    assert any(a.status == "planned" for a in result.actions)


def test_onboard_apply_creates_user_and_assigns_groups(okta, role_map, ada):
    lc = Lifecycle(okta, role_map, providers=[])
    result = lc.onboard(ada, dry_run=False)
    assert result.ok
    assert "ada@example.com" in okta.users
    created = okta.users["ada@example.com"]
    members = okta.memberships[created.id]
    assert {okta.groups["all-staff"].id, okta.groups["engineering"].id} <= members


def test_onboard_is_idempotent(okta, role_map, ada):
    lc = Lifecycle(okta, role_map, providers=[])
    lc.onboard(ada, dry_run=False)
    result = lc.onboard(ada, dry_run=False)
    assert result.ok
    # User already existed; no duplicate create, still ok.
    assert any(a.action == "create_user" and a.status == "skipped"
               for a in result.actions)


def test_unknown_role_fails_one_row_only(okta, role_map):
    lc = Lifecycle(okta, role_map, providers=[])
    bad = Employee(first_name="X", last_name="Y", email="x@y.com", role="nope")
    good = Employee(first_name="Ada", last_name="Lovelace",
                    email="ada@example.com", role="software-engineer")
    r_bad = lc.onboard(bad, dry_run=False)
    r_good = lc.onboard(good, dry_run=False)
    assert not r_bad.ok
    assert r_good.ok


def test_offboard_suspends_active_user(okta, role_map):
    u = okta.seed_user("alan@example.com", status="ACTIVE")
    lc = Lifecycle(okta, role_map, providers=[])
    result = lc.offboard("alan@example.com", dry_run=False)
    assert result.ok
    assert okta.users["alan@example.com"].status == "SUSPENDED"


def test_offboard_skips_already_suspended(okta, role_map):
    okta.seed_user("alan@example.com", status="SUSPENDED")
    lc = Lifecycle(okta, role_map, providers=[])
    result = lc.offboard("alan@example.com", dry_run=False)
    assert result.ok
    assert any(a.status == "skipped" for a in result.actions)


def test_offboard_unknown_user_is_noop(okta, role_map):
    lc = Lifecycle(okta, role_map, providers=[])
    result = lc.offboard("ghost@example.com", dry_run=False)
    assert result.ok
    assert any("not found" in a.detail for a in result.actions)
