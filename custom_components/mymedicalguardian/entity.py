"""Shared device-info helper so every entity gets the same identifiers/model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

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
