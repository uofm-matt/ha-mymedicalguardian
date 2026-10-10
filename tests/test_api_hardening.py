"""Regression tests for portal-drift, login-status and timestamp edge cases."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import (
    _TZ_OFFSETS,
    AuthError,
    FetchError,
    _parse_dashboard,
    _parse_locations,
    _parse_when,
    _unescape_js,
    critical_type,
    fetch_snapshot,
)
from custom_components.mymedicalguardian.const import DOMAIN
from custom_components.mymedicalguardian.coordinator import PORTAL_UNREACHABLE_AFTER

from .fixtures import DASHBOARD_JSON_FULL
from .test_api_fetch import _build_transport, _make_client
from .test_coordinator import _make_coordinator

_DS_OK = DASHBOARD_JSON_FULL["data"]["html_device_status"]
_DS_RENAMED = _DS_OK.replace("deviceStatus =", "deviceStatuses =")
_DS_UNQUOTED = _DS_OK.replace('"time_utc":', "time_utc:")


def _drifted(ds: str) -> dict:
    return {"data": {"html_device_status": ds, "html_step_counting": ""}}


@pytest.mark.parametrize("ds", [_DS_RENAMED, _DS_UNQUOTED], ids=["renamed", "unquoted"])
async def test_drifted_device_status_raises_fetch_error(ds):
    async with _make_client(_build_transport(dashboard_payload=_drifted(ds))) as c:
        with pytest.raises(FetchError):
            await fetch_snapshot(c, "user", "pass")


def test_fragment_without_device_status_raises():
    with pytest.raises(FetchError):
        _parse_dashboard(_drifted("<p>no script here</p>"), None)


def test_empty_device_status_fragment_is_not_drift():
    assert "alerts" not in _parse_dashboard(_drifted(""), None)


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_login_4xx_with_json_body_is_transient_not_auth(status):
    transport = _build_transport(
        login_status=status, login_post_body='{"error": true, "message": "nope"}'
    )
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="login endpoint"):
            await fetch_snapshot(c, "user", "pass")


async def test_login_401_is_auth_error():
    transport = _build_transport(login_status=401, login_post_body="{}")
    async with _make_client(transport) as c:
        with pytest.raises(AuthError):
            await fetch_snapshot(c, "user", "pass")


async def test_login_401_with_html_body_is_auth_error():
    transport = _build_transport(login_status=401, login_post_body="<html>no</html>")
    async with _make_client(transport) as c:
        with pytest.raises(AuthError):
            await fetch_snapshot(c, "user", "pass")


async def test_login_429_is_fetch_error():
    transport = _build_transport(login_status=429, login_post_body="slow down")
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="429"):
            await fetch_snapshot(c, "user", "pass")


async def test_login_500_boundary_is_fetch_error_and_499_is_not():
    transport = _build_transport(login_status=500, login_post_body="{}")
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="500"):
            await fetch_snapshot(c, "user", "pass")
    transport = _build_transport(login_status=499, login_post_body="{}")
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="499"):
            await fetch_snapshot(c, "user", "pass")


async def test_login_200_without_error_key_is_rejected():
    transport = _build_transport(login_post_body='{"message": "hmm"}')
    async with _make_client(transport) as c:
        with pytest.raises(AuthError):
            await fetch_snapshot(c, "user", "pass")


@pytest.mark.parametrize(("abbrev", "hours"), sorted(_TZ_OFFSETS.items()))
def test_every_tz_abbreviation_maps_to_its_offset(abbrev, hours):
    when = _parse_when(f"Jan 15, 2026 10:00 AM {abbrev}")
    assert when.utcoffset() == timedelta(hours=hours)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Fall", "Fall"),
        ("SOS", "SOS"),
        ("fall detected", "Fall"),
        ("SOS Pressed", "SOS"),
        ("Heart Beat", None),
        ("On Cradle", None),
        (["Fall"], None),
    ],
)
def test_critical_type(label, expected):
    assert critical_type(label) == expected


def test_non_string_alert_type_does_not_raise():
    ds = (
        "<script>var deviceStatus = "
        + json.dumps([{"type": ["Fall"], "time_utc": "2026-05-12 01:31:24"}])
        + ";</script>"
    )
    fields = _parse_dashboard(_drifted(ds), None)
    assert fields["alerts"][0].type == "Unknown"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Zo\u00eb", "Zo\u00eb"),  # escaped in the JS source
        ("Zoë", "Zoë"),  # literal non-ASCII character in the JS source
        ("a\\x41\\n", "aA\n"),
    ],
)
def test_unescape_js_keeps_non_ascii_text_intact(raw, expected):
    # A utf-8 encode before unicode_escape turned a literal "Zoë" into mojibake.
    assert _unescape_js(raw) == expected


def test_steps_with_literal_non_ascii_still_parse():
    payload = {
        "data": {
            "html_device_status": "",
            "html_step_counting": (
                'todaysSteps = JSON.parse(\'{\\"totalSteps\\": 12, '
                '\\"name\\": \\"Zoë\\"}\');'
            ),
        }
    }
    assert _parse_dashboard(payload, None)["steps_today"] == 12


def test_non_string_time_user_tz_drops_only_that_alert():
    ds = (
        "<script>var deviceStatus = "
        + json.dumps(
            [
                {"type": "Fall", "time_user_tz": ["x"]},
                {"type": "SOS", "time_utc": "2026-05-12 01:31:24"},
            ]
        )
        + ";</script>"
    )
    alerts = _parse_dashboard(_drifted(ds), None)["alerts"]
    assert [a.type for a in alerts] == ["SOS"]


def test_device_status_with_bracket_semicolon_in_address_still_parses():
    ds = (
        "<script>var deviceStatus = "
        + json.dumps(
            [
                {
                    "type": "Fall",
                    "address": "Apt [2]; Main St",
                    "time_utc": "2026-05-12 01:31:24",
                }
            ]
        )
        + ";</script>"
    )
    alerts = _parse_dashboard(_drifted(ds), None)["alerts"]
    assert alerts[0].address == "Apt [2]; Main St"


@pytest.mark.parametrize(
    "label",
    [
        "SOS Cancelled",
        "Fall Cleared",
        "Fall Dismissed",
        "Waterfall",
        "Manual Test",
        "Fall Detection Disabled",
        "False SOS",
    ],
)
def test_cancel_and_lookalike_labels_are_not_critical(label):
    assert critical_type(label) is None


async def _setup_with_failure(hass, side_effect):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={CONF_NAME: "Mom", CONF_EMAIL: "test@example.com", CONF_PASSWORD: "x"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.mymedicalguardian.coordinator.fetch_snapshot",
        AsyncMock(side_effect=side_effect),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_auth_error_starts_reauth(hass):
    entry = await _setup_with_failure(hass, AuthError("rejected"))
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_repair_issue_threshold_is_exact(hass):
    coord = await _make_coordinator(hass)
    registry = ir.async_get(hass)
    for age, expected in [
        (PORTAL_UNREACHABLE_AFTER - timedelta(seconds=1), False),
        (PORTAL_UNREACHABLE_AFTER + timedelta(seconds=1), True),
    ]:
        coord._first_failure_at = utcnow() - age
        coord._record_failure("down")
        assert (
            registry.async_get_issue(DOMAIN, coord._issue_id) is not None
        ) is expected


def test_card_missing_coordinates_does_not_borrow_next_card():
    html = (
        '<strong>Fall</strong><div class="mb-1">1 Elm St</div>'
        '<div class="text-muted">May 12, 2026 09:50 AM EDT</div>'
        '<strong>Heart Beat</strong><div class="mb-1">456 Oak</div>'
        '<div class="mb-1">39.78, -89.65</div>'
        '<div class="text-muted">May 12, 2026 09:44 AM EDT</div>'
    )
    locs = _parse_locations(html)
    assert [(loc.event, loc.address) for loc in locs] == [("Heart Beat", "456 Oak")]


def test_user_agent_version_matches_manifest():
    from pathlib import Path

    from custom_components.mymedicalguardian.api import UA

    manifest = Path("custom_components/mymedicalguardian/manifest.json")
    assert UA.endswith("/" + json.loads(manifest.read_text())["version"])
