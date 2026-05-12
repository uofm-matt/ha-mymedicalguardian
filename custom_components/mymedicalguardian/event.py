"""Event entity that fires whenever a new alert appears in the portal.

Unlike `sensor.event` (which exposes the latest *location-history* event type as
a string state), this `EventEntity` triggers HA's event bus the moment a new
deviceStatus entry appears — perfect for automating on Fall / SOS within one
poll cycle.
"""

from datetime import datetime

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MGConfigEntry
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
        self._seen_at: datetime | None = None

    async def async_added_to_hass(self) -> None:
        # CoordinatorEntity hooks up the listener for future updates, but does
        # not invoke _handle_coordinator_update with whatever's already on the
        # coordinator at setup time. Fire once on the initial snapshot so the
        # latest alert is visible immediately after a restart.
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        alert = self.coordinator.data.latest_alert if self.coordinator.data else None
        if not (
            alert and alert.observed_at != self._seen_at and alert.type in ALERT_TYPES
        ):
            return
        self._seen_at = alert.observed_at
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
