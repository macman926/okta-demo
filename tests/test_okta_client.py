"""Unit tests for OktaClient internals that don't need a real tenant."""

from __future__ import annotations

from src.okta_client import _parse_next_link


def test_parse_next_link_extracts_next_url():
    header = (
        '<https://dev.okta.com/api/v1/users?after=abc>; rel="next", '
        '<https://dev.okta.com/api/v1/users?after=aaa>; rel="self"'
    )
    assert _parse_next_link(header) == "https://dev.okta.com/api/v1/users?after=abc"


def test_parse_next_link_returns_none_when_absent():
    assert _parse_next_link("") is None
    assert _parse_next_link('<https://x>; rel="self"') is None
