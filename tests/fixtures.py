"""Test data fixtures — minimal HTML/JSON capturing the portal shapes we parse."""

LOGIN_PAGE_HTML = """
<html><body>
<form id="form_login" action="/ajax_handlers/account/login" method="post">
  <input type="hidden" name="ci_csrf_token" value="">
  <input type="text" name="user_name">
  <input type="password" name="password">
  <button type="submit">Sign in</button>
</form>
</body></html>
"""

LOGIN_OK_JSON = '{"message":"Login successful!","error":false,"status":200,"data":{}}'
LOGIN_BAD_JSON = '{"message":"Bad creds","error":true,"status":401}'

ACCOUNT_LISTS_HTML = """
<html><body>
<script>
let mixPanelAccountDetails = {"current_account":{"mg_system_name":"MGMini-Pearl VZ"},"switch_account":[]};
</script>
</body></html>
"""

ACCOUNT_LISTS_MULTI_HTML = ACCOUNT_LISTS_HTML.replace(
    '"switch_account":[]',
    '"switch_account":[{"id":"123","mg_system_name":"MGMini-Pearl VZ"}]',
)

HISTORY_PAGE_HTML = """
<html><body>
<h4>Recent Locations</h4>
<ul class="timeline">
  <li>
    <span class="timeline_span one_digit">1</span>
    <div class="mb-1"><strong>Heart Beat</strong></div>
    <div class="mb-1">123 Main St, Springfield, IL 62701, USA</div>
    <div class="mb-1">39.7817, -89.6501</div>
    <div class="text-muted">May 12, 2026 10:44 AM EDT</div>
  </li>
  <li>
    <span class="timeline_span one_digit">2</span>
    <div class="mb-1"><strong>Heart Beat</strong></div>
    <div class="mb-1">456 Oak Ave, Springfield, IL 62701, USA</div>
    <div class="mb-1">39.7820, -89.6504</div>
    <div class="text-muted">May 12, 2026 09:44 AM EDT</div>
  </li>
</ul>
</body></html>
"""

HISTORY_PAGE_HTML_BAD_ROW = """
<html><body>
<h4>Recent Locations</h4>
<ul class="timeline">
  <li>
    <div class="mb-1"><strong>Heart Beat</strong></div>
    <div class="mb-1">123 Main St</div>
    <div class="mb-1">42.0, -84.0</div>
    <div class="text-muted">May 12, 2026 10:44 AM ZZZ</div>
  </li>
  <li>
    <div class="mb-1"><strong>Heart Beat</strong></div>
    <div class="mb-1">456 Other</div>
    <div class="mb-1">42.1, -84.1</div>
    <div class="text-muted">May 12, 2026 11:44 AM EDT</div>
  </li>
</ul>
</body></html>
"""

# /ajax_handlers/Account/dashboard/ payload — only the fields we actually parse
DASHBOARD_JSON_FULL = {
    "data": {
        "html_device_status": """
            <h4>BATTERY</h4>
            const batteryInfo = {"battery_percentage":"60","time_user_tz":"May 11, 2026 10:44 PM EDT","charging_state":null};
            deviceStatus = [
                {"time_utc":"2026-05-12 01:31:24","type":"On Cradle","address":"123 Main St, Springfield, IL 62701, USA","latitude":"39.7817","longitude":"-89.6501","time_user_tz":"May 11, 2026 09:31 PM EDT"},
                {"time_utc":"2026-05-11 02:00:00","type":"Off Cradle","time_user_tz":"May 10, 2026 10:00 PM EDT"}
            ];
            <img src="https://www.mymedicalguardian.com/assets/images/wifi-good.svg">
            <h4 class="sys-card-head">SIGNAL</h4>
            <p>Good</p>
        """,
        "html_step_counting": (
            "let todaysSteps = JSON.parse('"
            r"{\"totalSteps\":1929,\"totalCalories\":0.41,\"totalMiles\":0.83}"
            "');"
        ),
    }
}

DASHBOARD_JSON_BATTERY_FLOAT_STRING = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"60.5","time_user_tz":"May 11, 2026 10:44 PM EDT"};'
            "deviceStatus = [];"
        ),
        "html_step_counting": "",
    }
}

DASHBOARD_JSON_NO_CRADLE = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"100","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            "deviceStatus = [];"
        ),
        "html_step_counting": "",
    }
}

DASHBOARD_JSON_OFF_CRADLE = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"95","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            'deviceStatus = [{"time_utc":"2026-05-12 13:44:00","type":"Off Cradle","time_user_tz":"May 12, 2026 09:44 AM EDT"}];'
        ),
        "html_step_counting": "",
    }
}

# battery_percentage outside 0..100 must be dropped, not surfaced verbatim.
DASHBOARD_JSON_BATTERY_OUT_OF_RANGE = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"250","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            "deviceStatus = [];"
        ),
        "html_step_counting": "",
    }
}

# A real alert whose entry omits time_user_tz but carries full-second time_utc;
# latest_alert must still be built (falling back to time_utc).
DASHBOARD_JSON_ALERT_NO_USER_TZ = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"80","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            'deviceStatus = [{"time_utc":"2026-05-12 14:44:30","type":"Fall",'
            '"address":"123 Main St","latitude":"42.0","longitude":"-84.0"}];'
        ),
        "html_step_counting": "",
    }
}

# Two distinct alerts in the same wall-clock minute (different seconds); the
# newest by time_utc (SOS at :50) must win and carry second-resolution time.
DASHBOARD_JSON_SAME_MINUTE_ALERTS = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"80","time_user_tz":"May 12, 2026 10:44 AM EDT"};'
            "deviceStatus = ["
            '{"time_utc":"2026-05-12 14:44:05","type":"Fall","time_user_tz":"May 12, 2026 10:44 AM EDT"},'
            '{"time_utc":"2026-05-12 14:44:50","type":"SOS","time_user_tz":"May 12, 2026 10:44 AM EDT"}'
            "];"
        ),
        "html_step_counting": "",
    }
}

# A null element mixed into an otherwise-valid deviceStatus array must not crash
# the sort or cradle scan; the real alert still parses.
DASHBOARD_JSON_DEVICESTATUS_NULL_ELEM = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"70","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            "deviceStatus = [null, "
            '{"time_utc":"2026-05-12 14:44:30","type":"Fall","time_user_tz":"May 12, 2026 10:44 AM EDT"}];'
        ),
        "html_step_counting": "",
    }
}

# A Fall whose latitude is a non-numeric string: the alert must still be built
# (so the event fires), with only the bad coordinate nulled out.
DASHBOARD_JSON_ALERT_BAD_LATLON = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"70","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            'deviceStatus = [{"time_utc":"2026-05-12 14:44:30","type":"Fall",'
            '"address":"123 Main St","latitude":"unknown","longitude":"-84.0"}];'
        ),
        "html_step_counting": "",
    }
}

# todaysSteps that deserializes to a non-dict (a list) must not crash.
DASHBOARD_JSON_STEPS_NONDICT = {
    "data": {
        "html_device_status": "",
        "html_step_counting": "let todaysSteps = JSON.parse('[1,2,3]');",
    }
}

# A Fall masked by a newer routine Heart Beat in the same window: latest_alert
# must still surface the Fall, not the newer heartbeat.
DASHBOARD_JSON_CRITICAL_MASKED = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"70","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            "deviceStatus = ["
            '{"time_utc":"2026-05-12 14:46:00","type":"Heart Beat","time_user_tz":"May 12, 2026 10:46 AM EDT"},'
            '{"time_utc":"2026-05-12 14:44:30","type":"Fall","time_user_tz":"May 12, 2026 10:44 AM EDT"}'
            "];"
        ),
        "html_step_counting": "",
    }
}

# battery_percentage as a JSON Infinity literal (json.loads accepts it) must be
# dropped via OverflowError, not crash the whole poll.
DASHBOARD_JSON_BATTERY_INFINITY = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":Infinity,"time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            "deviceStatus = [];"
        ),
        "html_step_counting": "",
    }
}

# An alert whose time_utc carries milliseconds must keep sub-second resolution.
DASHBOARD_JSON_ALERT_MILLIS_UTC = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"70","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            'deviceStatus = [{"time_utc":"2026-05-12 14:44:30.500","type":"Fall",'
            '"time_user_tz":"May 12, 2026 10:44 AM EDT"}];'
        ),
        "html_step_counting": "",
    }
}

# An alert entry with an out-of-range time_user_tz and no time_utc must be
# skipped (FetchError swallowed), not crash the whole parse.
DASHBOARD_JSON_ALERT_BAD_TIMESTAMP = {
    "data": {
        "html_device_status": (
            'const batteryInfo = {"battery_percentage":"70","time_user_tz":"May 12, 2026 09:44 AM EDT"};'
            'deviceStatus = [{"type":"Fall","time_user_tz":"May 12, 2026 13:44 AM EDT"}];'
        ),
        "html_step_counting": "",
    }
}
