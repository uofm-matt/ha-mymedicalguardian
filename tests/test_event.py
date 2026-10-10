"""Event entity tests — fired-set firing over the recent-alerts list.

The entity fires an alert only when it is newer than the baseline (the restored
mark, or the time monitoring began) and not already fired, firing every fresh
Fall/SOS oldest-first so a critical is never masked by a routine event nor lost
because a newer routine one fired earlier. It never replays a pre-install alert,
never re-fires one, and survives restart and a v1.0.0 -> 1.0.1 upgrade without
phantom events.
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


async def test_malformed_fired_list_entries_are_ignored(hass):
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
    assert hass.states.get("event.mom_alert").attributes["alert_type"] == "Heart Beat"
