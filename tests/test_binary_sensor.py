"""Binary sensor tests — online staleness window adapts to the poll interval."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import Location, Snapshot
from custom_components.mymedicalguardian.const import CONF_SCAN_INTERVAL, DOMAIN

ENTRY_DATA = {CONF_NAME: "Mom", CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"}


def _snapshot_with_location_age(minutes: int) -> Snapshot:
    loc = Location(
        event="Heart Beat",
        address="123 Main St",
        latitude=42.0,
        longitude=-84.0,
        observed_at=utcnow() - timedelta(minutes=minutes),
        observed_raw="x",
    )
    return Snapshot(locations=[loc])


async def _setup(hass, snapshot, options=None) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data=ENTRY_DATA,
        title="Mom",
        options=options or {},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=snapshot),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_online_stays_on_within_widened_window_on_long_interval(hass):
    # 120-min poll interval, location 100 min old: data is legitimately one
    # interval old, so the device must read "on", not flap to "disconnected".
    await _setup(
        hass,
        _snapshot_with_location_age(100),
        options={CONF_SCAN_INTERVAL: 120},
    )
    assert hass.states.get("binary_sensor.mom_online").state == "on"


async def test_online_off_when_clearly_stale(hass):
    # Default 10-min interval, location 200 min old: well beyond the window → off.
    await _setup(hass, _snapshot_with_location_age(200))
    assert hass.states.get("binary_sensor.mom_online").state == "off"
