"""End-to-end tests for fetch_snapshot using httpx.MockTransport."""

import httpx
import pytest

from custom_components.mymedicalguardian.api import (
    AuthError,
    FetchError,
    fetch_snapshot,
)

from .fixtures import (
    ACCOUNT_LISTS_HTML,
    DASHBOARD_JSON_FULL,
    HISTORY_PAGE_HTML,
    LOGIN_OK_JSON,
    LOGIN_PAGE_HTML,
)

BASE = "https://www.mymedicalguardian.com"


def _build_transport(
    *,
    login_html: str = LOGIN_PAGE_HTML,
    login_post_body: str = LOGIN_OK_JSON,
    accounts_html: str = ACCOUNT_LISTS_HTML,
    dashboard_payload: object = DASHBOARD_JSON_FULL,
    dashboard_as_json: bool = True,
    history_status: int = 200,
    history_html: str = HISTORY_PAGE_HTML,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BASE + "/":
            return httpx.Response(200, text=login_html)
        if url == BASE + "/ajax_handlers/account/login":
            return httpx.Response(200, text=login_post_body)
        if url == BASE + "/account-lists":
            return httpx.Response(200, text=accounts_html)
        if url == BASE + "/ajax_handlers/Account/dashboard/":
            return (
                httpx.Response(200, json=dashboard_payload)
                if dashboard_as_json
                else httpx.Response(200, text=str(dashboard_payload))
            )
        if url == BASE + "/managedcare/location-history":
            return httpx.Response(history_status, text=history_html)
        return httpx.Response(404, text="unhandled")

    return httpx.MockTransport(handler)


def _make_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, follow_redirects=True)


async def test_full_happy_path():
    async with _make_client(_build_transport()) as c:
        snap = await fetch_snapshot(c, "user", "pass")
    assert snap.device_model == "MGMini-Pearl VZ"
    assert len(snap.locations) == 2
    assert snap.battery_percent == 60
    assert snap.charging is True
    assert snap.signal_label == "Good"
    assert snap.signal_slug == "good"
    assert snap.steps_today == 1929
    assert snap.latest_alert is not None
    assert snap.latest_alert.type == "On Cradle"


async def test_missing_csrf_raises_fetch_error():
    transport = _build_transport(login_html="<html>no csrf field here</html>")
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="CSRF"):
            await fetch_snapshot(c, "user", "pass")


async def test_login_rejected_raises_auth_error():
    transport = _build_transport(login_post_body='{"error":true,"message":"bad creds"}')
    async with _make_client(transport) as c:
        with pytest.raises(AuthError, match="rejected"):
            await fetch_snapshot(c, "user", "pass")


async def test_history_5xx_raises_fetch_error():
    transport = _build_transport(history_status=500, history_html="oops")
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="history page"):
            await fetch_snapshot(c, "user", "pass")


async def test_history_missing_marker_raises_fetch_error():
    transport = _build_transport(history_html="<html>blank</html>")
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="history page"):
            await fetch_snapshot(c, "user", "pass")


async def test_dashboard_non_json_raises_fetch_error():
    transport = _build_transport(
        dashboard_payload="<html>oh no this isn't json</html>",
        dashboard_as_json=False,
    )
    async with _make_client(transport) as c:
        with pytest.raises(FetchError, match="non-JSON"):
            await fetch_snapshot(c, "user", "pass")


async def test_empty_history_returns_empty_locations_no_raise():
    """Brand-new device with no recorded heartbeats: marker present, no <li> cards."""
    empty_history = """
    <html><body>
    <h4>Recent Locations</h4>
    <ul class="timeline"></ul>
    </body></html>
    """
    async with _make_client(_build_transport(history_html=empty_history)) as c:
        snap = await fetch_snapshot(c, "user", "pass")
    assert snap.locations == []
    # Dashboard data still came through
    assert snap.battery_percent == 60
