# Changelog

Newest first, one entry per change: what changed, then why.

## 2026-10-10 (after 1.0.2)

- CI tests run a matrix: Home Assistant 2024.12 (0.13.195) and 2025.1 (0.13.205) on Python 3.12, plus a non-blocking latest on Python 3.14 — the 84 tests had only ever run on one two-year-old stack.
- Test pins move to pytest-homeassistant-custom-component 0.13.205 (HA 2025.1.4, pytest 8.3.4); Dependabot's separate pytest, pytest-asyncio and pytest-cov bumps conflict with that package's pins, so they were closed.
- Actions: checkout v7, setup-python v7, setup-uv v7 (Dependabot) — current majors.

## 1.0.2 — 2026-10-10

Upgrade notes:

- If the portal ever returns a dashboard with no `deviceStatus` block, the integration now reports a failed update (and a Repair issue after 24 h) instead of silently showing no alerts. If you see "dashboard has no deviceStatus block" in the log, please open an issue with the diagnostics download.
- The first restart after upgrading from 1.0.1 has no stored list of fired alerts, so a Fall the portal posts late with an older timestamp can still be missed on that one restart. Later restarts keep the list.
- Fall/SOS detection now matches labels containing the whole word `fall` or `sos`. Labels that also contain cancel, clear, dismiss, resolve, false, test or disabled are treated as non-critical.

Changes:

- Drop `httpx>=0.27` from the manifest `requirements` — Hassfest rejects it because Home Assistant ships httpx itself, and the scheduled Validate run had been red since at least 2026-10-08.

Fixes from an internal code-review audit of 1.0.1.

### Fall / SOS safety

- Event entity tracks fired alerts in a bounded set keyed `(type, observed_at)` instead of one high-water mark — a Fall posted late with an older timestamp than an already-fired Heart Beat was never fired (audit High 1).
- Every unfired Fall/SOS fires oldest-first in the same poll, with a state write per alert — the second emergency used to wait one scan interval, up to 120 min (High 1).
- Persist `fired_through` (clamped to now) and the 16 newest fired `(type, observed_at)` keys in the event attributes. On restart the fired set is restored and the baseline looks back 3 h past the mark, so a Fall posted late after a restart still fires, already-fired alerts do not replay, and a clock-skewed future stamp cannot block real alerts. Events persisted by 1.0.1 or earlier have no fired list and keep the old baseline, so a late Fall that first appears after the upgrade restart can still be missed.
- Critical labels match case-insensitively as whole words (`fall detected` fires as `Fall`); labels containing cancel, clear, dismiss, resolve, false, test or disabled (`SOS Cancelled`, `False SOS`) are never critical. Raw label stays in `alert_type` — only three labels were ever verified against the portal (Medium 5).
- Dashboard with a non-empty `html_device_status` but no parseable `deviceStatus` now raises `FetchError`, so it counts toward the portal-unreachable repair instead of silently disabling alerts (High 2).
- Duplicate rows with the same type and timestamp fire once.
- A 401 login response is a credential rejection even when its body is not JSON.
- `deviceStatus` is decoded with `raw_decode`, so a `];` inside an address no longer truncates the array; a non-string `time_user_tz` drops only that alert.
- Tests: SOS-before-newer-routine, late Fall, future-stamped routine, restart with late Fall, drift variants (High 3).

### Parser and login

- Login 400/403/404 with a JSON body is a transient `FetchError`; only 401 or a 200 with `error` is `AuthError`, so a portal hiccup no longer stops polling until reauth (Medium 4).
- Location cards are parsed per `<strong>` chunk so a card missing a field cannot borrow the next card's address, coordinates and time; also removes the quadratic scan (Medium 8).
- Non-string alert `type` becomes `Unknown` instead of raising `TypeError` outside the coordinator's handler (Low 16).
- Steps JSON decode keeps non-ASCII text intact (`Zoë` no longer becomes mojibake) (Low 13).
- Single `_alert_time` helper replaces two copies of the timestamp-fallback logic (Low 12).

### Structure and housekeeping

- Shared `MGEntity` base in `entity.py` replaces two copied `_Base` classes and two repeated constructors; entity and unique ids unchanged (Low 11).
- `_validate` drops its unused `hass` parameter; `async_get_options_flow` names its parameter `config_entry`; event.py no longer imports a private name from api.py (Low 14).
- pyproject version 1.0.1 to match manifest and UA, with a test pinning UA to the manifest; bug-report placeholder updated (Low 15).
- README: diagnostics no longer "safe to attach" (contradicted Privacy); drift and label behavior described as they now are (Medium 9, Low 17).
- Diagnostics test populates a location and alert and asserts address/latitude/longitude redaction (Medium 9).
- Tests for every `_TZ_OFFSETS` key, login status boundaries, reauth after `AuthError`, and the 24 h repair threshold (Medium 10).
- CI: ruff covers `tests/`, ruff and mypy pinned, mypy job installs the pinned test stack, test deps pinned to match pyproject, coverage floor 90, dependabot for actions and pip (Medium 6, 7).
