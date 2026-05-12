"""MyMedicalGuardian Tracker integration entry point."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import MGCoordinator

type MGConfigEntry = ConfigEntry[MGCoordinator]

PLATFORMS = [
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
]


async def async_setup_entry(hass: HomeAssistant, entry: MGConfigEntry) -> bool:
    coordinator = MGCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MGConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: MGConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
