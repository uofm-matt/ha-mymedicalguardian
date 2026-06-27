"""Event entity that fires whenever a new alert appears in the portal.

Unlike `sensor.event` (which exposes the latest *location-history* event type as
a string state), this `EventEntity` triggers HA's event bus the moment a new
deviceStatus entry appears — perfect for automating on Fall / SOS within one
poll cycle.

Firing is gated by a monotonic high-water mark (`_fired_through`, the observed_at
of the last alert fired). Each poll fires the oldest unfired Fall/SOS — so the
mark advances minimally and a second simultaneous critical fires on the next poll
rather than being swallowed — else the newest unfired routine event. An alert
fires only when strictly newer than the mark, so the entity never re-fires one,
never replays an alert that predates startup, and is immune to the de-dup edge
cases (oscillation, restore-resolution mismatch) a single-slot marker had.
"""

from datetime import UTC, datetime

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import utcnow

from . import MGConfigEntry
from .api import _CRITICAL_ALERTS, Alert
from .coordinator import MGCoordinator
from .entity import device_info

PARALLEL_UPDATES = 0

ALERT_TYPES = [
    "Heart Beat", "On Cradle", "Off Cradle", "Fall", "SOS", "Manual Test", "Power On",
]  # fmt: skip
# Catch-all so a portal-introduced type still fires an automation (HA rejects a
# _trigger_event type absent from _attr_event_types) instead of being dropped.
OTHER_ALERT = "Other"
EVENT_TYPES = [*ALERT_TYPES, OTHER_ALERT]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _same_minute(a: datetime, b: datetime) -> bool:
    return a.replace(second=0, microsecond=0) == b.replace(second=0, microsecond=0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MGConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([MGAlertEvent(entry.runtime_data, entry)])


class MGAlertEvent(CoordinatorEntity[MGCoordinator], EventEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "alert"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_alert"
        self._attr_device_info = device_info(entry, coordinator)
        self._fired_through: datetime | None = None
        self._added_at = utcnow()

    async def async_added_to_hass(self) -> None:
        # CoordinatorEntity wires up future updates but doesn't replay the current
        # snapshot. Restore the high-water mark from the last event HA persisted
        # before shutdown, then evaluate the initial snapshot once.
        await super().async_added_to_hass()
        legacy_marker = False
        if (last := await self.async_get_last_event_data()) is not None:
            attrs = last.last_event_attributes or {}
            self._fired_through = _parse_iso(attrs.get("observed_at"))
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
        if alert := self._pick(alerts):
            self._fire(alert)
        # Always write state so availability tracks coordinator success even on a
        # poll that fires no event.
        self.async_write_ha_state()

    def _pick(self, alerts: tuple[Alert, ...]) -> Alert | None:
        # Baseline is the last-fired mark, or — before anything has fired — the
        # moment monitoring began, so a pre-install alert is never fired no matter
        # which poll first surfaces it. A genuine post-startup alert is strictly
        # newer; on a restart the restored mark lets a real HA-downtime alert
        # through. Fire the OLDEST unfired Fall/SOS first so the mark advances
        # minimally and any newer critical in the same window still fires on the
        # next poll (never swallowed); with no critical, fire the newest routine.
        # (Two distinct alerts stamped at the exact same second collapse to one
        # fire — the caregiver is still alerted to that instant.)
        baseline = self._fired_through or self._added_at
        fresh = [a for a in alerts if a.observed_at > baseline]
        if not fresh:
            return None
        critical = [a for a in fresh if a.type in _CRITICAL_ALERTS]
        if critical:
            return min(critical, key=lambda a: a.observed_at)
        return fresh[0]  # alerts are newest-first

    def _fire(self, alert: Alert) -> None:
        self._fired_through = alert.observed_at
        event_type = alert.type if alert.type in ALERT_TYPES else OTHER_ALERT
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
            },
        )
