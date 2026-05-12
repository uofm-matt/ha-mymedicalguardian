"""device_tracker platform: latest location of the care recipient."""

from homeassistant.components.device_tracker import (
    SourceType,
    TrackerEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MGConfigEntry
from .api import Location
from .coordinator import MGCoordinator
from .entity import device_info

# Entity state updates flow through the coordinator's single update; entities
# never poll on their own, so HA's parallel_updates limiter is unused.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MGConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([MGTracker(entry.runtime_data, entry)])


class MGTracker(CoordinatorEntity[MGCoordinator], TrackerEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "location"
    # BaseTrackerEntity declares _attr_device_info: None; we narrow it.
    _attr_device_info: DeviceInfo  # type: ignore[assignment]

    def __init__(self, coordinator: MGCoordinator, entry: MGConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_location"
        self._attr_device_info = device_info(entry, coordinator)

    def _loc(self) -> Location | None:
        data = self.coordinator.data
        return data.latest_location if data else None

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        loc = self._loc()
        return loc.latitude if loc else None

    @property
    def longitude(self) -> float | None:
        loc = self._loc()
        return loc.longitude if loc else None

    @property
    def location_accuracy(self) -> int:
        # The portal doesn't expose accuracy. Reporting 0 (HA default) means
        # "no estimate" rather than claiming a number we don't have, and avoids
        # the accuracy-halo on the map fighting with the user's chosen zone radii.
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        loc = self._loc()
        if not loc:
            return {}
        return {
            "address": loc.address,
            "event": loc.event,
            "observed_at": loc.observed_at.isoformat(),
            "observed_raw": loc.observed_raw,
        }
