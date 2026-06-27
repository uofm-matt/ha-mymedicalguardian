"""Event entity that fires whenever a new alert appears in the portal.

Unlike `sensor.event` (which exposes the latest *location-history* event type as
a string state), this `EventEntity` triggers HA's event bus the moment a new
deviceStatus entry appears — perfect for automating on Fall / SOS within one
poll cycle.

Firing is gated by a monotonic high-water mark (`_fired_through`, the observed_at
of the newest alert already fired). An alert fires only when it is strictly newer
than that mark, which means the entity never re-fires an alert, never replays one
that predates startup, and is immune to the de-dup edge cases (oscillating
latest_alert, restore-resolution mismatch) a single-slot identity marker had.
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
        self._added_at = utcnow()
        if (last := await self.async_get_last_event_data()) is not None:
            self._fired_through = _parse_iso(
                (last.last_event_attributes or {}).get("observed_at")
            )
        # Upgrade safety: a v1.0.0 marker is minute-resolution. If the live alert
        # is the same event at finer (second) resolution, raise the mark to it
        # without firing so the upgrade restart doesn't replay it.
        alert = self.coordinator.data.latest_alert if self.coordinator.data else None
        hwm = self._fired_through
        if (
            hwm is not None
            and alert is not None
            and alert.observed_at > hwm
            and _same_minute(alert.observed_at, hwm)
        ):
            self._fired_through = alert.observed_at
        self._handle_coordinator_update()

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
        # through. Among the unfired alerts, surface the newest Fall/SOS first so
        # a critical event is never delayed by a routine one; the rest fire on the
        # following polls (each advances the mark).
        baseline = self._fired_through or self._added_at
        fresh = [a for a in alerts if a.observed_at > baseline]
        if not fresh:
            return None
        critical = [a for a in fresh if a.type in _CRITICAL_ALERTS]
        return (critical or fresh)[0]  # alerts are newest-first

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
