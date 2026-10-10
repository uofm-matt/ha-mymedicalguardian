"""Event entity tests — fired-set firing over the recent-alerts list.

The entity fires an alert only when it is newer than the baseline (the restored
mark, or the time monitoring began) and not already fired, firing every fresh
Fall/SOS oldest-first so a critical is never masked by a routine event nor lost
because a newer routine one fired earlier. It never replays a pre-install alert,
never re-fires one, and survives restart and a v1.0.0 -> 1.0.1 upgrade without
phantom events.
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import State
from homeassistant.setup import async_setup_component
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.mymedicalguardian.api import Alert, Snapshot
from custom_components.mymedicalguardian.const import DOMAIN
from custom_components.mymedicalguardian.event import (
    ALERT_TYPES,
    OTHER_ALERT,
    MGAlertEvent,
)

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
    # A Fall and a slightly-newer SOS first surface together: both fire in the
    # same poll, oldest first. Neither emergency waits a scan interval.
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base.replace(second=30), "Fall")
    sos = _alert(base.replace(second=50), "SOS")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(sos, fall), _snap(sos, fall)])
    fired: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda ev: (
            fired.append(ev.data["new_state"].attributes.get("event_type"))
            if ev.data["entity_id"] == "event.mom_alert" and ev.data["new_state"]
            else None
        ),
    )
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "SOS"
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert [t for t in fired if t][:2] == ["Fall", "SOS"]  # oldest first
    assert _event_type(hass) == "SOS"  # second poll fires nothing new


async def test_sos_fires_before_newer_routine(hass):
    # An SOS and a NEWER Heart Beat first surface together: the SOS fires first.
    # Dropping SOS from the critical set makes the Heart Beat fire instead.
    base = utcnow() + timedelta(minutes=5)
    sos = _alert(base.replace(second=5), "SOS")
    beat = _alert(base.replace(second=40), "Heart Beat")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(beat, sos), _snap(beat, sos)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "SOS"
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert _event_type(hass) == "Heart Beat"


async def test_late_fall_after_fired_routine_fires(hass):
    # Poll 1 fires a Heart Beat at T+10. Poll 2 surfaces a Fall stamped T+5 that
    # the portal posted late. The Fall must still fire.
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base, "Fall")
    beat = _alert(base + timedelta(minutes=5), "Heart Beat")
    entry = _entry(hass)
    fake = AsyncMock(
        side_effect=[Snapshot(), _snap(beat), _snap(beat, fall), _snap(beat, fall)]
    )
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "Heart Beat"
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "Fall"
        state = hass.states.get("event.mom_alert").state
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get("event.mom_alert").state == state  # not re-fired


async def test_future_stamped_routine_does_not_block_later_real_alert(hass):
    # A Heart Beat stamped two hours ahead (portal clock skew) must not hide a
    # Fall stamped a few minutes ahead on a later poll.
    skewed = _alert(utcnow() + timedelta(hours=2), "Heart Beat")
    fall = _alert(utcnow() + timedelta(minutes=10), "Fall")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(skewed), _snap(skewed, fall)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert _event_type(hass) == "Heart Beat"
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    assert _event_type(hass) == "Fall"


async def test_restart_does_not_replay_routine_newer_than_late_fall(hass):
    # Last event fired was a late Fall (T+5) after a Heart Beat (T+10). The
    # persisted fired_through mark keeps the restart from replaying the beat.
    fall_at = datetime(2026, 5, 12, 14, 45, 0, tzinfo=UTC)
    beat_at = fall_at + timedelta(minutes=5)
    entry = _entry(hass)
    restored = State(
        "event.mom_alert",
        beat_at.isoformat(),
        {"event_type": "Fall", "observed_at": fall_at.isoformat()},
    )
    extra = {
        "last_event_type": "Fall",
        "last_event_attributes": {
            "observed_at": fall_at.isoformat(),
            "alert_type": "Fall",
            "fired_through": beat_at.isoformat(),
        },
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])

    snap = _snap(_alert(beat_at, "Heart Beat"), _alert(fall_at, "Fall"))
    with patch(FETCH, AsyncMock(return_value=snap)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.attributes["event_type"] == "Fall"
    assert state.state == beat_at.isoformat(timespec="milliseconds")


async def test_unrecognized_critical_label_fires_as_fall(hass):
    # Only Heart Beat / cradle labels are verified against the real portal, so a
    # Fall labelled "Fall Detected" must still fire at critical priority.
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base.replace(second=5), "fall detected")
    beat = _alert(base.replace(second=40), "Heart Beat")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(beat, fall)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    state = hass.states.get("event.mom_alert")
    assert state.attributes["event_type"] == "Fall"
    assert state.attributes["alert_type"] == "fall detected"


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


async def test_fired_set_is_bounded_and_keeps_newest(hass):
    # The fired set must not grow without bound over months of uptime; eviction
    # drops the oldest keys so recent alerts still cannot re-fire.
    base = utcnow() + timedelta(minutes=5)
    alerts = [_alert(base + timedelta(seconds=i), "Fall") for i in range(5)]
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(*reversed(alerts))])
    with (
        patch(FETCH, fake),
        patch("custom_components.mymedicalguardian.event._FIRED_CAP", 3),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    entity = hass.data["event"].get_entity("event.mom_alert")
    assert sorted(t for _, t in entity._fired) == [a.observed_at for a in alerts[2:]]


async def test_persisted_mark_never_exceeds_now(hass):
    # A +2h skewed routine must not persist a mark in the future: after a restart
    # that would swallow every real alert stamped before it.
    skewed = _alert(utcnow() + timedelta(hours=2), "Heart Beat")
    entry = _entry(hass)
    fake = AsyncMock(side_effect=[Snapshot(), _snap(skewed)])
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    mark = datetime.fromisoformat(
        hass.states.get("event.mom_alert").attributes["fired_through"]
    )
    assert mark <= utcnow()


def _restored(hass, **attrs):
    last = {"alert_type": "Heart Beat", **attrs}
    state = State("event.mom_alert", "2026-05-12T14:00:00.000+00:00", {})
    mock_restore_cache_with_extra_data(
        hass,
        [(state, {"last_event_type": "Heart Beat", "last_event_attributes": last})],
    )


async def _setup(hass, entry, snapshots):
    fake = AsyncMock(side_effect=snapshots)
    with patch(FETCH, fake):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for _ in snapshots[1:]:
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()


async def test_late_fall_after_restart_fires_and_fired_alerts_do_not_replay(hass):
    # Heart Beat T+10 fired, HA restarted, then a Fall stamped T+5 posts late.
    # The persisted fired list lets the Fall through without replaying the beat.
    t = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    beat = _alert(t + timedelta(minutes=10), "Heart Beat")
    fall = _alert(t + timedelta(minutes=5), "Fall")
    _restored(
        hass,
        observed_at=beat.observed_at.isoformat(),
        fired_through=beat.observed_at.isoformat(),
        fired_floor=t.isoformat(),
        fired=[["Heart Beat", beat.observed_at.isoformat()]],
    )
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(beat), _snap(beat, fall)])

    assert _event_type(hass) == "Fall"


async def test_restart_with_fired_list_does_not_replay_fired_critical(hass):
    t = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    fall = _alert(t, "Fall")
    beat = _alert(t + timedelta(minutes=30), "Heart Beat")
    _restored(
        hass,
        observed_at=beat.observed_at.isoformat(),
        fired_through=beat.observed_at.isoformat(),
        fired=[
            ["Fall", fall.observed_at.isoformat()],
            ["Heart Beat", beat.observed_at.isoformat()],
        ],
    )
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(beat, fall)])

    assert hass.states.get("event.mom_alert").attributes.get("alert_type") == (
        "Heart Beat"
    )


async def test_restored_future_observed_at_does_not_block_real_alert(hass):
    # The last fired routine was stamped two hours ahead; the clamped
    # fired_through is what must set the restart baseline, not observed_at.
    now = utcnow()
    skewed = now + timedelta(hours=2)
    _restored(
        hass,
        observed_at=skewed.isoformat(),
        fired_through=now.isoformat(),
        fired=[["Heart Beat", skewed.isoformat()]],
    )
    fall = _alert(now + timedelta(minutes=10), "Fall")
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(_alert(skewed, "Heart Beat"), fall)])

    assert _event_type(hass) == "Fall"


async def test_duplicate_critical_rows_fire_once(hass):
    when = utcnow() + timedelta(minutes=5)
    dup = [_alert(when, "Fall"), _alert(when, "Fall")]
    entry = _entry(hass)
    seen: list[str] = []
    hass.bus.async_listen(
        "state_changed",
        lambda ev: (
            seen.append(ev.data["new_state"].attributes.get("event_type"))
            if ev.data["entity_id"] == "event.mom_alert" and ev.data["new_state"]
            else None
        ),
    )
    await _setup(hass, entry, [Snapshot(), _snap(*dup)])

    assert seen.count("Fall") == 1


async def test_malformed_fired_list_entries_are_ignored(hass, fires):
    t = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    beat = _alert(t, "Heart Beat")
    _restored(
        hass,
        observed_at=t.isoformat(),
        fired_through=t.isoformat(),
        fired=[["only-one"], 5, [1, "x"], ["Heart Beat", "not-a-date"]],
    )
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(beat)])

    # Nothing usable was restored, so the in-window beat is simply unfired: it
    # fires once rather than the malformed entries raising or being trusted.
    assert fires == ["Heart Beat"]


async def _reload(hass, entry, snapshots):
    """Restart the entity the way HA does: unload stores its state and restore
    payload, reload restores from them. Then run one refresh per extra snapshot."""
    with patch(FETCH, AsyncMock(side_effect=snapshots)):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        for _ in snapshots[1:]:
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()


@pytest.fixture
def fires():
    """The alert_type of every event the entity triggers, in order. Spies on
    _trigger_event because a fire during setup or reload is folded into the
    entity's first state write and never shows as its own state change."""
    seen: list[str] = []
    original = MGAlertEvent._trigger_event

    def spy(self, event_type, event_attributes=None):
        seen.append(event_attributes["alert_type"])
        return original(self, event_type, event_attributes)

    with patch.object(MGAlertEvent, "_trigger_event", spy):
        yield seen


async def _poll(hass, entry, snapshot):
    with patch(FETCH, AsyncMock(return_value=snapshot)):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()


async def test_first_restart_does_not_fire_preinstall_fall(hass, freezer, fires):
    # A Fall from before the install sits in the portal window and must never
    # fire: not at setup, and not after the first restart looks back 3 hours.
    start = utcnow()
    old_fall = _alert(start - timedelta(hours=2), "Fall")
    beat = _alert(start + timedelta(minutes=5), "Heart Beat")
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(old_fall)])
    freezer.move_to(start + timedelta(minutes=10))
    await _poll(hass, entry, _snap(beat, old_fall))
    assert fires == ["Heart Beat"]

    freezer.move_to(start + timedelta(minutes=20))
    await _reload(hass, entry, [_snap(beat, old_fall), _snap(beat, old_fall)])

    assert fires == ["Heart Beat"]


async def test_second_restart_after_upgrade_does_not_replay_fired_fall(
    hass, freezer, fires
):
    # A Fall fired under 1.0.1 (second-resolution mark, no fired list). After the
    # upgrade restart one Heart Beat fires, then HA restarts again.
    start = utcnow()
    fall = _alert(start - timedelta(hours=1), "Fall")
    beat = _alert(start + timedelta(minutes=5), "Heart Beat")
    stamp = fall.observed_at.isoformat()
    restored = State("event.mom_alert", stamp, {"event_type": "Fall"})
    extra = {
        "last_event_type": "Fall",
        "last_event_attributes": {"observed_at": stamp, "alert_type": "Fall"},
    }
    mock_restore_cache_with_extra_data(hass, [(restored, extra)])
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(fall)])
    freezer.move_to(start + timedelta(minutes=10))
    await _poll(hass, entry, _snap(beat, fall))
    assert fires == ["Heart Beat"]

    freezer.move_to(start + timedelta(minutes=20))
    await _reload(hass, entry, [_snap(beat, fall), _snap(beat, fall)])

    assert fires == ["Heart Beat"]


async def test_late_fall_still_fires_after_a_real_restart(hass, freezer, fires):
    # Round trip through HA's own restore store: Heart Beat fires, HA restarts,
    # then the portal posts a Fall stamped before the Heart Beat.
    start = utcnow()
    fall = _alert(start + timedelta(minutes=2), "Fall")
    beat = _alert(start + timedelta(minutes=5), "Heart Beat")
    entry = _entry(hass)
    await _setup(hass, entry, [Snapshot()])
    freezer.move_to(start + timedelta(minutes=10))
    await _poll(hass, entry, _snap(beat))

    freezer.move_to(start + timedelta(minutes=20))
    await _reload(hass, entry, [_snap(beat), _snap(beat, fall)])

    assert fires == ["Heart Beat", "Fall"]
    # The stored mark is the newest stamp fired, not the last event's: the late
    # Fall must not pull it back behind the Heart Beat.
    attrs = hass.states.get("event.mom_alert").attributes
    assert datetime.fromisoformat(attrs["fired_through"]) == beat.observed_at


async def test_late_fall_fires_after_restart_behind_many_newer_alerts(
    hass, freezer, fires
):
    # Twenty alerts inside the lookback window, then a restart, then a Fall
    # stamped before all of them. The stored list covers the whole window, so
    # none of the twenty repeats and the late Fall is not taken as handled.
    start = utcnow()
    soses = [_alert(start + timedelta(minutes=m), "SOS") for m in range(1, 21)]
    fall = _alert(start + timedelta(seconds=30), "Fall")
    entry = _entry(hass)
    await _setup(hass, entry, [Snapshot()])
    freezer.move_to(start + timedelta(minutes=30))
    await _poll(hass, entry, _snap(*soses))
    assert fires == ["SOS"] * 20

    freezer.move_to(start + timedelta(minutes=40))
    await _reload(hass, entry, [_snap(*soses), _snap(*soses, fall)])

    assert fires == ["SOS"] * 20 + ["Fall"]


async def test_alerts_past_the_storage_bound_repeat_rather_than_hide_a_fall(
    hass, freezer, fires
):
    # More alerts in the window than the stored list may hold. The ones that
    # did not fit fire again after a restart; the alternative, raising the
    # baseline past them, would also hide a Fall posted late from before them.
    start = utcnow()
    falls = [_alert(start + timedelta(minutes=m), "Fall") for m in (1, 2, 3, 4)]
    entry = _entry(hass)
    with patch("custom_components.mymedicalguardian.event._PERSIST_FIRED", 2):
        await _setup(hass, entry, [Snapshot()])
        freezer.move_to(start + timedelta(minutes=10))
        await _poll(hass, entry, _snap(*reversed(falls)))
        assert fires == ["Fall"] * 4

        freezer.move_to(start + timedelta(minutes=20))
        await _reload(hass, entry, [_snap(*reversed(falls))] * 2)

    assert fires == ["Fall"] * 6


async def test_stored_list_holds_only_the_lookback_window(hass, freezer):
    # A key older than the lookback can never matter after a restart, so it is
    # not carried in the event attributes.
    start = utcnow()
    early = _alert(start + timedelta(minutes=1), "Heart Beat")
    late = _alert(start + timedelta(hours=4), "Heart Beat")
    entry = _entry(hass)
    await _setup(hass, entry, [Snapshot()])
    freezer.move_to(start + timedelta(minutes=10))
    await _poll(hass, entry, _snap(early))
    freezer.move_to(start + timedelta(hours=4, minutes=10))
    await _poll(hass, entry, _snap(late, early))

    attrs = hass.states.get("event.mom_alert").attributes
    assert attrs["fired"] == [["Heart Beat", late.observed_at.isoformat()]]


def _restored_from_1_0_2(hass, beat):
    stamp = beat.observed_at.isoformat()
    _restored(
        hass, observed_at=stamp, fired_through=stamp, fired=[["Heart Beat", stamp]]
    )


async def test_late_fall_fires_after_restart_from_1_0_2_state(hass, fires):
    # 1.0.2 stored the fired list without a floor. A late Fall and an already
    # handled one look the same from that state, and a missed Fall costs more
    # than a repeated one, so that one restart keeps the full 3 hour lookback.
    t = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    beat = _alert(t + timedelta(minutes=10), "Heart Beat")
    fall = _alert(t + timedelta(minutes=5), "Fall")
    _restored_from_1_0_2(hass, beat)
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(beat), _snap(beat, fall)])

    assert fires == ["Fall"]


async def test_state_stored_by_1_0_2_can_repeat_an_alert_once_and_never_again(
    hass, fires
):
    # The cost of that choice: an alert from behind 1.0.2's baseline can fire
    # once more on the restart that reads its state. The floor stored from then
    # on stops any further replay.
    t = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    beat = _alert(t, "Heart Beat")
    old_fall = _alert(t - timedelta(hours=1), "Fall")
    _restored_from_1_0_2(hass, beat)
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(beat, old_fall)])
    assert fires == ["Fall"]

    await _reload(hass, entry, [_snap(beat, old_fall), _snap(beat, old_fall)])

    assert fires == ["Fall"]


async def test_non_string_restore_values_are_ignored(hass, fires):
    t = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    _restored(
        hass, observed_at=t.isoformat(), fired_through=5, fired_floor=["x"], fired=[]
    )
    entry = _entry(hass)
    fall = _alert(t + timedelta(minutes=1), "Fall")
    await _setup(hass, entry, [_snap(fall)])

    assert hass.states.get("event.mom_alert") is not None
    assert fires == ["Fall"]


@pytest.mark.parametrize(
    "stored",
    [
        {"fired_floor": "2026-05-12T11:00:00", "fired": []},
        {"fired": [["Heart Beat", "2026-05-12T11:00:00"]]},
        {"fired_through": "2026-05-12T12:00:00", "fired": []},
    ],
    ids=["naive-floor", "naive-fired-entry", "naive-mark"],
)
async def test_naive_restore_stamps_are_read_as_utc(hass, fires, stored):
    # Every released version stored offset-aware stamps; a hand-edited or
    # corrupt store must not stop the entity from loading.
    t = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    _restored(
        hass, **{"observed_at": t.isoformat(), "fired_through": t.isoformat()} | stored
    )
    entry = _entry(hass)
    await _setup(hass, entry, [_snap(_alert(t + timedelta(minutes=1), "Fall"))])

    assert hass.states.get("event.mom_alert") is not None
    assert fires == ["Fall"]


async def test_readme_automation_delivers_both_criticals_of_one_poll(hass):
    # Runs the README's own example automation against a notify that takes a
    # moment. Without `mode: queued` Home Assistant skips the second run
    # ("Already running") and one of two emergencies never notifies.
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    section = readme.split("### Notify on Fall / SOS", 1)[1]
    automation = yaml.safe_load(section.split("```yaml", 1)[1].split("```", 1)[0])
    titles: list[str] = []

    async def slow_notify(call):
        await asyncio.sleep(0.05)
        titles.append(call.data["title"])

    hass.services.async_register("notify", "family_group", slow_notify)
    assert await async_setup_component(hass, "automation", {"automation": automation})
    base = utcnow() + timedelta(minutes=5)
    fall = _alert(base.replace(second=30), "Fall")
    sos = _alert(base.replace(second=50), "SOS")
    entry = _entry(hass)
    await _setup(hass, entry, [Snapshot(), _snap(sos, fall)])
    await asyncio.sleep(0.3)
    await hass.async_block_till_done()

    assert [title.split()[1] for title in titles] == ["Fall", "SOS"]


@pytest.fixture
def fired_keys():
    """Like `fires`, but the (alert_type, observed_at) key of each triggered event."""
    seen: list[tuple[str, str]] = []
    original = MGAlertEvent._trigger_event

    def spy(self, event_type, event_attributes=None):
        seen.append((event_attributes["alert_type"], event_attributes["observed_at"]))
        return original(self, event_type, event_attributes)

    with patch.object(MGAlertEvent, "_trigger_event", spy):
        yield seen


@pytest.mark.parametrize("seed", range(60))
async def test_no_alert_fires_twice_and_no_seen_critical_is_missed(
    hass, freezer, fired_keys, seed
):
    # Property over random portal windows and restarts. Whatever order alerts
    # surface in and wherever HA restarts: nothing fires twice, nothing from
    # before monitoring began fires, and every Fall/SOS stamped after it began
    # that the portal ever showed fires exactly once.
    rng = random.Random(seed)
    start = utcnow()
    minutes = rng.sample([-120, -30, 1, 2, 3, 4, 5, 6, 7, 8, 9], k=rng.randint(3, 8))
    pool = [
        _alert(
            start + timedelta(minutes=m),
            rng.choice(["Fall", "SOS", "Heart Beat", "On Cradle"]),
        )
        for m in minutes
    ]
    entry = _entry(hass)
    await _setup(hass, entry, [Snapshot()])
    origin, shown = start, set()
    for step in range(rng.randint(2, 6)):
        freezer.move_to(start + timedelta(minutes=10 + step))
        window = _snap(*rng.sample(pool, k=rng.randint(0, len(pool))))
        if rng.random() < 0.4:
            if not fired_keys:
                # Nothing has fired, so nothing was stored: monitoring begins
                # again at this restart, as it does on a fresh install.
                origin = utcnow()
            await _reload(hass, entry, [window])
        else:
            await _poll(hass, entry, window)
        shown |= {(a.type, a.observed_at) for a in window.alerts}

    fired = [(t, datetime.fromisoformat(when)) for t, when in fired_keys]
    assert len(fired) == len(set(fired)), f"fired twice: {fired}"
    assert all(when > origin for _, when in fired), (
        f"fired from before monitoring: {fired}"
    )
    owed = {k for k in shown if k[0] in ("Fall", "SOS") and k[1] > origin}
    assert owed <= set(fired), f"critical shown but never fired: {owed - set(fired)}"
