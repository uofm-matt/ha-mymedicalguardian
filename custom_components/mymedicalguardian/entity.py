"""Shared device-info helper so every entity gets the same identifiers/model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from . import MGConfigEntry
    from .coordinator import MGCoordinator


def device_info(entry: MGConfigEntry, coordinator: MGCoordinator) -> DeviceInfo:
    snap = coordinator.data
    model = snap.device_model if snap and snap.device_model else None
    info: DeviceInfo = {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "manufacturer": "Medical Guardian",
        "name": entry.title,
        "entry_type": DeviceEntryType.SERVICE,
        "configuration_url": "https://www.mymedicalguardian.com/",
    }
    if model:
        info["model"] = model
    return info


class MGEntity(CoordinatorEntity["MGCoordinator"]):
    """Common base: unique id `<entry_id>_<suffix>`, shared device, entity names."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MGCoordinator, entry: MGConfigEntry, suffix: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = device_info(entry, coordinator)
