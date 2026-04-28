"""Slack provider.

Real Slack integration using slack_sdk. If SLACK_BOT_TOKEN is not set,
the provider falls back to mock mode (logs what it *would* do) so the
rest of the demo still runs cleanly.

Scopes required on the bot token:
    users:read              - lookup members
    users:read.email        - resolve members by email
    channels:read           - list public channels (to resolve channel IDs)
    channels:manage         - invite/kick in public channels
    groups:write            - invite/kick in private channels (optional)
    chat:write              - optional; posting a welcome message

Rate limits: Slack returns 429 with a Retry-After header. slack_sdk's
builtin RateLimitErrorRetryHandler handles this for us. We layer our
own tenacity retry for ConnectionError / TimeoutError so failure modes
look the same as OktaClient.

What this provider does NOT do:
  * Workspace-level deactivation / deletion — those are Enterprise Grid
    (`admin.users.setInactive`). On a free Slack workspace you remove
    users from channels and surface a warning; actual deactivation
    happens manually by a workspace admin. This mirrors the real
    operational split at a lot of shops.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .base import Employee, Provider, ProviderAction, ProviderError

log = logging.getLogger(__name__)

try:  # slack_sdk is optional at import time so tests and mock mode don't need it
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    from slack_sdk.http_retry.builtin_handlers import (
        ConnectionErrorRetryHandler,
        RateLimitErrorRetryHandler,
    )
    _HAS_SDK = True
except ImportError:  # pragma: no cover - only hit when slack-sdk isn't installed
    WebClient = None  # type: ignore[assignment]
    SlackApiError = Exception  # type: ignore[assignment,misc]
    _HAS_SDK = False


class SlackProvider(Provider):
    name = "slack"

    def __init__(
        self,
        channels_for_role: Callable[[str], List[str]],
        token: Optional[str] = None,
    ) -> None:
        self._channels_for_role = channels_for_role
        self._token = (token or "").strip()
        self._mock = not self._token
        # channel name -> channel id, populated lazily.
        self._channel_cache: Dict[str, str] = {}

        if self._mock:
            log.info("slack provider running in MOCK mode (no SLACK_BOT_TOKEN)")
            self._client = None
            return

        if not _HAS_SDK:
            raise ProviderError(
                "SLACK_BOT_TOKEN is set but slack_sdk is not installed. "
                "Run `pip install -r requirements.txt` to pick up slack-sdk."
            )

        self._client = WebClient(
            token=self._token,
            retry_handlers=[
                RateLimitErrorRetryHandler(max_retry_count=3),
                ConnectionErrorRetryHandler(max_retry_count=3),
            ],
        )

    # ------------------------------------------------------------------
    # Provider interface
    # ------------------------------------------------------------------

    def onboard(self, employee: Employee, *, dry_run: bool) -> List[ProviderAction]:
        channels = self._channels_for_role(employee.role)
        if not channels:
            return []

        if self._mock:
            return [self._mock_invite(employee.email, ch, dry_run) for ch in channels]

        # Real mode. Look up user; if they're not in Slack yet (most
        # common on day-0 before SSO first-login), we surface a "planned"
        # action per channel so the plan is visible, and rely on the
        # reconcile path to complete the invites once the user signs in.
        user_id = self._lookup_user_id(employee.email)
        if user_id is None:
            return [
                ProviderAction(
                    self.name, "add_to_channel",
                    f"{employee.email}->#{ch}",
                    "planned",
                    detail="user not in Slack yet (will reconcile post-SSO)",
                )
                for ch in channels
            ]

        actions: List[ProviderAction] = []
        for ch in channels:
            actions.append(self._invite(user_id, employee.email, ch, dry_run))
        return actions

    def offboard(self, email: str, *, dry_run: bool) -> List[ProviderAction]:
        if self._mock:
            status = "planned" if dry_run else "applied"
            return [ProviderAction(
                self.name, "deactivate", email, status,
                detail="remove from all channels + disable (mock)",
            )]

        user_id = self._lookup_user_id(email)
        if user_id is None:
            return [ProviderAction(
                self.name, "deactivate", email, "skipped",
                detail="user not in Slack",
            )]

        channel_ids = self._list_user_channels(user_id)
        if dry_run:
            return [ProviderAction(
                self.name, "remove_from_channels", email, "planned",
                detail=f"{len(channel_ids)} channel(s) + mark inactive (requires Enterprise Grid)",
            )]

        actions: List[ProviderAction] = []
        for cid in channel_ids:
            self._kick(cid, user_id)
            actions.append(ProviderAction(
                self.name, "remove_from_channel", f"{email}->{cid}", "applied",
            ))
        # Full workspace deactivation is admin.users.setInactive (Grid only).
        # Surface that explicitly so the runbook doesn't pretend it happened.
        actions.append(ProviderAction(
            self.name, "deactivate_workspace", email, "skipped",
            detail="requires Enterprise Grid; manual step on free tier",
        ))
        return actions

    def health_check(self) -> Dict[str, str]:
        if self._mock:
            return {"provider": self.name, "status": "mock"}
        try:
            resp = self._client.auth_test()  # type: ignore[union-attr]
            return {
                "provider": self.name,
                "status": "ok",
                "team": resp.get("team", ""),
                "bot": resp.get("user", ""),
            }
        except SlackApiError as e:
            return {"provider": self.name, "status": f"error: {e.response['error']}"}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    def _lookup_user_id(self, email: str) -> Optional[str]:
        try:
            resp = self._client.users_lookupByEmail(email=email)  # type: ignore[union-attr]
            return resp["user"]["id"]
        except SlackApiError as e:
            if e.response["error"] == "users_not_found":
                return None
            raise ProviderError(f"slack users.lookupByEmail failed: {e.response['error']}") from e

    def _resolve_channel_id(self, channel_name: str) -> str:
        if channel_name in self._channel_cache:
            return self._channel_cache[channel_name]

        cursor: Optional[str] = None
        while True:
            try:
                resp = self._client.conversations_list(  # type: ignore[union-attr]
                    limit=200,
                    exclude_archived=True,
                    types="public_channel,private_channel",
                    cursor=cursor,
                )
            except SlackApiError as e:
                raise ProviderError(
                    f"slack conversations.list failed: {e.response['error']}"
                ) from e
            for ch in resp.get("channels", []):
                self._channel_cache[ch["name"]] = ch["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor or channel_name in self._channel_cache:
                break

        if channel_name not in self._channel_cache:
            raise ProviderError(
                f"slack channel #{channel_name} not found or bot is not a member"
            )
        return self._channel_cache[channel_name]

    def _invite(
        self, user_id: str, email: str, channel_name: str, dry_run: bool
    ) -> ProviderAction:
        target = f"{email}->#{channel_name}"
        if dry_run:
            return ProviderAction(self.name, "add_to_channel", target, "planned")

        channel_id = self._resolve_channel_id(channel_name)
        try:
            self._client.conversations_invite(  # type: ignore[union-attr]
                channel=channel_id, users=user_id
            )
        except SlackApiError as e:
            err = e.response["error"]
            # "already_in_channel" is the Slack way of saying "great, no-op".
            if err == "already_in_channel":
                return ProviderAction(self.name, "add_to_channel", target,
                                      "skipped", detail="already in channel")
            raise ProviderError(
                f"slack conversations.invite failed ({err}) for {target}"
            ) from e
        log.info("slack invite applied",
                 extra={"email": email, "channel": channel_name})
        return ProviderAction(self.name, "add_to_channel", target, "applied")

    def _list_user_channels(self, user_id: str) -> List[str]:
        ids: List[str] = []
        cursor: Optional[str] = None
        while True:
            try:
                resp = self._client.users_conversations(  # type: ignore[union-attr]
                    user=user_id,
                    limit=200,
                    exclude_archived=True,
                    types="public_channel,private_channel",
                    cursor=cursor,
                )
            except SlackApiError as e:
                raise ProviderError(
                    f"slack users.conversations failed: {e.response['error']}"
                ) from e
            ids.extend(ch["id"] for ch in resp.get("channels", []))
            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
        return ids

    def _kick(self, channel_id: str, user_id: str) -> None:
        try:
            self._client.conversations_kick(  # type: ignore[union-attr]
                channel=channel_id, user=user_id
            )
        except SlackApiError as e:
            err = e.response["error"]
            if err in ("not_in_channel", "cant_kick_self", "cant_kick_from_general"):
                # Non-fatal: log and keep going so one channel doesn't abort offboarding.
                log.warning("slack kick non-fatal",
                            extra={"channel": channel_id, "user": user_id, "err": err})
                return
            raise ProviderError(
                f"slack conversations.kick failed ({err}) for {user_id} in {channel_id}"
            ) from e

    # ------------------------------------------------------------------
    # helpers for tests and pretty output
    # ------------------------------------------------------------------

    def _mock_invite(self, email: str, channel: str, dry_run: bool) -> ProviderAction:
        status = "planned" if dry_run else "applied"
        log.info("slack invite (mock)",
                 extra={"email": email, "channel": channel, "dry_run": dry_run})
        return ProviderAction(self.name, "add_to_channel", f"{email}->#{channel}", status)
