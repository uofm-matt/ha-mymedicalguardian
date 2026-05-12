"""Diagnostics dump tests — payload shape + redaction."""

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import Snapshot
from custom_components.mymedicalguardian.const import DOMAIN
from custom_components.mymedicalguardian.diagnostics import (
    async_get_config_entry_diagnostics,
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
        AsyncMock(return_value=Snapshot(device_model="MGMini-Pearl VZ")),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["title"] == "Mom"
    assert diag["entry"]["data"][CONF_EMAIL] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_NAME] == "Mom"
    assert diag["snapshot"]["device_model"] == "MGMini-Pearl VZ"
    assert diag["snapshot"]["latest_location"] is None
    assert diag["snapshot"]["latest_alert"] is None
