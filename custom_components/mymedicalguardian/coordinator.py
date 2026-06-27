"""DataUpdateCoordinator for MyMedicalGuardian."""

import logging
from datetime import datetime, timedelta

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import utcnow
from homeassistant.util.ssl import client_context

from .api import AuthError, FetchError, Snapshot, fetch_snapshot
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN, DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_PORTAL_UNREACHABLE = "portal_unreachable"
PORTAL_UNREACHABLE_AFTER = timedelta(hours=24)
ISSUE_URL = "https://github.com/uofm-matt/ha-mymedicalguardian/issues"
# Keyed by entry_id in hass.data so the first-failure timestamp survives the
# coordinator being recreated on every ConfigEntryNotReady setup retry —
# otherwise the 24h repair issue could never fire when the portal is down at
# initial setup.
_FAILURE_STORE = f"{DOMAIN}_first_failures"


class MGCoordinator(DataUpdateCoordinator[Snapshot]):
    """Polls mymedicalguardian.com and exposes the latest snapshot.

    Owns its own ``httpx.AsyncClient`` rather than borrowing HA's shared one:
    the login flow is cookie-stateful (login → cookie → fetch → cookie cleared)
    and depends on ``follow_redirects=True``, neither of which the shared
    client guarantees. Closed cleanly in ``async_shutdown``.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
        )
        self._email: str = entry.data[CONF_EMAIL]
        self._password: str = entry.data[CONF_PASSWORD]
        # Pass HA's cached SSL context so constructing the client never builds an
        # SSLContext on the event loop (a blocking disk read in newer httpx).
        self._client = httpx.AsyncClient(
            verify=client_context(), follow_redirects=True, timeout=30.0
        )
        self._entry_id = entry.entry_id
        # Per-entry issue id so two accounts (two config entries) don't collide
        # on a single domain-wide repair issue.
        self._issue_id = f"{ISSUE_PORTAL_UNREACHABLE}_{entry.entry_id}"

    @property
    def _first_failure_at(self) -> datetime | None:
        store: dict[str, datetime] = self.hass.data.get(_FAILURE_STORE, {})
        return store.get(self._entry_id)

    @_first_failure_at.setter
    def _first_failure_at(self, value: datetime | None) -> None:
        store: dict[str, datetime] = self.hass.data.setdefault(_FAILURE_STORE, {})
        if value is None:
            store.pop(self._entry_id, None)
        else:
            store[self._entry_id] = value

    async def _async_update_data(self) -> Snapshot:
        # Login produces a session cookie; clear stale cookies before each
        # poll so a previous session's state can't carry over.
        self._client.cookies.clear()
        try:
            snap = await fetch_snapshot(self._client, self._email, self._password)
        except AuthError as err:
            # Auth failure proves the portal is reachable, so any pending
            # portal-unreachable repair issue is no longer relevant.
            self._clear_portal_unreachable()
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
            ) from err
        except (FetchError, httpx.HTTPError, OSError) as err:
            reason = str(err) or type(err).__name__
            self._record_failure(reason)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="fetch_failed",
                translation_placeholders={"reason": reason},
            ) from err
        self._clear_portal_unreachable()
        return snap

    def _record_failure(self, reason: str) -> None:
        now = utcnow()
        if self._first_failure_at is None:
            self._first_failure_at = now
            return
        if now - self._first_failure_at < PORTAL_UNREACHABLE_AFTER:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PORTAL_UNREACHABLE,
            translation_placeholders={"reason": reason, "issue_url": ISSUE_URL},
        )

    def _clear_portal_unreachable(self) -> None:
        self._first_failure_at = None
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    async def async_shutdown(self) -> None:
        await self._client.aclose()
        await super().async_shutdown()


def forget_failure_state(hass: HomeAssistant, entry_id: str) -> None:
    """Drop an entry's persisted first-failure timestamp. Called on entry removal
    so the hass.data store doesn't retain a datetime for a deleted entry. Not done
    in async_shutdown, which also runs on every setup-retry where the timestamp
    must survive."""
    hass.data.get(_FAILURE_STORE, {}).pop(entry_id, None)
