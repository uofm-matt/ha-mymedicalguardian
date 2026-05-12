"""Config / reauth / reconfigure flow tests."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import AuthError, FetchError, Snapshot
from custom_components.mymedicalguardian.const import DOMAIN

USER_INPUT = {
    CONF_NAME: "Mom",
    CONF_EMAIL: "test@example.com",
    CONF_PASSWORD: "secret",
}


def _mock_fetch_ok():
    return patch(
        "custom_components.mymedicalguardian.config_flow.fetch_snapshot",
        AsyncMock(return_value=Snapshot()),
    )


def _mock_fetch_auth_error():
    return patch(
        "custom_components.mymedicalguardian.config_flow.fetch_snapshot",
        AsyncMock(side_effect=AuthError("bad creds")),
    )


def _mock_fetch_connect_error():
    return patch(
        "custom_components.mymedicalguardian.config_flow.fetch_snapshot",
        AsyncMock(side_effect=FetchError("server down")),
    )


class TestUserStep:
    async def test_happy_path_creates_entry(self, hass, bypass_setup_fixture):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        with _mock_fetch_ok():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], USER_INPUT
            )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == "Mom"
        assert result["data"] == USER_INPUT

    async def test_invalid_auth_shows_error(self, hass, bypass_setup_fixture):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        with _mock_fetch_auth_error():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], USER_INPUT
            )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}

    async def test_cannot_connect_shows_error(self, hass, bypass_setup_fixture):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        with _mock_fetch_connect_error():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], USER_INPUT
            )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_duplicate_email_aborts(self, hass, bypass_setup_fixture):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="test@example.com",
            data=USER_INPUT,
        )
        entry.add_to_hass(hass)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        with _mock_fetch_ok():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], USER_INPUT
            )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"


class TestReauthFlow:
    async def test_happy_path_updates_password(self, hass, bypass_setup_fixture):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="test@example.com",
            data=USER_INPUT,
        )
        entry.add_to_hass(hass)

        result = await entry.start_reauth_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        with _mock_fetch_ok():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "newpass"}
            )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_PASSWORD] == "newpass"

    async def test_reauth_invalid_creds_shows_error(self, hass, bypass_setup_fixture):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="test@example.com",
            data=USER_INPUT,
        )
        entry.add_to_hass(hass)

        result = await entry.start_reauth_flow(hass)
        with _mock_fetch_auth_error():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_PASSWORD: "wrong"}
            )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}


class TestReconfigureFlow:
    async def test_happy_path_updates_title_and_data(self, hass, bypass_setup_fixture):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="test@example.com",
            data=USER_INPUT,
            title="Mom",
        )
        entry.add_to_hass(hass)

        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        new_input = {
            CONF_NAME: "Mama",
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "newpass",
        }
        with _mock_fetch_ok():
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], new_input
            )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.title == "Mama"
        assert entry.data[CONF_PASSWORD] == "newpass"

    async def test_reconfigure_email_mismatch_aborts(self, hass, bypass_setup_fixture):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="test@example.com",
            data=USER_INPUT,
            title="Mom",
        )
        entry.add_to_hass(hass)

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Mom",
                CONF_EMAIL: "different@example.com",
                CONF_PASSWORD: "pw",
            },
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "unique_id_mismatch"


class TestOptionsFlow:
    async def test_scan_interval_round_trip(self, hass, bypass_setup_fixture):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id="test@example.com",
            data=USER_INPUT,
        )
        entry.add_to_hass(hass)

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"scan_interval_min": 30}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert entry.options == {"scan_interval_min": 30}
