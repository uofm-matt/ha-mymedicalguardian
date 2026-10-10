"""Regression tests for portal-drift, login-status and timestamp edge cases."""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qsl

import httpx
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.dt import utcnow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mymedicalguardian.api import (
    _TZ_OFFSETS,
    UA,
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
        with pytest.raises(AuthError) as err:
            await fetch_snapshot(c, "user", "pass")
    assert str(err.value) == "login rejected \u2014 check email/password"


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


def _recording_client(**kw):
    seen: list[httpx.Request] = []
    inner = _build_transport(**kw)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner.handler(request)

    return _make_client(httpx.MockTransport(handler)), seen


async def test_portal_request_contract():
    # The portal's wire contract: form fields, XHR headers and referers.
    client, seen = _recording_client()
    async with client as c:
        await fetch_snapshot(c, "me@example.com", "pw")

    by_path = {r.url.path: r for r in seen}
    login = by_path["/ajax_handlers/account/login"]
    assert dict(parse_qsl(login.content.decode(), keep_blank_values=True)) == {
        "ci_csrf_token": "",
        "user_name": "me@example.com",
        "password": "pw",
    }
    assert login.headers["X-Requested-With"] == "XMLHttpRequest"
    assert login.headers["Referer"] == "https://www.mymedicalguardian.com/"
    dash = by_path["/ajax_handlers/Account/dashboard/"]
    assert dash.headers["X-Requested-With"] == "XMLHttpRequest"
    assert dash.headers["Referer"] == "https://www.mymedicalguardian.com/dashboard"
    assert {r.headers["User-Agent"] for r in seen} == {UA}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 0), ("100", 100), ("101", None), ("-1", None), ("55.9", 55)],
)
def test_battery_percentage_bounds(raw, expected):
    ds = (
        'const batteryInfo = {"battery_percentage": '
        + raw
        + ', "time_user_tz": "May 12, 2026 10:44 AM EDT"};'
    )
    fields = _parse_dashboard(_drifted(ds + _EMPTY_STATUS), None)
    assert fields.get("battery_percent") == expected
    observed = fields.get("battery_observed_at")
    if expected is None:
        assert observed is None
    else:
        assert observed.utcoffset() == timedelta(hours=-4)
        assert (observed.hour, observed.minute) == (10, 44)


_EMPTY_STATUS = "<script>var deviceStatus = [];</script>"


@pytest.mark.parametrize(
    ("stamp", "valid"),
    [
        ("Jan 15, 2026 01:00 AM EST", True),
        ("Jan 15, 2026 12:59 PM EST", True),
        ("Jan 15, 2026 12:00 AM EST", True),
        ("Jan 15, 2026 12:60 PM EST", False),
        ("Jan 15, 2026 00:30 AM EST", False),
        ("Jan 15, 2026 13:00 PM EST", False),
        ("Feb 30, 2026 10:00 AM EST", False),
    ],
)
def test_parse_when_clock_bounds(stamp, valid):
    if valid:
        _parse_when(stamp)
    else:
        with pytest.raises(FetchError, match="invalid timestamp components"):
            _parse_when(stamp)


def test_parse_when_keeps_minutes_and_midnight():
    d = _parse_when("Jan 15, 2026 12:07 AM EST")
    assert (d.hour, d.minute) == (0, 7)
    assert _parse_when("Jan 15, 2026 12:07 PM EST").hour == 12


def test_parse_when_unknown_month_message():
    with pytest.raises(FetchError, match="unrecognized month"):
        _parse_when("Xyz 15, 2026 10:00 AM EST")


def test_parse_utc_keeps_offset_and_assumes_utc_when_naive():
    from custom_components.mymedicalguardian.api import _parse_utc

    aware = _parse_utc("2026-05-12T01:31:24+02:00")
    assert aware.utcoffset() == timedelta(hours=2)
    assert _parse_utc("2026-05-12 01:31:24").utcoffset() == timedelta(0)


def test_alert_keeps_coordinates_and_nulls_non_finite():
    from custom_components.mymedicalguardian.api import _build_alert

    ev = {
        "type": "Fall",
        "time_utc": "2026-05-12 01:31:24",
        "latitude": "42.5",
        "longitude": "-84.25",
    }
    alert = _build_alert(ev)
    assert (alert.latitude, alert.longitude) == (42.5, -84.25)
    bad = _build_alert({**ev, "latitude": "nan", "longitude": True})
    assert (bad.latitude, bad.longitude) == (None, None)


def test_unescape_js_handles_chars_outside_latin1():
    # A literal snowman cannot be latin-1 encoded; backslashreplace must carry it
    # through as an escape that unicode_escape then restores.
    assert _unescape_js("\u2603") == "\u2603"
    assert _unescape_js(r"\u2603") == "\u2603"


async def test_non_list_switch_account_counts_zero():
    html = 'x "switch_account": [1, 2] y "mg_system_name": "M"'
    async with _make_client(_build_transport(accounts_html=html)) as c:
        assert (await fetch_snapshot(c, "u", "p")).extra_recipients == 2
    html = 'x "switch_account": [bad json] y'
    async with _make_client(_build_transport(accounts_html=html)) as c:
        assert (await fetch_snapshot(c, "u", "p")).extra_recipients == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"login_html": "<html>none</html>"}, "no CSRF field on login page"),
        ({"login_post_body": "<html>x</html>"}, "login XHR returned non-JSON"),
        (
            {"dashboard_payload": "<html>x</html>", "dashboard_as_json": False},
            "dashboard XHR returned non-JSON",
        ),
        (
            {"dashboard_payload": _drifted("<p>x</p>")},
            "dashboard has no deviceStatus block (markup changed?)",
        ),
        (
            {"dashboard_payload": _drifted("var deviceStatus = [oops];")},
            "deviceStatus is not valid JSON",
        ),
    ],
)
async def test_fetch_error_messages_are_specific(kwargs, message):
    async with _make_client(_build_transport(**kwargs)) as c:
        with pytest.raises(FetchError) as err:
            await fetch_snapshot(c, "u", "p")
    assert str(err.value).startswith(message)


async def test_login_rejected_message():
    transport = _build_transport(login_post_body='{"error": true}')
    async with _make_client(transport) as c:
        with pytest.raises(AuthError) as err:
            await fetch_snapshot(c, "u", "p")
    assert str(err.value) == "login rejected \u2014 check email/password"
