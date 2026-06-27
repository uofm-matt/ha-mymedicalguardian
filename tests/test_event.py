"""Event entity tests — high-water-mark firing over the recent-alerts list.

The entity fires an alert only when its observed_at is newer than the baseline
(the last-fired mark, or the time monitoring began before anything has fired),
preferring the newest Fall/SOS so a critical is never masked by a routine event
nor a routine event lost behind an in-window critical. It never replays a
pre-install alert, never re-fires one, and survives restart and a v1.0.0 -> 1.0.1
upgrade without phantom events.
"""

from datetime import UTC, datetime, timedelta, timezone
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
from custom_components.mymedicalguardian.event import ALERT_TYPES, OTHER_ALERT

FETCH = "custom_components.mymedicalguardian.coordinator.fetch_snapshot"

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


def _snap(*alerts: Alert) -> Snapshot:
    return Snapshot(alerts=tuple(alerts))


def _after_setup(atype: str = "Fall", *, minutes: int = 5) -> Alert:
    """An alert timestamped after the entity begins monitoring (so it fires).
    Real alerts are in the past; a forward offset simulates 'occurred after setup'
    deterministically without time-mocking the event loop."""
    return _alert(utcnow() + timedelta(minutes=minutes), atype)


def _before_setup(atype: str = "Fall", *, minutes: int = 5) -> Alert:
    return _alert(utcnow() - timedelta(minutes=minutes), atype)


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="test@example.com", data=ENTRY_DATA, title="Mom"
    )
    entry.add_to_hass(hass)
    return entry


def _event_type(hass):
    return hass.states.get("event.mom_alert").attributes.get("event_type")


def test_critical_alerts_are_known_types():
    from custom_components.mymedicalguardian.api import _CRITICAL_ALERTS

    assert set(ALERT_TYPES) >= _CRITICAL_ALERTS


async def test_no_fire_for_alert_present_at_setup(hass):
    # An alert already present when the entity is added predates monitoring and
    # must not fire (no replaying history on install/restart).
    entry = _entry(hass)
    with patch(FETCH, AsyncMock(return_value=_snap(_before_setup("Fall")))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.state in (None, "unknown")
    assert state.attributes.get("event_type") is None


async def test_fires_for_new_alert_after_setup(hass):
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(_after_setup("Fall"))])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _event_type(hass) is None

        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.attributes["event_type"] == "Fall"
    assert state.attributes["address"] == "123 Main St"
    assert state.attributes["location"] == {"latitude": 42.0, "longitude": -84.0}


async def test_does_not_refire_same_alert(hass):
    entry = _entry(hass)
    alert = _after_setup("Fall")
    fake = AsyncMock(side_effect=[Snapshot(), _snap(alert), _snap(alert)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        fired_state = hass.states.get("event.mom_alert").state
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get("event.mom_alert").state == fired_state


async def test_fires_distinct_same_minute_alerts(hass):
    # Two alerts in the same wall-clock minute but different seconds both fire.
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base.replace(second=5), "Fall")
    sos = _alert(base.replace(second=50), "SOS")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(fall), _snap(sos, fall)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "Fall"
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    # event_type only advances via _trigger_event, so SOS proves the same-minute
    # second alert fired rather than collapsing into the Fall.
    assert _event_type(hass) == "SOS"


async def test_critical_fires_before_newer_routine(hass):
    # A Fall and a NEWER Heart Beat both fresh in one poll: the Fall fires first
    # (critical preference), and the routine Heart Beat fires on the next poll —
    # neither is masked.
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base.replace(second=5), "Fall")
    beat = _alert(base.replace(second=40), "Heart Beat")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(beat, fall), _snap(beat, fall)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "Fall"  # critical first, not the newer beat
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert _event_type(hass) == "Heart Beat"  # routine fires next, not masked


async def test_two_criticals_same_window_both_fire(hass):
    # A Fall and a slightly-newer SOS first surface together: the OLDER critical
    # fires first (mark advances minimally), the newer fires next poll. Neither
    # emergency is swallowed.
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base.replace(second=30), "Fall")
    sos = _alert(base.replace(second=50), "SOS")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(sos, fall), _snap(sos, fall)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "Fall"  # oldest critical first
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert _event_type(hass) == "SOS"  # newer critical not lost behind the Fall


async def test_no_phantom_for_stale_alert_on_later_poll(hass):
    # Fresh install, empty first poll, then a stale (pre-install) Fall surfaces on
    # a later poll: it must NOT fire even though nothing has fired yet.
    entry = _entry(hass)
    fake = AsyncMock(
        side_effect=[Snapshot(), _snap(_before_setup("Fall", minutes=180))]
    )
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.state in (None, "unknown")
    assert state.attributes.get("event_type") is None


async def test_no_refire_on_oscillation(hass):
    # latest list goes [Fall] -> [Heart Beat, Fall] -> [Fall]: the older Fall
    # re-appearing must NOT re-fire (monotonic high-water mark).
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base.replace(second=5), "Fall")
    beat = _alert(base.replace(second=30), "Heart Beat")
    entry = _entry(hass)
    fake = AsyncMock(
        side_effect=[Snapshot(), _snap(fall), _snap(beat, fall), _snap(fall)]
    )
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for _ in range(3):
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()

    # Last fired event is the Heart Beat (newest), not a re-fired Fall.
    assert _event_type(hass) == "Heart Beat"


async def test_unknown_type_fires_as_other(hass):
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(_after_setup("Earthquake"))])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.attributes["event_type"] == OTHER_ALERT
    assert state.attributes["alert_type"] == "Earthquake"


async def test_no_refire_known_alert_after_restart(hass):
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
        "last_event_attributes": {
            "observed_at": when.isoformat(),
            "alert_type": "Fall",
        },
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    with patch(FETCH, AsyncMock(return_value=_snap(_alert(when, "Fall")))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.state == triggered.isoformat(timespec="milliseconds")
    assert state.attributes["event_type"] == "Fall"


async def test_fires_downtime_alert_after_restart(hass):
    restored_at = datetime(2026, 5, 12, 14, 40, 0, tzinfo=UTC)
    downtime = datetime(2026, 5, 12, 14, 50, 0, tzinfo=UTC)
    triggered = datetime(2026, 5, 12, 14, 41, 0, tzinfo=UTC)
    entry = _entry(hass)
    restored = State(
        "event.mom_alert",
        triggered.isoformat(),
        {"event_type": "Heart Beat", "observed_at": restored_at.isoformat()},
    )
    extra = {
        "last_event_type": "Heart Beat",
        "last_event_attributes": {
            "observed_at": restored_at.isoformat(),
            "alert_type": "Heart Beat",
        },
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    with patch(FETCH, AsyncMock(return_value=_snap(_alert(downtime, "Fall")))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.state != triggered.isoformat(timespec="milliseconds")
    assert state.attributes["event_type"] == "Fall"


async def test_no_refire_on_upgrade_minute_resolution(hass):
    # v1.0.0 stored a minute-resolution, offset-local marker. The same alert at
    # finer (second) resolution must not re-fire on the upgrade restart.
    restored_local = datetime(
        2026, 5, 12, 10, 44, 0, tzinfo=timezone(timedelta(hours=-4))
    )
    triggered = datetime(2026, 5, 12, 14, 45, 0, tzinfo=UTC)
    live = datetime(2026, 5, 12, 14, 44, 5, tzinfo=UTC)  # same instant-minute, +5s
    entry = _entry(hass)
    restored = State(
        "event.mom_alert",
        triggered.isoformat(),
        {"event_type": "Fall", "observed_at": restored_local.isoformat()},
    )
    extra = {
        "last_event_type": "Fall",
        "last_event_attributes": {"observed_at": restored_local.isoformat()},
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    with patch(FETCH, AsyncMock(return_value=_snap(_alert(live, "Fall")))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.state == triggered.isoformat(timespec="milliseconds")  # no re-fire


async def test_upgrade_no_refire_fall_with_newer_routine_present(hass):
    # v1.0.0 minute-res marker for an already-fired Fall, plus a NEWER routine
    # Heart Beat in the same window. Adoption must scan the whole list (not just
    # the newest Heart Beat) so the Fall isn't replayed; the new routine may fire.
    restored_local = datetime(
        2026, 5, 12, 10, 44, 0, tzinfo=timezone(timedelta(hours=-4))
    )  # 14:44:00 UTC, minute resolution
    triggered = datetime(2026, 5, 12, 14, 45, 0, tzinfo=UTC)
    fall = _alert(datetime(2026, 5, 12, 14, 44, 5, tzinfo=UTC), "Fall")  # finer res
    beat = _alert(datetime(2026, 5, 12, 14, 50, 0, tzinfo=UTC), "Heart Beat")
    entry = _entry(hass)
    restored = State(
        "event.mom_alert",
        triggered.isoformat(),
        {"event_type": "Fall", "observed_at": restored_local.isoformat()},
    )
    extra = {
        "last_event_type": "Fall",
        "last_event_attributes": {"observed_at": restored_local.isoformat()},
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    with patch(FETCH, AsyncMock(return_value=_snap(beat, fall))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The Fall is adopted (not replayed); the genuinely-new Heart Beat fires.
    assert _event_type(hass) == "Heart Beat"


async def test_new_same_minute_alert_fires_after_v101_marker(hass):
    # A v1.0.1 marker (carries 'alert_type') must not trigger minute-adoption even
    # when its seconds land on :00 — which the portal routinely produces — so a
    # genuinely-new same-minute alert from downtime still fires (not swallowed).
    restored_at = datetime(2026, 5, 12, 14, 44, 0, tzinfo=UTC)  # :00 seconds!
    triggered = datetime(2026, 5, 12, 14, 45, 0, tzinfo=UTC)
    sos = _alert(datetime(2026, 5, 12, 14, 44, 50, tzinfo=UTC), "SOS")  # same minute
    entry = _entry(hass)
    restored = State(
        "event.mom_alert",
        triggered.isoformat(),
        {"event_type": "Fall", "observed_at": restored_at.isoformat()},
    )
    extra = {
        "last_event_type": "Fall",
        "last_event_attributes": {
            "observed_at": restored_at.isoformat(),
            "alert_type": "Fall",  # marks this as a v1.0.1 (second-resolution) mark
        },
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    with patch(FETCH, AsyncMock(return_value=_snap(sos))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _event_type(hass) == "SOS"


async def test_goes_unavailable_when_coordinator_fails(hass):
    from custom_components.mymedicalguardian.api import FetchError

    entry = _entry(hass)
    fake = AsyncMock(
        side_effect=[_snap(_after_setup("Fall")), FetchError("portal down")]
    )
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get("event.mom_alert").state != "unavailable"

        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get("event.mom_alert").state == "unavailable"
