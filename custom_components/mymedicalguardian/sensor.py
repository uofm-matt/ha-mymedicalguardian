"""Sensors derived from the dashboard + location-history snapshot."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MGConfigEntry
from .coordinator import MGCoordinator
from .entity import MGEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MGConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord = entry.runtime_data
    async_add_entities(
        [
            MGEventSensor(coord, entry),
            MGLastSeenSensor(coord, entry),
            MGBatterySensor(coord, entry),
            MGSignalSensor(coord, entry),
            MGStepsSensor(coord, entry),
        ]
    )


class _Base(MGEntity, SensorEntity):
    pass


class MGEventSensor(_Base):
    """Latest event type — Heart Beat / SOS / Fall / Manual Test / Power On."""

    _attr_translation_key = "event"

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "event")

    @property
    def native_value(self) -> str | None:
        loc = self.coordinator.data.latest_location if self.coordinator.data else None
        return loc.event if loc else None


class MGLastSeenSensor(_Base):
    """Timestamp of the latest observation. Renders as 'N min ago' in HA."""

    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_seen")

    @property
    def native_value(self) -> datetime | None:
        loc = self.coordinator.data.latest_location if self.coordinator.data else None
        return loc.observed_at if loc else None


class MGBatterySensor(_Base):
    """Battery percentage reported by the device's last heartbeat."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "battery")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.battery_percent if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        snap = self.coordinator.data
        if snap and snap.battery_observed_at:
            return {"observed_at": snap.battery_observed_at.isoformat()}
        return {}


class MGSignalSensor(_Base):
    """Cell-signal label as reported by the portal (Good / Fair / Poor / None)."""

    _attr_translation_key = "signal"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "signal")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.signal_label if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        snap = self.coordinator.data
        return {"slug": snap.signal_slug} if snap and snap.signal_slug else {}


class MGStepsSensor(_Base):
    """Step count for the current day. Resets at midnight in the device's tz."""

    _attr_translation_key = "steps_today"
    _attr_native_unit_of_measurement = "steps"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator, entry, "steps_today")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.steps_today if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, float]:
        snap = self.coordinator.data
        if not snap:
            return {}
        out: dict[str, float] = {}
        if snap.miles_today is not None:
            out["miles"] = round(snap.miles_today, 2)
        if snap.calories_today is not None:
            out["calories"] = round(snap.calories_today, 2)
        return out
