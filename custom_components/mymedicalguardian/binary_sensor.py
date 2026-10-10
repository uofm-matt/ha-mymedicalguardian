"""Binary sensors: device online (recent observation) + charging (on cradle)."""

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.dt import utcnow

from . import MGConfigEntry
from .coordinator import MGCoordinator
from .entity import MGEntity

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


class _Base(MGEntity, BinarySensorEntity):
    pass


class MGOnlineSensor(_Base):
    """`on` while the device phoned home recently — within a window that is the
    larger of 90 minutes and the poll interval plus 30 minutes, so a long scan
    interval doesn't flap a healthy device to off."""

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "online")

    @property
    def is_on(self) -> bool | None:
        loc = self.coordinator.data.latest_location if self.coordinator.data else None
        if not loc:
            return None
        # Data is refreshed only at the poll cadence and is legitimately up to one
        # interval old, so widen the staleness window to interval + 30 min (up to
        # 150 min at the 120-min max) — otherwise a healthy device flaps to off.
        interval = self.coordinator.update_interval or timedelta()
        window = max(STALE_AFTER, interval + timedelta(minutes=30))
        return (utcnow() - loc.observed_at) < window


class MGChargingSensor(_Base):
    """`on` while the device sits on its charging cradle."""

    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "charging")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.charging if self.coordinator.data else None
