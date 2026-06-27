"""Event entity tests — fires on new alert, suppresses unchanged/stale/restored."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import State
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.mymedicalguardian.api import Alert, Snapshot
from custom_components.mymedicalguardian.const import DOMAIN

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


def _recent(atype: str = "Fall", *, seconds_ago: int = 0) -> Alert:
    """An alert recent enough to fire on the initial snapshot."""
    return _alert(utcnow() - timedelta(seconds=seconds_ago), atype)


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="test@example.com", data=ENTRY_DATA, title="Mom"
    )
    entry.add_to_hass(hass)
    return entry


async def test_event_entity_fires_on_first_alert(hass):
    entry = _entry(hass)
    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot(latest_alert=_recent("Fall"))),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state is not None
    assert state.attributes["event_type"] == "Fall"
    assert state.attributes["address"] == "123 Main St"
    assert state.attributes["location"] == {"latitude": 42.0, "longitude": -84.0}


async def test_event_entity_does_not_refire_same_alert(hass):
    entry = _entry(hass)
    alert = _recent("On Cradle")

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot(latest_alert=alert)),
    ) as mocked:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        first_state = hass.states.get("event.mom_alert").state
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        second_state = hass.states.get("event.mom_alert").state

    # Same alert ⇒ entity state (the event timestamp) does not change.
    assert second_state == first_state
    assert mocked.call_count >= 2


async def test_event_entity_fires_again_on_new_alert(hass):
    entry = _entry(hass)
    first = Snapshot(latest_alert=_recent("Heart Beat", seconds_ago=60))
    second = Snapshot(latest_alert=_recent("Fall"))
    fake = AsyncMock(side_effect=[first, second])

    with patch("custom_components.mymedicalguardian.coordinator.fetch_snapshot", fake):
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


async def test_event_fires_for_distinct_same_minute_alert(hass):
    # Two alerts in the same wall-clock minute but different seconds (Fall→SOS)
    # must both fire — the old minute-resolution de-dup dropped the second.
    base = utcnow().replace(second=0, microsecond=0)
    entry = _entry(hass)
    first = Snapshot(latest_alert=_alert(base + timedelta(seconds=5), "Fall"))
    second = Snapshot(latest_alert=_alert(base + timedelta(seconds=50), "SOS"))
    fake = AsyncMock(side_effect=[first, second])

    with patch("custom_components.mymedicalguardian.coordinator.fetch_snapshot", fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        first_state = hass.states.get("event.mom_alert").state
        assert hass.states.get("event.mom_alert").attributes["event_type"] == "Fall"

        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    final = hass.states.get("event.mom_alert")
    assert final.state != first_state
    assert final.attributes["event_type"] == "SOS"


async def test_event_suppresses_stale_alert_on_fresh_install(hass):
    # Fresh install (no restored event) with a Fall from hours ago: HA must NOT
    # fire a phantom 'Fall detected' for an alert that predates startup.
    stale = _alert(utcnow() - timedelta(hours=3), "Fall")
    entry = _entry(hass)

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot(latest_alert=stale)),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    # Never triggered ⇒ no event timestamp and no event_type.
    assert state.state in (None, "unknown")
    assert state.attributes.get("event_type") is None


async def test_event_does_not_refire_known_alert_after_restart(hass):
    # HA restored the last-fired event from before shutdown; the same alert is
    # still latest. The entity must NOT re-fire it (no phantom Fall on restart).
    when = datetime(2026, 5, 12, 14, 44, 5, tzinfo=UTC)
    triggered = datetime(2026, 5, 12, 14, 45, 0, tzinfo=UTC)
    entry = _entry(hass)

    restored = State(
        "event.mom_alert",
        triggered.isoformat(),
        {"event_type": "Fall", "observed_at": when.isoformat()},
    )
    extra = {
        "last_event_type": "Fall",
        "last_event_attributes": {"observed_at": when.isoformat()},
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot(latest_alert=_alert(when, "Fall"))),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    # Restored trigger timestamp is preserved ⇒ the stale alert did not re-fire.
    assert state.state == triggered.isoformat(timespec="milliseconds")
    assert state.attributes["event_type"] == "Fall"


async def test_event_fires_genuinely_new_alert_after_restart(hass):
    # After a restart, an alert that differs from the restored one and is recent
    # still fires.
    old_when = datetime(2026, 5, 12, 14, 44, 5, tzinfo=UTC)
    triggered = datetime(2026, 5, 12, 14, 45, 0, tzinfo=UTC)
    new_alert = _recent("Fall")
    entry = _entry(hass)

    restored = State(
        "event.mom_alert",
        triggered.isoformat(),
        {"event_type": "Heart Beat", "observed_at": old_when.isoformat()},
    )
    extra = {
        "last_event_type": "Heart Beat",
        "last_event_attributes": {"observed_at": old_when.isoformat()},
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=Snapshot(latest_alert=new_alert)),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.state != triggered.isoformat(timespec="milliseconds")
    assert state.attributes["event_type"] == "Fall"
