"""Diagnostics dump tests — payload shape + redaction."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import Alert, Location, Snapshot
from custom_components.mymedicalguardian.const import DOMAIN
from custom_components.mymedicalguardian.diagnostics import (
    async_get_config_entry_diagnostics,
)

_NOW = datetime(2026, 5, 12, 14, 0, tzinfo=UTC)
_SNAPSHOT = Snapshot(
    device_model="MGMini-Pearl VZ",
    locations=[Location("GPS", "1 Oak St", 42.5, -84.5, _NOW, "raw")],
    alerts=(Alert("Fall", _NOW, "1 Oak St", 42.5, -84.5),),
)


async def test_diagnostics_redacts_credentials_and_coords(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={
            CONF_NAME: "Mom",
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "supersecret",
        },
        title="Mom",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(return_value=_SNAPSHOT),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # The monitored person's name is PII on a medical device — redact it from
    # both the title and entry data.
    assert diag["entry"]["title"] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_EMAIL] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_NAME] == "**REDACTED**"
    assert diag["snapshot"]["device_model"] == "MGMini-Pearl VZ"
    for key in ("latest_location", "latest_alert"):
        for field in ("address", "latitude", "longitude"):
            assert diag["snapshot"][key][field] == "**REDACTED**"
    assert diag["snapshot"]["latest_alert"]["type"] == "Fall"
