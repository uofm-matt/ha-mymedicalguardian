"""Diagnostics dump for MyMedicalGuardian Tracker.

Triggered from Settings → Devices & Services → MyMedicalGuardian Tracker
→ ⋮ → Download diagnostics. Useful when filing bug reports.
"""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import MGConfigEntry

# "title" and CONF_NAME both hold the monitored person's name — PII for a
# medical-alert device, redacted alongside credentials and precise location.
TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_NAME,
    "title",
    "address",
    "latitude",
    "longitude",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: MGConfigEntry,
) -> dict[str, Any]:
    snap = entry.runtime_data.data
    snapshot: dict[str, Any] = {}
    if snap is not None:
        snapshot = {
            "battery_percent": snap.battery_percent,
            "charging": snap.charging,
            "signal_label": snap.signal_label,
            "signal_slug": snap.signal_slug,
            "steps_today": snap.steps_today,
            "miles_today": snap.miles_today,
            "calories_today": snap.calories_today,
            "device_model": snap.device_model,
            "locations_count": len(snap.locations),
            "latest_location": {
                "event": snap.latest_location.event,
                "observed_raw": snap.latest_location.observed_raw,
                "latitude": snap.latest_location.latitude,
                "longitude": snap.latest_location.longitude,
                "address": snap.latest_location.address,
            }
            if snap.latest_location
            else None,
            "latest_alert": {
                "type": snap.latest_alert.type,
                "observed_at": snap.latest_alert.observed_at.isoformat(),
                "address": snap.latest_alert.address,
                "latitude": snap.latest_alert.latitude,
                "longitude": snap.latest_alert.longitude,
            }
            if snap.latest_alert
            else None,
        }

    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "version": entry.version,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "snapshot": snapshot,
        },
        TO_REDACT,
    )
