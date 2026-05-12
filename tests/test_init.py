"""Setup / unload round-trip + entity creation."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import Snapshot
from custom_components.mymedicalguardian.const import DOMAIN

ENTRY_DATA = {
    CONF_NAME: "Mom",
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "secret",
}


async def test_setup_then_unload_round_trip(hass):
    """Entry sets up, all 4 platforms forward, then unload tears it down."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data=ENTRY_DATA,
        title="Mom",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # runtime_data is the coordinator
    assert entry.runtime_data is not None

    # Entities materialise — find them via the registry (entity_ids are
    # slugged from the device name "Mom", not the integration domain).
    registry = er.async_get(hass)
    ours = [
        e for e in registry.entities.values() if e.config_entry_id == entry.entry_id
    ]
    assert {e.domain for e in ours} == {
        "device_tracker",
        "sensor",
        "binary_sensor",
        "event",
    }
    assert sum(e.domain == "sensor" for e in ours) == 5
    assert sum(e.domain == "binary_sensor" for e in ours) == 2

    # Unload removes them
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_with_empty_snapshot_does_not_wedge(hass):
    """A brand-new device with no heartbeats yet should still complete setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data=ENTRY_DATA,
        title="Mom",
    )
    entry.add_to_hass(hass)

    empty = Snapshot()  # no locations, no battery, no alerts
    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=empty),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
