"""Event entity that fires whenever a new alert appears in the portal.

Unlike `sensor.event` (which exposes the latest *location-history* event type as
a string state), this `EventEntity` triggers HA's event bus the moment a new
deviceStatus entry appears — perfect for automating on Fall / SOS within one
poll cycle.

Firing is gated by a fixed baseline (`_fired_through`: the mark restored from the
last event HA persisted, else the moment monitoring began) plus a set of the
alerts already fired, keyed `(type, observed_at)`. An alert fires only when it is
newer than the baseline and not yet in the set, so the entity never re-fires one,
never replays an alert that predates startup, and cannot lose an older Fall/SOS
because a newer routine event fired first (the portal posts with lag, so arrival
order is not time order). Each poll fires every unfired Fall/SOS oldest-first; if
none, the newest unfired routine event, and routine events older than it are
dropped as superseded.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.dt import utcnow

from . import MGConfigEntry
from .api import Alert, critical_type
from .coordinator import MGCoordinator
from .entity import MGEntity

PARALLEL_UPDATES = 0

ALERT_TYPES = [
    "Heart Beat", "On Cradle", "Off Cradle", "Fall", "SOS", "Manual Test", "Power On",
]  # fmt: skip
# Catch-all so a portal-introduced type still fires an automation (HA rejects a
# _trigger_event type absent from _attr_event_types) instead of being dropped.
OTHER_ALERT = "Other"
EVENT_TYPES = [*ALERT_TYPES, OTHER_ALERT]

# Bound on remembered fired alerts; far above the portal's recent-alerts window.
_FIRED_CAP = 256
# How many fired alerts ride along in the event attributes so a restart can tell
# fired from unfired, and how far before the restored mark it still looks.
_PERSIST_FIRED = 16
_RESTART_LOOKBACK = timedelta(hours=3)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _fired_key(item: object) -> tuple[str, datetime] | None:
    if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)):
        return None
    when = _parse_iso(item[1]) if isinstance(item[1], str) else None
    return (item[0], when) if when else None


def _same_minute(a: datetime, b: datetime) -> bool:
    return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MGConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([MGAlertEvent(entry.runtime_data, entry)])


class MGAlertEvent(MGEntity, EventEntity):
    _attr_translation_key = "alert"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "alert")
        self._fired_through: datetime | None = None
        self._fired: set[tuple[str, datetime]] = set()
        self._latest_fired: datetime | None = None
        self._added_at = utcnow()

    async def async_added_to_hass(self) -> None:
        # CoordinatorEntity wires up future updates but doesn't replay the current
        # snapshot. Restore the high-water mark from the last event HA persisted
        # before shutdown, then evaluate the initial snapshot once.
        await super().async_added_to_hass()
        legacy_marker = False
        if (last := await self.async_get_last_event_data()) is not None:
            attrs = last.last_event_attributes or {}
            # fired_through is clamped to the restart clock; observed_at is the
            # last event's own (possibly skewed, future) stamp, so it is only the
            # fallback for events persisted before fired_through existed.
            mark = _parse_iso(attrs.get("fired_through")) or _parse_iso(
                attrs.get("observed_at")
            )
            self._latest_fired = mark
            self._fired_through = mark
            persisted = attrs.get("fired")
            if mark is not None and isinstance(persisted, list):
                # The fired set survived the restart, so look back past the mark:
                # a Fall posted late with a stamp older than the last routine
                # event is unfired and must still go out.
                self._fired = {k for i in persisted if (k := _fired_key(i))}
                self._fired_through = mark - _RESTART_LOOKBACK
            # v1.0.0 persisted minute-resolution marks and never set the
            # 'alert_type' attribute (added in v1.0.1), so its absence is a
            # reliable legacy signal — unlike the wall-clock seconds, which the
            # portal legitimately stamps on :00 boundaries.
            legacy_marker = (
                self._fired_through is not None and "alert_type" not in attrs
            )
        if legacy_marker:
            self._adopt_minute_resolution_marker()
        self._handle_coordinator_update()

    def _adopt_minute_resolution_marker(self) -> None:
        # Restored mark is a v1.0.0 minute-resolution value. An in-window alert in
        # that same minute is the same already-fired event at finer (second)
        # resolution: raise the mark to it WITHOUT firing so the upgrade restart
        # doesn't replay it. Scan the whole list (not just the newest) so a newer
        # routine event can't hide the still-in-window fired critical.
        hwm = self._fired_through
        if hwm is None:
            return
        alerts = self.coordinator.data.alerts if self.coordinator.data else ()
        same_minute = [
            a.observed_at
            for a in alerts
            if a.observed_at > hwm and _same_minute(a.observed_at, hwm)
        ]
        if same_minute:
            self._fired_through = max(same_minute)

    @callback
    def _handle_coordinator_update(self) -> None:
        alerts = self.coordinator.data.alerts if self.coordinator.data else ()
        for alert in self._pick(alerts):
            self._fire(alert)
            # _trigger_event only stages the event; write per alert so each one
            # reaches the state machine (and automations), not just the last.
            self.async_write_ha_state()
        # Always write state so availability tracks coordinator success even on a
        # poll that fires no event.
        self.async_write_ha_state()

    def _pick(self, alerts: tuple[Alert, ...]) -> list[Alert]:
        # Baseline is the restored mark or, before anything has fired, the moment
        # monitoring began, so a pre-install alert is never fired no matter which
        # poll first surfaces it. Every unfired Fall/SOS fires oldest-first in this
        # poll (a caregiver should not wait a scan interval for the second one).
        # With no critical, fire only the newest routine alert and mark the older
        # ones superseded so they are not drip-fed one per poll. (Two distinct
        # alerts of one type stamped at the exact same second collapse to one fire.)
        baseline = self._fired_through or self._added_at
        unfired = {
            key: a
            for a in alerts
            if a.observed_at > baseline
            and (key := (a.type, a.observed_at)) not in self._fired
        }
        fresh = list(unfired.values())
        if not fresh:
            return []
        if critical := [a for a in fresh if critical_type(a.type)]:
            return sorted(critical, key=lambda a: a.observed_at)
        newest = max(fresh, key=lambda a: a.observed_at)
        self._remember(a for a in fresh if a is not newest)
        return [newest]

    def _remember(self, alerts: Iterable[Alert]) -> None:
        self._fired.update((a.type, a.observed_at) for a in alerts)
        if len(self._fired) > _FIRED_CAP:
            self._fired = set(sorted(self._fired, key=lambda k: k[1])[-_FIRED_CAP:])

    def _fire(self, alert: Alert) -> None:
        self._remember([alert])
        # Persisted so a restart does not replay a routine event newer than a
        # late-arriving Fall that was the last thing fired. Clamped to now so a
        # skewed future stamp cannot block real alerts after the restart.
        mark = min(alert.observed_at, utcnow())
        self._latest_fired = max(mark, self._latest_fired or mark)
        event_type = critical_type(alert.type) or (
            alert.type if alert.type in ALERT_TYPES else OTHER_ALERT
        )
        # Nest coords under `location` so HA's map doesn't sniff lat/lon and render
        # this event entity as an extra pin (the device_tracker is canonical).
        self._trigger_event(
            event_type,
            {
                "alert_type": alert.type,
                "address": alert.address,
                "location": {
                    "latitude": alert.latitude,
                    "longitude": alert.longitude,
                },
                "observed_at": alert.observed_at.astimezone(UTC).isoformat(),
                "fired_through": self._latest_fired.astimezone(UTC).isoformat(),
                "fired": [
                    [t, when.astimezone(UTC).isoformat()]
                    for t, when in sorted(self._fired, key=lambda k: k[1])[
                        -_PERSIST_FIRED:
                    ]
                ],
            },
        )
