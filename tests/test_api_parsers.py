"""Pure-function tests for api.py parsers."""

import pytest

from custom_components.mymedicalguardian.api import (
    FetchError,
    _parse_dashboard,
    _parse_locations,
    _parse_when,
)

from .fixtures import (
    DASHBOARD_JSON_ALERT_BAD_LATLON,
    DASHBOARD_JSON_ALERT_BAD_TIMESTAMP,
    DASHBOARD_JSON_ALERT_INF_COORD,
    DASHBOARD_JSON_ALERT_MILLIS_UTC,
    DASHBOARD_JSON_ALERT_NO_USER_TZ,
    DASHBOARD_JSON_BATTERY_FLOAT_STRING,
    DASHBOARD_JSON_BATTERY_INFINITY,
    DASHBOARD_JSON_BATTERY_OUT_OF_RANGE,
    DASHBOARD_JSON_CRITICAL_MASKED,
    DASHBOARD_JSON_DEVICESTATUS_NULL_ELEM,
    DASHBOARD_JSON_FIELDS_NULL,
    DASHBOARD_JSON_FULL,
    DASHBOARD_JSON_NO_CRADLE,
    DASHBOARD_JSON_OFF_CRADLE,
    DASHBOARD_JSON_SAME_MINUTE_ALERTS,
    DASHBOARD_JSON_STEPS_MIXED,
    DASHBOARD_JSON_STEPS_NONDICT,
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

    def test_invalid_components_raise_fetch_error(self):
        # Regex matches "13:44 AM" but strptime rejects hour 13 — must surface as
        # FetchError, not a raw ValueError that escapes callers' guards.
        with pytest.raises(FetchError, match="invalid timestamp"):
            _parse_when("May 12, 2026 13:44 AM EDT")

    def test_pst_maps_to_los_angeles(self):
        d = _parse_when("Jan 01, 2026 12:00 PM PST")
        assert d.utcoffset().total_seconds() == -8 * 3600

    def test_arizona_mst_keeps_standard_offset_in_summer(self):
        # Arizona stays MST (UTC-7) year-round. The abbreviation already encodes
        # the offset, so a July MST timestamp must not be shifted to MDT.
        d = _parse_when("Jul 01, 2026 12:00 PM MST")
        assert d.utcoffset().total_seconds() == -7 * 3600

    def test_atlantic_abbreviation_resolves(self):
        # AST (Puerto Rico / USVI) is a valid US offset the zone DB doesn't key on.
        d = _parse_when("May 12, 2026 10:44 AM AST")
        assert d.utcoffset().total_seconds() == -4 * 3600

    def test_twelve_hour_boundaries(self):
        assert _parse_when("Jan 01, 2026 12:00 AM EST").hour == 0
        assert _parse_when("Jan 01, 2026 12:00 PM EST").hour == 12
        assert _parse_when("Jan 01, 2026 11:00 PM EST").hour == 23

    def test_all_months_parse(self):
        names = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]  # fmt: skip
        for i, mon in enumerate(names, 1):
            assert _parse_when(f"{mon} 15, 2026 09:00 AM EST").month == i

    def test_parse_when_is_locale_independent(self):
        # The portal stamps English month/AM-PM; parsing must not depend on the
        # host LC_TIME, which would otherwise wipe all timestamps on non-US hosts.
        import locale

        saved = locale.setlocale(locale.LC_TIME)
        try:
            for loc in ("de_DE.UTF-8", "de_DE", "fr_FR.UTF-8", "fr_FR"):
                try:
                    locale.setlocale(locale.LC_TIME, loc)
                    break
                except locale.Error:
                    continue
            else:
                pytest.skip("no non-English LC_TIME locale installed")
            d = _parse_when("May 12, 2026 10:44 AM EDT")
            assert d.month == 5 and d.hour == 10
        finally:
            locale.setlocale(locale.LC_TIME, saved)


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
        assert fields["alerts"][0].type == "On Cradle"  # newest deviceStatus

    def test_battery_float_string_coerced(self):
        fields = _parse_dashboard(
            DASHBOARD_JSON_BATTERY_FLOAT_STRING, device_model=None
        )
        assert fields["battery_percent"] == 60  # int(float("60.5")) → 60

    def test_charging_unknown_with_empty_deviceStatus(self):
        # No cradle event in the window ⇒ charging is unknown (key absent), not
        # a definitive "not charging".
        fields = _parse_dashboard(DASHBOARD_JSON_NO_CRADLE, device_model=None)
        assert "charging" not in fields

    def test_charging_off_when_latest_is_off_cradle(self):
        fields = _parse_dashboard(DASHBOARD_JSON_OFF_CRADLE, device_model=None)
        assert fields["charging"] is False

    def test_empty_payload_returns_only_model(self):
        fields = _parse_dashboard({"data": {}}, device_model="X")
        assert fields == {"device_model": "X"}

    def test_data_null_does_not_crash(self):
        # A 200 body of {"data": null} must not raise AttributeError.
        assert _parse_dashboard({"data": None}, device_model="X") == {
            "device_model": "X"
        }

    def test_battery_out_of_range_dropped(self):
        fields = _parse_dashboard(
            DASHBOARD_JSON_BATTERY_OUT_OF_RANGE, device_model=None
        )
        assert "battery_percent" not in fields
        # No accepted reading ⇒ no dangling observed_at either.
        assert "battery_observed_at" not in fields

    def test_non_dict_payload_does_not_crash(self):
        # dash.json() yielding a list / string / null must not raise.
        assert _parse_dashboard([], device_model="X") == {"device_model": "X"}
        assert _parse_dashboard("nope", device_model="X") == {"device_model": "X"}
        assert _parse_dashboard(None, device_model="X") == {"device_model": "X"}

    def test_non_dict_devicestatus_element_does_not_crash(self):
        # A null element mixed into the array is skipped; the real Fall parses.
        fields = _parse_dashboard(
            DASHBOARD_JSON_DEVICESTATUS_NULL_ELEM, device_model=None
        )
        assert fields["alerts"][0].type == "Fall"

    def test_bad_coordinate_keeps_alert(self):
        # A non-numeric latitude nulls only that coord — the Fall alert survives.
        fields = _parse_dashboard(DASHBOARD_JSON_ALERT_BAD_LATLON, device_model=None)
        alert = fields["alerts"][0]
        assert alert.type == "Fall"
        assert alert.latitude is None
        assert alert.longitude == -84.0

    def test_non_dict_steps_does_not_crash(self):
        fields = _parse_dashboard(DASHBOARD_JSON_STEPS_NONDICT, device_model=None)
        assert "steps_today" not in fields

    def test_all_window_alerts_exposed_newest_first(self):
        # Both a newer Heart Beat and an older Fall reach the alerts list (the
        # event entity, not the parser, prioritises the Fall when firing).
        fields = _parse_dashboard(DASHBOARD_JSON_CRITICAL_MASKED, device_model=None)
        assert [a.type for a in fields["alerts"]] == ["Heart Beat", "Fall"]

    def test_battery_infinity_dropped_not_crash(self):
        fields = _parse_dashboard(DASHBOARD_JSON_BATTERY_INFINITY, device_model=None)
        assert "battery_percent" not in fields

    def test_alert_millisecond_time_utc_keeps_subsecond(self):
        fields = _parse_dashboard(DASHBOARD_JSON_ALERT_MILLIS_UTC, device_model=None)
        assert fields["alerts"][0].observed_at.microsecond == 500_000

    def test_alert_invalid_timestamp_skipped_not_crash(self):
        # An out-of-range time_user_tz with no time_utc drops just that alert.
        fields = _parse_dashboard(DASHBOARD_JSON_ALERT_BAD_TIMESTAMP, device_model=None)
        assert "alerts" not in fields

    def test_present_but_null_html_fields_do_not_crash(self):
        # html_device_status/html_step_counting present with JSON null must not
        # blow up re.search(None).
        assert _parse_dashboard(DASHBOARD_JSON_FIELDS_NULL, device_model="X") == {
            "device_model": "X"
        }

    def test_truthy_non_string_html_fields_do_not_crash(self):
        # A number/list/bool/object is truthy, so `or ""` wouldn't catch it — the
        # isinstance coercion must keep it out of re.search().
        for bad in (123, ["x"], True, {"k": 1}):
            assert _parse_dashboard(
                {"data": {"html_device_status": bad, "html_step_counting": bad}},
                device_model="X",
            ) == {"device_model": "X"}

    def test_non_finite_coordinate_nulled_keeps_alert(self):
        fields = _parse_dashboard(DASHBOARD_JSON_ALERT_INF_COORD, device_model=None)
        alert = fields["alerts"][0]
        assert alert.type == "Fall"
        assert alert.latitude is None  # Infinity rejected
        assert alert.longitude == -84.0

    def test_steps_subfields_independent(self):
        # Decimal-string steps parse; non-finite miles drop; calories survive.
        fields = _parse_dashboard(DASHBOARD_JSON_STEPS_MIXED, device_model=None)
        assert fields["steps_today"] == 1929
        assert "miles_today" not in fields
        assert fields["calories_today"] == pytest.approx(0.41)

    def test_alert_built_from_time_utc_when_user_tz_missing(self):
        fields = _parse_dashboard(DASHBOARD_JSON_ALERT_NO_USER_TZ, device_model=None)
        alert = fields["alerts"][0]
        assert alert.type == "Fall"
        assert alert.observed_at.second == 30  # full-second time_utc preserved

    def test_same_minute_alerts_keep_seconds_and_pick_newest(self):
        fields = _parse_dashboard(DASHBOARD_JSON_SAME_MINUTE_ALERTS, device_model=None)
        # Sorted newest-first by full-second time_utc; both retain their seconds
        # so the two same-minute alerts stay distinct for the event entity.
        assert [a.type for a in fields["alerts"]] == ["SOS", "Fall"]
        assert fields["alerts"][0].observed_at.second == 50
        assert fields["alerts"][1].observed_at.second == 5


def test_alert_with_unparseable_local_time_and_no_utc_is_dropped():
    from custom_components.mymedicalguardian.api import _alert_time, _build_alert

    ev = {"type": "Fall", "time_user_tz": "not a time"}
    assert _alert_time(ev) is None
    assert _build_alert(ev) is None
    assert _alert_time({"type": "Fall"}) is None
