"""Event entity that fires whenever a new alert appears in the portal.

Unlike `sensor.event` (which exposes the latest *location-history* event type as
a string state), this `EventEntity` triggers HA's event bus the moment a new
deviceStatus entry appears — perfect for automating on Fall / SOS within one
poll cycle.
"""

from datetime import UTC, datetime, timedelta

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

# On a fresh install (no restored event to suppress against) only fire for an
# alert that occurred within this window of startup, so a days-old Fall/SOS
# isn't replayed. Fixed rather than tied to the poll interval, which can be 2h.
FRESH_INSTALL_WINDOW = timedelta(minutes=15)

ALERT_TYPES = [
    "Heart Beat",
    "On Cradle",
    "Off Cradle",
    "Fall",
    "SOS",
    "Manual Test",
    "Power On",
]


def _to_utc_iso(value: str) -> str:
    """Normalize a restored observed_at string to the same UTC form dedup_key
    uses, so a marker persisted by an older build (local offset) still matches."""
    try:
        return datetime.fromisoformat(value).astimezone(UTC).isoformat()
    except ValueError:
        return value


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
        self._restored = False

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
                self._seen = (last.last_event_type, _to_utc_iso(seen_at))
        self._restored = self._seen is not None
        self._handle_coordinator_update()

    def _is_recent(self, alert: Alert) -> bool:
        return utcnow() - alert.observed_at <= FRESH_INSTALL_WINDOW

    @callback
    def _handle_coordinator_update(self) -> None:
        alert = self.coordinator.data.latest_alert if self.coordinator.data else None
        if alert and alert.type in ALERT_TYPES and alert.dedup_key != self._seen:
            # Fresh install only (nothing restored AND nothing seen yet): don't
            # replay an alert that predates startup, whether it's in the first
            # snapshot or first surfaces on a later poll. Once an alert has been
            # recorded (or a marker restored), every genuinely new alert fires
            # regardless of age — never drop a real Fall/SOS from HA downtime.
            suppress = (
                not self._restored and self._seen is None and not self._is_recent(alert)
            )
            self._seen = alert.dedup_key
            if not suppress:
                # Nest coords under `location` so HA's map doesn't sniff lat/lon
                # and render this event entity as an extra pin (the
                # device_tracker is the canonical map source).
                self._trigger_event(
                    alert.type,
                    {
                        "address": alert.address,
                        "location": {
                            "latitude": alert.latitude,
                            "longitude": alert.longitude,
                        },
                        # Persist the same normalized value dedup_key uses, so the
                        # marker restored from this attribute matches on restart.
                        "observed_at": alert.dedup_key[1],
                    },
                )
        # Always write state so availability tracks coordinator success even on a
        # poll that fires no event.
        self.async_write_ha_state()
