"""Event entity tests — fires on new alert, suppresses on unchanged."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import Alert, Snapshot
from custom_components.mymedicalguardian.const import DOMAIN

ET = ZoneInfo("America/New_York")
ENTRY_DATA = {
    CONF_NAME: "Mom",
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "secret",
}


def _alert(when: datetime, atype: str = "Fall") -> Alert:
    return Alert(
        type=atype,
        observed_at=when,
        address="123 Main St",
        latitude=42.0,
        longitude=-84.0,
    )


async def test_event_entity_fires_on_first_alert(hass):
    when = datetime(2026, 5, 12, 10, 0, tzinfo=ET)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="test@example.com", data=ENTRY_DATA, title="Mom"
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot(latest_alert=_alert(when, "Fall"))),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state is not None
    assert state.attributes["event_type"] == "Fall"
    assert state.attributes["address"] == "123 Main St"
    assert state.attributes["location"] == {"latitude": 42.0, "longitude": -84.0}


async def test_event_entity_does_not_refire_same_alert(hass):
    when = datetime(2026, 5, 12, 10, 0, tzinfo=ET)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="test@example.com", data=ENTRY_DATA, title="Mom"
    )
    entry.add_to_hass(hass)
    alert = _alert(when, "On Cradle")

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot(latest_alert=alert)),
    ) as mocked:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        first_state = hass.states.get("event.mom_alert").state
        # Force a coordinator refresh; same alert as before.
        coord = entry.runtime_data
        await coord.async_refresh()
        await hass.async_block_till_done()
        second_state = hass.states.get("event.mom_alert").state

    # Same alert ⇒ entity state (the event timestamp) does not change.
    assert second_state == first_state
    assert mocked.call_count >= 2


async def test_event_entity_fires_again_on_new_alert(hass):
    first_when = datetime(2026, 5, 12, 10, 0, tzinfo=ET)
    second_when = first_when + timedelta(hours=1)

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="test@example.com", data=ENTRY_DATA, title="Mom"
    )
    entry.add_to_hass(hass)

    first = Snapshot(latest_alert=_alert(first_when, "Heart Beat"))
    second = Snapshot(latest_alert=_alert(second_when, "Fall"))
    fake = AsyncMock(side_effect=[first, second])

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        fake,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first_state = hass.states.get("event.mom_alert").state
        assert (
            hass.states.get("event.mom_alert").attributes["event_type"] == "Heart Beat"
        )

        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    final = hass.states.get("event.mom_alert")
    assert final.state != first_state  # new timestamp on the entity state
    assert final.attributes["event_type"] == "Fall"
