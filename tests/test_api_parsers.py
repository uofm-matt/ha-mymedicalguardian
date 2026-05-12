"""Pure-function tests for api.py parsers."""

import pytest

from custom_components.mymedicalguardian.api import (
    FetchError,
    _parse_dashboard,
    _parse_locations,
    _parse_when,
)

from .fixtures import (
    DASHBOARD_JSON_BATTERY_FLOAT_STRING,
    DASHBOARD_JSON_FULL,
    DASHBOARD_JSON_NO_CRADLE,
    DASHBOARD_JSON_OFF_CRADLE,
    HISTORY_PAGE_HTML,
    HISTORY_PAGE_HTML_BAD_ROW,
)


class TestParseWhen:
    def test_known_tz(self):
        d = _parse_when("May 12, 2026 10:44 AM EDT")
        assert d.year == 2026
        assert d.hour == 10
        assert d.utcoffset().total_seconds() == -4 * 3600

    def test_unknown_tz_raises(self):
        with pytest.raises(FetchError, match="unknown timezone"):
            _parse_when("May 12, 2026 10:44 AM XYZ")

    def test_bad_format_raises(self):
        with pytest.raises(FetchError, match="unrecognized timestamp"):
            _parse_when("not a date")

    def test_pst_maps_to_los_angeles(self):
        d = _parse_when("Jan 01, 2026 12:00 PM PST")
        assert d.utcoffset().total_seconds() == -8 * 3600


class TestParseLocations:
    def test_well_formed_html_returns_sorted_newest_first(self):
        locs = _parse_locations(HISTORY_PAGE_HTML)
        assert len(locs) == 2
        # Newest first
        assert locs[0].observed_raw == "May 12, 2026 10:44 AM EDT"
        assert locs[1].observed_raw == "May 12, 2026 09:44 AM EDT"

    def test_extracts_event_address_coords(self):
        loc = _parse_locations(HISTORY_PAGE_HTML)[0]
        assert loc.event == "Heart Beat"
        assert loc.address == "123 Main St, Springfield, IL 62701, USA"
        assert loc.latitude == pytest.approx(39.7817)
        assert loc.longitude == pytest.approx(-89.6501)

    def test_skips_bad_row_keeps_good(self):
        """A single malformed timestamp must not crash the whole snapshot."""
        locs = _parse_locations(HISTORY_PAGE_HTML_BAD_ROW)
        assert len(locs) == 1
        assert locs[0].observed_raw == "May 12, 2026 11:44 AM EDT"

    def test_empty_html_returns_empty_list(self):
        assert _parse_locations("<html></html>") == []


class TestParseDashboard:
    def test_full_parse(self):
        fields = _parse_dashboard(DASHBOARD_JSON_FULL, device_model="MGMini-Pearl VZ")
        assert fields["device_model"] == "MGMini-Pearl VZ"
        assert fields["battery_percent"] == 60
        assert fields["charging"] is True  # latest deviceStatus is On Cradle
        assert fields["signal_label"] == "Good"
        assert fields["signal_slug"] == "good"
        assert fields["steps_today"] == 1929
        assert fields["miles_today"] == pytest.approx(0.83)
        assert fields["calories_today"] == pytest.approx(0.41)
        assert fields["latest_alert"] is not None
        assert fields["latest_alert"].type == "On Cradle"

    def test_battery_float_string_coerced(self):
        fields = _parse_dashboard(
            DASHBOARD_JSON_BATTERY_FLOAT_STRING, device_model=None
        )
        assert fields["battery_percent"] == 60  # int(float("60.5")) → 60

    def test_charging_default_false_with_empty_deviceStatus(self):
        fields = _parse_dashboard(DASHBOARD_JSON_NO_CRADLE, device_model=None)
        assert fields["charging"] is False

    def test_charging_off_when_latest_is_off_cradle(self):
        fields = _parse_dashboard(DASHBOARD_JSON_OFF_CRADLE, device_model=None)
        assert fields["charging"] is False

    def test_empty_payload_returns_only_model(self):
        fields = _parse_dashboard({"data": {}}, device_model="X")
        assert fields == {"device_model": "X"}
