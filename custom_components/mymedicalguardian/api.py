"""Async client for mymedicalguardian.com.

One login produces a Snapshot containing:
  - recent locations (from /managedcare/location-history)
  - battery / signal / steps / charging / device model (from /ajax_handlers/Account/dashboard/)

Both are server-rendered. Some widgets embed JSON literals as JavaScript variables
inside HTML fragments; we extract those by regex rather than executing JS.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_LOGGER = logging.getLogger(__name__)

BASE = "https://www.mymedicalguardian.com"
UA = "ha-mymedicalguardian/1.0.0"

_CSRF_RE = re.compile(r'name="ci_csrf_token"[^>]*value="([^"]*)"')
_CARD_RE = re.compile(
    r"<strong>([^<]+)</strong>.*?"
    r'<div class="mb-1">\s*([^<]+?)\s*</div>\s*'
    r'<div class="mb-1">\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*</div>\s*'
    r'<div class="text-muted">\s*([^<]+?)\s*</div>',
    re.DOTALL,
)
_WHEN_RE = re.compile(
    r"\b([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) (\d{1,2}):(\d{2}) (AM|PM) ([A-Z]{2,4})\b"
)
_BATTERY_RE = re.compile(r"const\s+batteryInfo\s*=\s*(\{[^;]+?\})\s*;", re.DOTALL)
_DEVICE_STATUS_RE = re.compile(r"\bdeviceStatus\s*=\s*(\[.*?\])\s*;", re.DOTALL)
_STEPS_RE = re.compile(r"todaysSteps\s*=\s*JSON\.parse\(\s*'((?:\\.|[^'\\])*)'\s*\)")
_SIGNAL_LABEL_RE = re.compile(r"SIGNAL</h4>\s*<p[^>]*>\s*([A-Za-z]+)\s*</p>", re.DOTALL)
_SIGNAL_ICON_RE = re.compile(r"images/wifi-([a-z]+)\.svg")
_MODEL_RE = re.compile(r'"mg_system_name"\s*:\s*"([^"]+)"')
_SWITCH_ACCOUNTS_RE = re.compile(r'"switch_account"\s*:\s*(\[[^\]]*\])')

_TZ_MAP = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",
}


class AuthError(Exception):
    """Login was rejected."""


class FetchError(Exception):
    """Could not reach or parse a portal page."""


@dataclass(slots=True, frozen=True)
class Location:
    event: str
    address: str
    latitude: float
    longitude: float
    observed_at: datetime
    observed_raw: str


@dataclass(slots=True, frozen=True)
class Alert:
    """A discrete event from the device — Fall / SOS / On Cradle / Off Cradle / etc."""

    type: str
    observed_at: datetime
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(slots=True, frozen=True)
class Snapshot:
    locations: list[Location] = field(default_factory=list)
    battery_percent: int | None = None
    battery_observed_at: datetime | None = None
    charging: bool | None = None
    signal_label: str | None = None  # "Good" / "Fair" / "Poor" / "None"
    signal_slug: str | None = None  # "good" / "fair" / "poor" / "none"
    steps_today: int | None = None
    miles_today: float | None = None
    calories_today: float | None = None
    device_model: str | None = None  # "MGMini-Pearl VZ"
    latest_alert: Alert | None = None

    @property
    def latest_location(self) -> Location | None:
        return self.locations[0] if self.locations else None


def _parse_when(raw: str) -> datetime:
    m = _WHEN_RE.search(raw)
    if not m:
        raise FetchError(f"unrecognized timestamp: {raw!r}")
    mon, day, year, hour, minute, ampm, tz = m.groups()
    naive = datetime.strptime(
        f"{mon} {day} {year} {hour}:{minute} {ampm}", "%b %d %Y %I:%M %p"
    )
    try:
        zone = ZoneInfo(_TZ_MAP.get(tz, tz))
    except ZoneInfoNotFoundError as err:
        raise FetchError(f"unknown timezone abbreviation in {raw!r}") from err
    return naive.replace(tzinfo=zone)


def _parse_locations(html: str) -> list[Location]:
    out: list[Location] = []
    for event, address, lat, lon, when in _CARD_RE.findall(html):
        try:
            out.append(
                Location(
                    event=event.strip(),
                    address=address.strip(),
                    latitude=float(lat),
                    longitude=float(lon),
                    observed_at=_parse_when(when),
                    observed_raw=when.strip(),
                )
            )
        except (ValueError, FetchError) as err:
            _LOGGER.debug("skipping unparseable card (%s): %s", when, err)
    out.sort(key=lambda loc: loc.observed_at, reverse=True)
    return out


def _parse_dashboard(
    payload: dict[str, Any], device_model: str | None
) -> dict[str, Any]:
    """Return kwargs suitable for `Snapshot(**fields, locations=...)`."""
    data = payload.get("data", {})
    ds = data.get("html_device_status", "")
    steps_html = data.get("html_step_counting", "")
    fields: dict[str, Any] = {"device_model": device_model}

    if m := _BATTERY_RE.search(ds):
        try:
            bi = json.loads(m.group(1))
            pct = bi.get("battery_percentage")
            if pct is not None and pct != "":
                fields["battery_percent"] = int(float(pct))
            if t := bi.get("time_user_tz"):
                fields["battery_observed_at"] = _parse_when(t)
        except (ValueError, TypeError, FetchError) as err:
            _LOGGER.debug("batteryInfo unparseable: %s", err)

    if m := _DEVICE_STATUS_RE.search(ds):
        try:
            alerts = json.loads(m.group(1))
            fields["charging"] = False  # default if parsed but no Cradle alerts yet
            for ev in alerts:
                t = ev.get("type", "")
                if t in {"On Cradle", "Off Cradle"}:
                    fields["charging"] = t == "On Cradle"
                    break
            if alerts and (raw := alerts[0].get("time_user_tz")):
                lat = alerts[0].get("latitude")
                lon = alerts[0].get("longitude")
                try:
                    fields["latest_alert"] = Alert(
                        type=alerts[0].get("type", "Unknown"),
                        observed_at=_parse_when(raw),
                        address=alerts[0].get("address"),
                        latitude=float(lat) if lat not in (None, "") else None,
                        longitude=float(lon) if lon not in (None, "") else None,
                    )
                except (ValueError, TypeError, FetchError) as err:
                    _LOGGER.debug("alert parse failed: %s", err)
        except ValueError as err:
            _LOGGER.debug("deviceStatus unparseable: %s", err)

    if m := _SIGNAL_LABEL_RE.search(ds):
        fields["signal_label"] = m.group(1).strip()
    if m := _SIGNAL_ICON_RE.search(ds):
        fields["signal_slug"] = m.group(1)

    if m := _STEPS_RE.search(steps_html):
        try:
            steps = json.loads(m.group(1).encode().decode("unicode_escape"))
            if (s := steps.get("totalSteps")) is not None:
                fields["steps_today"] = int(s)
            if (mi := steps.get("totalMiles")) is not None:
                fields["miles_today"] = float(mi)
            if (cal := steps.get("totalCalories")) is not None:
                fields["calories_today"] = float(cal)
        except (ValueError, TypeError) as err:
            _LOGGER.debug("todaysSteps unparseable: %s", err)

    return fields


async def fetch_snapshot(
    client: httpx.AsyncClient,
    email: str,
    password: str,
) -> Snapshot:
    """Log in once and pull both feeds in a single session.

    Takes an externally-owned ``httpx.AsyncClient`` (the coordinator passes
    one created via HA's ``create_async_httpx_client``). Caller is responsible
    for closing the client; we just borrow it.
    """
    headers = {"User-Agent": UA}
    r = await client.get(BASE + "/", headers=headers)
    r.raise_for_status()
    m = _CSRF_RE.search(r.text)
    if not m:
        raise FetchError("no CSRF field on login page")
    rp = await client.post(
        BASE + "/ajax_handlers/account/login",
        data={"ci_csrf_token": m.group(1), "user_name": email, "password": password},
        headers={
            **headers,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/",
        },
    )
    if rp.status_code != 200 or '"error":false' not in rp.text:
        raise AuthError("login rejected — check email/password")

    accounts = await client.get(BASE + "/account-lists", headers=headers)
    model_match = _MODEL_RE.search(accounts.text)
    device_model = model_match.group(1) if model_match else None
    if m := _SWITCH_ACCOUNTS_RE.search(accounts.text):
        try:
            extra = json.loads(m.group(1))
            if extra:
                _LOGGER.warning(
                    "MyGuardian login has %d additional care recipient(s) beyond the default; "
                    "only the default is currently tracked. Open an issue if multi-account support matters.",
                    len(extra),
                )
        except ValueError as err:
            _LOGGER.debug("switch_account payload unparseable: %s", err)

    dash = await client.get(
        BASE + "/ajax_handlers/Account/dashboard/",
        headers={
            **headers,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE + "/dashboard",
        },
    )
    try:
        dash_json = dash.json()
    except json.JSONDecodeError as err:
        raise FetchError("dashboard XHR returned non-JSON") from err

    hist = await client.get(BASE + "/managedcare/location-history", headers=headers)
    if hist.status_code != 200 or "Recent Locations" not in hist.text:
        raise FetchError(f"history page not rendered (status {hist.status_code})")

    # An empty list is legitimate for a brand-new device that hasn't phoned home
    # yet — let entities expose None until data arrives instead of wedging setup.
    return Snapshot(
        locations=_parse_locations(hist.text),
        **_parse_dashboard(dash_json, device_model),
    )
