# Changelog

Newest first, one entry per change: what changed, then why.

## 1.0.3 — 2026-10-10

Upgrade notes:

- If you copied the example emergency automation from the README, add `mode: queued` to it. Without it, two emergencies seen in one poll send one notification.
- The first restart after upgrading from 1.0.2 can repeat one alert that was already handled. Later restarts cannot.

Changes:

- Fix: a restart could re-fire a Fall or SOS the integration had already handled. After a restart 1.0.2 looked back 3 hours and fired anything not in its stored fired list, but that list says nothing about alerts from before the previous session's baseline (fired under 1.0.1, or older than the install). It bit when Home Assistant restarted within about 3 hours of an install or of the upgrade. The entity now stores a floor (`fired_floor`, the time from which the list is complete) and never looks back past it. A state stored by 1.0.2 has no floor, and from it a late Fall and an already handled one look the same; a missed Fall costs more than a repeated one, so the one restart that reads 1.0.2's state keeps the full lookback and can repeat an alert once. Every later restart is covered by the floor. Found while reading the code for the architecture plan, reproduced with failing tests first.
- The stored list now holds every fired alert inside the 3 hour lookback (up to 64) and nothing older, since a restart never looks further back. In normal use that is a handful of entries, fewer than the fixed 16 that 1.0.2 stored.
- README: the example emergency automation now sets `mode: queued`. Since 1.0.2 two emergencies in one poll fire in the same instant, and Home Assistant's default `single` mode skips the second run while the first notification is still sending (measured: one of two delivered). If you copied the example, add `mode: queued`.
- Restore values that are not strings (a hand-edited or corrupt restore file) are ignored, and a stamp without a UTC offset is read as UTC, instead of raising and leaving the entity unloaded.
- Known limits, unchanged by this fix: if Home Assistant restarts before the entity has ever fired an event, nothing is stored and monitoring begins again at that restart, so a Fall stamped before it and posted after it is not fired. If more than 64 alerts fire within 3 hours and Home Assistant then restarts, the oldest ones, which did not fit in the stored list, fire again after each restart while they stay inside that window; the list is not trusted past what it holds, because that would hide a Fall posted late from before them.
- Version 1.0.3 in the manifest, user agent, pyproject and the bug-report placeholder — the replay fix ships on its own so nobody waits on the architecture work for it.
- Dependabot ignores pytest, pytest-asyncio and pytest-cov, and holds pytest-homeassistant-custom-component at 0.13.205 — that package pins the other three exactly, and its later releases need Python 3.13, so every one of those PRs fails to resolve.
- Known gap: nothing in CI tests a Home Assistant newer than 2025.1. The non-blocking `HA latest` job installs the Python 3.12 pins before the newest test package, so it has failed at install on every run since it was added and has never run a test. Found when this release's run was read job by job; not fixed here.
- A sixth mutation sentinel pins the restore floor (`max(lookback, min(floor, mark))`) in `event.py`, and mutmut copies `README.md` into its working tree because a test now reads the example automation from it.
- `.gitignore` ignores `.carburize-review-token` — the repo is public and the per-repo review token must never be committed.
- Mutation gate for `api.py` (`tests/mutation_triage.toml`, `[tool.mutmut]`): 438 of 466 mutants killed, the other 28 triaged as equivalent, plus 5 hand-written sentinels over `api.py` and `event.py`. `event.py` cannot be mutated by mutmut because every mutant segfaults under its fork with the Home Assistant fixtures, so its load-bearing lines are pinned by sentinels.
- The gate found `_CRITICAL_ALERTS` had become vestigial after the whole-word rewrite (the regex hard-coded `fall|sos`); the regex is now built from the set, so the set is the single source.
- New tests pin the portal wire contract (login form fields, XHR headers, user agent), battery bounds and timestamp, clock-time bounds, coordinates on alerts, and exact error messages.
- `--cov-fail-under=90` moved from pytest `addopts` to the CI command, so a partial test selection (mutation runs) no longer fails on coverage.
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
