"""Binary sensors: device online (recent observation) + charging (on cradle)."""

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import utcnow

from . import MGConfigEntry
from .coordinator import MGCoordinator
from .entity import device_info

PARALLEL_UPDATES = 0

STALE_AFTER = timedelta(minutes=90)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MGConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord = entry.runtime_data
    async_add_entities(
        [
            MGOnlineSensor(coord, entry),
            MGChargingSensor(coord, entry),
        ]
    )


class _Base(CoordinatorEntity[MGCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MGCoordinator, entry: MGConfigEntry, suffix: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = device_info(entry, coordinator)


class MGOnlineSensor(_Base):
    """`on` while the device has phoned home in the last 90 minutes."""

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "online")

    @property
    def is_on(self) -> bool | None:
        loc = self.coordinator.data.latest_location if self.coordinator.data else None
        if not loc:
            return None
        return (utcnow() - loc.observed_at) < STALE_AFTER


class MGChargingSensor(_Base):
    """`on` while the device sits on its charging cradle."""

    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "charging")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.charging if self.coordinator.data else None
