"""Event entity that fires whenever a new alert appears in the portal.

Unlike `sensor.event` (which exposes the latest *location-history* event type as
a string state), this `EventEntity` triggers HA's event bus the moment a new
deviceStatus entry appears — perfect for automating on Fall / SOS within one
poll cycle.
"""

from datetime import timedelta

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import utcnow

from . import MGConfigEntry
from .api import Alert
from .coordinator import MGCoordinator
from .entity import device_info

PARALLEL_UPDATES = 0

ALERT_TYPES = [
    "Heart Beat",
    "On Cradle",
    "Off Cradle",
    "Fall",
    "SOS",
    "Manual Test",
    "Power On",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MGConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([MGAlertEvent(entry.runtime_data, entry)])


class MGAlertEvent(CoordinatorEntity[MGCoordinator], EventEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "alert"
    _attr_event_types = ALERT_TYPES

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_alert"
        self._attr_device_info = device_info(entry, coordinator)
        self._seen: tuple[str, str] | None = None
        self._primed = False

    async def async_added_to_hass(self) -> None:
        # CoordinatorEntity hooks up the listener for future updates, but does
        # not invoke _handle_coordinator_update with whatever's already on the
        # coordinator at setup time. Seed the de-dup marker from the event HA
        # restored from before shutdown so a restart doesn't re-fire an alert
        # that already triggered, then evaluate the initial snapshot once.
        await super().async_added_to_hass()
        if (last := await self.async_get_last_event_data()) is not None:
            seen_at = (last.last_event_attributes or {}).get("observed_at")
            if last.last_event_type and seen_at:
                self._seen = (last.last_event_type, seen_at)
        self._handle_coordinator_update()
        self._primed = True

    def _is_recent(self, alert: Alert) -> bool:
        """True if the alert is recent enough to be worth firing on the initial
        snapshot — bounded by one poll interval so we never replay a days-old
        Fall/SOS on a fresh install where there's no restored event to suppress."""
        window = self.coordinator.update_interval or timedelta(minutes=10)
        return utcnow() - alert.observed_at <= window

    @callback
    def _handle_coordinator_update(self) -> None:
        alert = self.coordinator.data.latest_alert if self.coordinator.data else None
        if not (alert and alert.type in ALERT_TYPES) or alert.dedup_key == self._seen:
            return
        # First evaluation after startup (fresh install, or restore cache lost):
        # record the alert as seen but don't fire if it predates HA coming up.
        # Live updates after priming always fire on a genuinely new alert.
        if not self._primed and not self._is_recent(alert):
            self._seen = alert.dedup_key
            return
        self._seen = alert.dedup_key
        # Nest coords under `location` so HA's map doesn't sniff lat/lon
        # at the top of the entity attributes and render this event entity
        # as an extra pin (the device_tracker is the canonical map source).
        self._trigger_event(
            alert.type,
            {
                "address": alert.address,
                "location": {
                    "latitude": alert.latitude,
                    "longitude": alert.longitude,
                },
                "observed_at": alert.observed_at.isoformat(),
            },
        )
        self.async_write_ha_state()
