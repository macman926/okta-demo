"""Thin, well-behaved Okta Management API client.

Why roll our own instead of using `okta-sdk-python`?
  * The SDK is heavy, its typing is awkward, and it hides the exact
    requests/responses — which is the thing you actually want to reason
    about when you're debugging a production integration at 2am.
  * Building it ourselves is maybe 150 lines and forces us to think about
    the three things that matter for any SaaS client: auth, pagination,
    and rate limits.

References:
  * Management API: https://developer.okta.com/docs/reference/core-okta-api/
  * Rate limits:    https://developer.okta.com/docs/reference/rl-global-mgmt/
  * Users API:      https://developer.okta.com/docs/reference/api/users/
  * Groups API:     https://developer.okta.com/docs/reference/api/groups/
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional
from urllib.parse import urljoin

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

# Header link parsing: Link: <https://.../users?after=xxx>; rel="next"
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


class OktaError(RuntimeError):
    """Base exception. All Okta-originated failures bubble up as this."""

    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"[HTTP {status}] {message}")
        self.status = status
        self.body = body


class OktaRateLimited(OktaError):
    """429 from Okta. Caller may want to back off and retry."""


class OktaTransientError(OktaError):
    """5xx or network blip. Retry is safe."""


@dataclass
class OktaUser:
    id: str
    status: str
    email: str
    first_name: str
    last_name: str
    login: str

    @classmethod
    def from_api(cls, obj: Dict[str, Any]) -> "OktaUser":
        p = obj.get("profile", {}) or {}
        return cls(
            id=obj["id"],
            status=obj.get("status", "UNKNOWN"),
            email=p.get("email", ""),
            first_name=p.get("firstName", ""),
            last_name=p.get("lastName", ""),
            login=p.get("login", ""),
        )


@dataclass
class OktaGroup:
    id: str
    name: str

    @classmethod
    def from_api(cls, obj: Dict[str, Any]) -> "OktaGroup":
        return cls(id=obj["id"], name=obj.get("profile", {}).get("name", ""))


class OktaClient:
    """Minimal Okta Management API client.

    Design notes:
      * Uses a persistent Session so connections are pooled.
      * Handles Okta's cursor-style pagination via the Link header.
      * Retries on 429 (rate limit) and 5xx with exponential backoff.
      * Idempotent helpers: find_user_by_login returns None rather than
        404-ing so callers can express "create if not exists" naturally.
    """

    def __init__(
        self,
        org_url: str,
        api_token: str,
        *,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.org_url = org_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"SSWS {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "hrt-okta-demo/0.1 (+enterprise-eng)",
            }
        )

    # ------------------------------------------------------------------
    # low-level request
    # ------------------------------------------------------------------

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((OktaRateLimited, OktaTransientError,
                                       requests.ConnectionError,
                                       requests.Timeout)),
    )
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = path if path.startswith("http") else urljoin(self.org_url + "/", path.lstrip("/"))
        resp = self._session.request(
            method, url, params=params, json=json, timeout=self.timeout
        )
        if resp.status_code == 429:
            # Honor the reset hint when present; tenacity will also wait.
            reset = resp.headers.get("X-Rate-Limit-Reset")
            if reset and reset.isdigit():
                sleep_for = max(0, int(reset) - int(time.time()))
                log.warning(
                    "okta rate-limited; sleeping before retry",
                    extra={"sleep_seconds": sleep_for, "path": path},
                )
                time.sleep(min(sleep_for, 30))
            raise OktaRateLimited(429, "rate limited by Okta", _safe_body(resp))
        if 500 <= resp.status_code < 600:
            raise OktaTransientError(resp.status_code, "okta server error", _safe_body(resp))
        if not resp.ok:
            raise OktaError(resp.status_code, resp.reason, _safe_body(resp))
        return resp

    def _paginate(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Iterator[Dict[str, Any]]:
        """Walk all pages of a list endpoint using the Link: rel=next header."""
        next_url: Optional[str] = path
        next_params = params
        while next_url:
            resp = self._request("GET", next_url, params=next_params)
            for item in resp.json():
                yield item
            next_url = _parse_next_link(resp.headers.get("Link", ""))
            # After the first request we hit absolute URLs returned by
            # Okta, so params would double-up — clear them.
            next_params = None

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------

    def find_user_by_login(self, login: str) -> Optional[OktaUser]:
        try:
            resp = self._request("GET", f"/api/v1/users/{login}")
        except OktaError as e:
            if e.status == 404:
                return None
            raise
        return OktaUser.from_api(resp.json())

    def create_user(
        self,
        *,
        email: str,
        first_name: str,
        last_name: str,
        login: Optional[str] = None,
        activate: bool = True,
        group_ids: Optional[Iterable[str]] = None,
    ) -> OktaUser:
        """Create a new Okta user. Idempotent at the caller level via find_user_by_login."""
        body: Dict[str, Any] = {
            "profile": {
                "firstName": first_name,
                "lastName": last_name,
                "email": email,
                "login": login or email,
            }
        }
        if group_ids:
            body["groupIds"] = list(group_ids)
        resp = self._request(
            "POST",
            "/api/v1/users",
            params={"activate": str(activate).lower()},
            json=body,
        )
        return OktaUser.from_api(resp.json())

    def suspend_user(self, user_id: str) -> None:
        """Step 1 of offboarding: freeze the account, don't delete it yet.

        Suspend is reversible; deactivate/delete is not. Keeping a gap
        between "they can't log in anymore" and "their data is gone"
        matters for offboarding mistakes and for legal hold.
        """
        self._request("POST", f"/api/v1/users/{user_id}/lifecycle/suspend")

    def deactivate_user(self, user_id: str, *, send_email: bool = False) -> None:
        self._request(
            "POST",
            f"/api/v1/users/{user_id}/lifecycle/deactivate",
            params={"sendEmail": str(send_email).lower()},
        )

    def list_users(self, *, status_filter: Optional[str] = None) -> Iterator[OktaUser]:
        params: Dict[str, Any] = {"limit": 200}
        if status_filter:
            params["filter"] = f'status eq "{status_filter}"'
        for obj in self._paginate("/api/v1/users", params=params):
            yield OktaUser.from_api(obj)

    # ------------------------------------------------------------------
    # groups
    # ------------------------------------------------------------------

    def find_group_by_name(self, name: str) -> Optional[OktaGroup]:
        """Exact-match lookup via the `q=` search parameter."""
        for obj in self._paginate("/api/v1/groups", params={"q": name, "limit": 50}):
            g = OktaGroup.from_api(obj)
            if g.name == name:
                return g
        return None

    def resolve_group_names(self, names: Iterable[str]) -> Dict[str, str]:
        """Map group name -> group id, failing loudly on unknown names."""
        out: Dict[str, str] = {}
        missing: List[str] = []
        for n in names:
            g = self.find_group_by_name(n)
            if g is None:
                missing.append(n)
            else:
                out[n] = g.id
        if missing:
            raise OktaError(
                400,
                f"Unknown Okta group(s): {', '.join(missing)}. "
                "Create them in Okta Admin or correct role_mappings.yaml.",
            )
        return out

    def add_user_to_group(self, user_id: str, group_id: str) -> None:
        self._request("PUT", f"/api/v1/groups/{group_id}/users/{user_id}")

    def remove_user_from_group(self, user_id: str, group_id: str) -> None:
        self._request("DELETE", f"/api/v1/groups/{group_id}/users/{user_id}")

    def list_user_groups(self, user_id: str) -> List[OktaGroup]:
        # /users/{id}/groups returns full list in one page for normal-sized users
        resp = self._request("GET", f"/api/v1/users/{user_id}/groups")
        return [OktaGroup.from_api(o) for o in resp.json()]

    def list_group_members(self, group_id: str) -> Iterator[OktaUser]:
        for obj in self._paginate(f"/api/v1/groups/{group_id}/users", params={"limit": 200}):
            yield OktaUser.from_api(obj)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _parse_next_link(header: str) -> Optional[str]:
    """Extract the rel=next URL from an Okta Link header, if any."""
    for match in _LINK_RE.finditer(header or ""):
        url, rel = match.group(1), match.group(2)
        if rel == "next":
            return url
    return None


def _safe_body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
