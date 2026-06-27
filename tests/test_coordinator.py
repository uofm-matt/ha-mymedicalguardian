"""Coordinator tests — failure tracking + repair-issue lifecycle."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import AuthError, FetchError, Snapshot
from custom_components.mymedicalguardian.const import DOMAIN
from custom_components.mymedicalguardian.coordinator import (
    ISSUE_PORTAL_UNREACHABLE,
    PORTAL_UNREACHABLE_AFTER,
    MGCoordinator,
)

ENTRY_DATA = {
    CONF_NAME: "Mom",
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "secret",
}


async def _make_coordinator(hass) -> MGCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="test@example.com", data=ENTRY_DATA
    )
    entry.add_to_hass(hass)
    return MGCoordinator(hass, entry)


class TestRepairIssueLifecycle:
    async def test_first_failure_does_not_create_issue(self, hass):
        coord = await _make_coordinator(hass)
        with patch(
            "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
            AsyncMock(side_effect=FetchError("transient")),
        ):
            await coord.async_refresh()

        registry = ir.async_get(hass)
        assert registry.async_get_issue(DOMAIN, ISSUE_PORTAL_UNREACHABLE) is None
        assert coord._first_failure_at is not None

    async def test_persistent_failure_creates_issue(self, hass):
        coord = await _make_coordinator(hass)
        long_ago = utcnow() - PORTAL_UNREACHABLE_AFTER - timedelta(minutes=1)
        coord._first_failure_at = long_ago
        with patch(
            "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
            AsyncMock(side_effect=FetchError("still down")),
        ):
            await coord.async_refresh()

        registry = ir.async_get(hass)
        issue = registry.async_get_issue(DOMAIN, ISSUE_PORTAL_UNREACHABLE)
        assert issue is not None
        assert issue.translation_placeholders["reason"] == "still down"

    async def test_success_clears_issue_and_resets_counter(self, hass):
        coord = await _make_coordinator(hass)
        # Pre-seed an issue + failure timestamp
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_PORTAL_UNREACHABLE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PORTAL_UNREACHABLE,
        )
        coord._first_failure_at = utcnow() - timedelta(hours=48)

        with patch(
            "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
            AsyncMock(return_value=Snapshot()),
        ):
            await coord.async_refresh()

        assert coord._first_failure_at is None
        registry = ir.async_get(hass)
        assert registry.async_get_issue(DOMAIN, ISSUE_PORTAL_UNREACHABLE) is None

    async def test_first_failure_persists_across_coordinator_recreation(self, hass):
        # ConfigEntryNotReady retries recreate the coordinator; the first-failure
        # time must survive so the 24h repair issue can eventually fire.
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id="test@example.com", data=ENTRY_DATA
        )
        entry.add_to_hass(hass)
        coord1 = MGCoordinator(hass, entry)
        with patch(
            "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
            AsyncMock(side_effect=FetchError("down")),
        ):
            await coord1.async_refresh()
        first = coord1._first_failure_at
        assert first is not None

        # A fresh coordinator for the same entry sees the persisted timestamp.
        coord2 = MGCoordinator(hass, entry)
        assert coord2._first_failure_at == first

    async def test_auth_error_also_clears_portal_issue(self, hass):
        """AuthError proves the portal IS reachable, so any pending
        portal-unreachable issue should clear."""
        coord = await _make_coordinator(hass)
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_PORTAL_UNREACHABLE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PORTAL_UNREACHABLE,
        )
        coord._first_failure_at = utcnow() - timedelta(hours=48)

        with patch(
            "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
            AsyncMock(side_effect=AuthError("password changed")),
        ):
            await coord.async_refresh()

        # Coordinator's last_update_success is False (AuthError flowed through)
        # but the portal-unreachable issue is gone.
        assert coord._first_failure_at is None
        registry = ir.async_get(hass)
        assert registry.async_get_issue(DOMAIN, ISSUE_PORTAL_UNREACHABLE) is None
