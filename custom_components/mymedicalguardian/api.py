"""Async client for mymedicalguardian.com.

One login produces a Snapshot containing:
  - recent locations (from /managedcare/location-history)
  - battery / signal / steps / charging / device model (from /ajax_handlers/Account/dashboard/)

Both are server-rendered. Some widgets embed JSON literals as JavaScript variables
inside HTML fragments; we extract those by regex rather than executing JS.
"""

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)

BASE = "https://www.mymedicalguardian.com"
UA = "ha-mymedicalguardian/1.0.1"

_CSRF_RE = re.compile(r'name="ci_csrf_token"[^>]*value="([^"]*)"')
_CARD_RE = re.compile(
    r"<strong>([^<]+)</strong>.*?"
    r'<div class="mb-1">\s*([^<]+?)\s*</div>\s*'
    r'<div class="mb-1">\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*</div>\s*'
    r'<div class="text-muted">\s*([^<]+?)\s*</div>',
    re.DOTALL,
)
_WHEN_RE = re.compile(
    r"\b([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) (\d{1,2}):(\d{2}) (AM|PM) ([A-Za-z]{2,4})\b"
)
_BATTERY_RE = re.compile(r"const\s+batteryInfo\s*=\s*(\{[^;]+?\})\s*;", re.DOTALL)
_DEVICE_STATUS_RE = re.compile(r"\bdeviceStatus\s*=\s*(\[.*?\])\s*;", re.DOTALL)
_STEPS_RE = re.compile(r"todaysSteps\s*=\s*JSON\.parse\(\s*'((?:\\.|[^'\\])*)'\s*\)")
_SIGNAL_LABEL_RE = re.compile(r"SIGNAL</h4>\s*<p[^>]*>\s*([A-Za-z]+)\s*</p>", re.DOTALL)
_SIGNAL_ICON_RE = re.compile(r"images/wifi-([a-z]+)\.svg")
_MODEL_RE = re.compile(r'"mg_system_name"\s*:\s*"([^"]+)"')
_SWITCH_ACCOUNTS_RE = re.compile(r'"switch_account"\s*:\s*(\[[^\]]*\])')

# The portal stamps each timestamp with a US timezone abbreviation that already
# encodes the DST state, so map straight to a fixed UTC offset. Re-deriving the
# offset from a zone name + wall time (the old approach) was wrong for zones that
# don't observe DST (Arizona stays MST year-round) and dropped valid abbrevs the
# zone database doesn't key on (AST, ChST, SST).
_TZ_OFFSETS = {
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8,
    "HST": -10,
    "AST": -4, "ADT": -3,
    "ChST": 10, "SST": -11,
}  # fmt: skip

# Emergency event types that must never be masked by a newer routine entry
# (heartbeat / cradle) sharing the same poll window.
_CRITICAL_ALERTS = {"Fall", "SOS"}

# Month lookup so timestamp parsing never depends on the host LC_TIME locale
# (strptime's %b/%p reject 'May'/'AM' under a non-English locale, which would
# silently wipe all location + alert data on non-US hosts).
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip


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
    alerts: tuple[Alert, ...] = ()  # recent deviceStatus events, newest-first
    extra_recipients: int = 0  # care recipients beyond the tracked default

    @property
    def latest_location(self) -> Location | None:
        return self.locations[0] if self.locations else None

    @property
    def latest_alert(self) -> Alert | None:
        return self.alerts[0] if self.alerts else None


def _parse_when(raw: str) -> datetime:
    m = _WHEN_RE.search(raw)
    if not m:
        raise FetchError(f"unrecognized timestamp: {raw!r}")
    mon, day, year, hour, minute, ampm, tz = m.groups()
    if (month := _MONTHS.get(mon)) is None:
        raise FetchError(f"unrecognized month in {raw!r}")
    if (off := _TZ_OFFSETS.get(tz)) is None:
        raise FetchError(f"unknown timezone abbreviation in {raw!r}")
    # The regex matches digit ranges loosely (e.g. hour 13, day 32); validate the
    # 12-hour clock by hand and let the datetime ctor reject impossible days.
    h12, mi = int(hour), int(minute)
    if not (1 <= h12 <= 12 and 0 <= mi <= 59):
        raise FetchError(f"invalid timestamp components in {raw!r}")
    hour24 = h12 % 12 + (12 if ampm == "PM" else 0)
    try:
        naive = datetime(int(year), month, int(day), hour24, mi)
    except ValueError as err:
        raise FetchError(f"invalid timestamp components in {raw!r}") from err
    return naive.replace(tzinfo=timezone(timedelta(hours=off)))


def _parse_utc(raw: str | None) -> datetime | None:
    """Parse the portal's ``time_utc`` as an aware UTC datetime, accepting both
    the second ('2026-05-12 01:31:24') and millisecond ('...14.060') forms the
    portal emits; return None when absent or malformed."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _finite_float(val: Any) -> float | None:
    """Coerce a portal value to a finite float, or None if absent / non-numeric /
    non-finite. json.loads accepts bare Infinity/NaN literals, so the isfinite
    check is what keeps them out of coords, battery, miles, and calories."""
    # bool is an int subclass; float(True) == 1.0 would otherwise slip a JSON
    # boolean through as a real-looking 0.0/1.0 coordinate or battery reading.
    if isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None


def _alert_age_key(ev: dict[str, Any]) -> datetime:
    """Sort key for deviceStatus entries: prefer full-second time_utc, fall back
    to the local time_user_tz, and sort genuinely timeless entries oldest."""
    if (utc := _parse_utc(ev.get("time_utc"))) is not None:
        return utc
    if local := ev.get("time_user_tz"):
        try:
            return _parse_when(local)
        except FetchError:
            pass
    return datetime.min.replace(tzinfo=UTC)


def _build_alert(ev: dict[str, Any]) -> Alert | None:
    """Build an Alert from a deviceStatus entry, or None if it has no usable
    timestamp. A non-numeric coordinate nulls only that field."""
    observed_at = _parse_utc(ev.get("time_utc"))
    if observed_at is None and (raw := ev.get("time_user_tz")):
        try:
            observed_at = _parse_when(raw)
        except FetchError:
            return None
    if observed_at is None:
        return None
    return Alert(
        type=ev.get("type", "Unknown"),
        observed_at=observed_at,
        address=ev.get("address"),
        latitude=_finite_float(ev.get("latitude")),
        longitude=_finite_float(ev.get("longitude")),
    )


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


def _parse_dashboard(payload: Any, device_model: str | None) -> dict[str, Any]:
    """Return kwargs suitable for `Snapshot(**fields, locations=...)`."""
    fields: dict[str, Any] = {"device_model": device_model}
    if not isinstance(payload, dict):
        # A non-object dashboard body (a JSON list, string, or bare null) would
        # otherwise crash on payload.get(...); nothing to parse, return early.
        return fields
    data = payload.get("data")
    if not isinstance(data, dict):
        # A 200 body of {"data": null} reaches here; treat as an empty dashboard.
        data = {}
    # Coerce to str: a present-but-non-string field (null, a number, a list)
    # would otherwise reach re.search() and raise TypeError outside the guards.
    ds = v if isinstance(v := data.get("html_device_status"), str) else ""
    steps_html = v if isinstance(v := data.get("html_step_counting"), str) else ""

    if m := _BATTERY_RE.search(ds):
        try:
            bi = json.loads(m.group(1))
            if isinstance(bi, dict):
                pct = _finite_float(bi.get("battery_percentage"))
                val = int(pct) if pct is not None and 0 <= pct <= 100 else None
                if pct is not None and val is None:
                    _LOGGER.debug("battery_percentage out of range: %s", pct)
                # Only record the reading's timestamp when the reading itself was
                # accepted, so a dropped value doesn't leave a dangling observed_at.
                if val is not None:
                    fields["battery_percent"] = val
                    if t := bi.get("time_user_tz"):
                        fields["battery_observed_at"] = _parse_when(t)
        except (ValueError, TypeError, FetchError) as err:
            _LOGGER.debug("batteryInfo unparseable: %s", err)

    if m := _DEVICE_STATUS_RE.search(ds):
        try:
            parsed = json.loads(m.group(1))
        except ValueError as err:
            _LOGGER.debug("deviceStatus unparseable: %s", err)
            parsed = []
        # Keep only object entries: a stray null/scalar element in an otherwise
        # valid array must not crash the sort or the cradle scan below.
        alerts = [ev for ev in parsed if isinstance(ev, dict)]
        # Don't trust portal ordering — sort newest-first by timestamp so charging
        # reflects the most recent cradle transition and latest_alert is newest.
        alerts.sort(key=_alert_age_key, reverse=True)
        for ev in alerts:
            t = ev.get("type", "")
            if t in {"On Cradle", "Off Cradle"}:
                fields["charging"] = t == "On Cradle"
                break
        # No cradle event in the window ⇒ leave charging None (unknown) rather
        # than asserting a definitive "not charging".
        # Expose the full recent-alerts list (newest-first) so the event entity
        # can fire each genuinely-new event and prioritise Fall/SOS itself,
        # without a single latest_alert slot masking one event with another.
        if built := tuple(a for ev in alerts if (a := _build_alert(ev)) is not None):
            fields["alerts"] = built

    if m := _SIGNAL_LABEL_RE.search(ds):
        fields["signal_label"] = m.group(1).strip()
    if m := _SIGNAL_ICON_RE.search(ds):
        fields["signal_slug"] = m.group(1)

    if m := _STEPS_RE.search(steps_html):
        try:
            steps = json.loads(m.group(1).encode().decode("unicode_escape"))
        except (ValueError, TypeError) as err:
            _LOGGER.debug("todaysSteps unparseable: %s", err)
            steps = None
        if isinstance(steps, dict):
            # Parse each field independently so one bad sub-field (a decimal
            # string, Infinity, NaN) drops only itself, not the whole reading.
            if (s := _finite_float(steps.get("totalSteps"))) is not None:
                fields["steps_today"] = int(s)
            if (mi := _finite_float(steps.get("totalMiles"))) is not None:
                fields["miles_today"] = mi
            if (cal := _finite_float(steps.get("totalCalories"))) is not None:
                fields["calories_today"] = cal

    return fields


async def fetch_snapshot(
    client: httpx.AsyncClient,
    email: str,
    password: str,
) -> Snapshot:
    """Log in once and pull both feeds in a single session.

    Takes an externally-owned, cookie-stateful ``httpx.AsyncClient`` (the
    coordinator builds one with HA's cached ``client_context()`` SSL context and
    ``follow_redirects=True``). Caller owns closing it; we just borrow it.
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
    # Transient upstream failures on the login endpoint are not credential
    # problems — surface them as FetchError so the coordinator retries instead
    # of forcing the user into a reauth flow.
    if rp.status_code == 429 or rp.status_code >= 500:
        raise FetchError(f"login endpoint returned status {rp.status_code}")
    try:
        login = rp.json()
    except json.JSONDecodeError as err:
        # A 200 carrying an HTML maintenance page is a transient portal issue,
        # not a credential rejection — don't force the user into a reauth flow.
        raise FetchError("login XHR returned non-JSON") from err
    if rp.status_code != 200 or not isinstance(login, dict) or login.get("error", True):
        raise AuthError("login rejected — check email/password")

    accounts = await client.get(BASE + "/account-lists", headers=headers)
    model_match = _MODEL_RE.search(accounts.text)
    device_model = model_match.group(1) if model_match else None
    extra_recipients = 0
    if m := _SWITCH_ACCOUNTS_RE.search(accounts.text):
        try:
            extra = json.loads(m.group(1))
            extra_recipients = len(extra) if isinstance(extra, list) else 0
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
    # Check status before parsing: a 4xx/5xx that still carries a JSON body would
    # otherwise yield a "successful" snapshot with every dashboard field None,
    # masking an outage as healthy. Symmetric with the history check below.
    if dash.status_code != 200:
        raise FetchError(f"dashboard XHR returned status {dash.status_code}")
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
        extra_recipients=extra_recipients,
        **_parse_dashboard(dash_json, device_model),
    )
