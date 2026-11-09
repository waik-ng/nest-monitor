# GitHub repository description — nest-monitor

## About description (paste into GitHub > About > Description)

> Monitors a Nest thermostat on a Raspberry Pi via the Google Smart Device Management API. Logs temperature and HVAC status locally, detects short cycling, rapid cycling, and long runs with no progress, and sends Gmail alerts.

Character count: ~230 — well under GitHub's 350-character limit.

## Shorter alternative (if you prefer it tighter)

> Personal Nest thermostat monitor for Raspberry Pi — detects short cycling and other HVAC faults, logs locally, alerts via Gmail.

## Topics / tags (paste into GitHub > About > Topics)

```
nest
nest-thermostat
google-smart-device-management
raspberry-pi
hvac
thermostat
python
cron
sqlite
gmail
short-cycling
```

## README intro (for the repo's README.md — the Pi setup guide in this
## folder already works as the full README; this is just the top section)

# Nest Thermostat Monitor

Polls a Nest thermostat every 2 minutes via the Google Smart Device
Management (SDM) API, logs temperature / setpoint / mode / HVAC status
locally to SQLite on a Raspberry Pi 4, detects likely HVAC faults, and
emails a Gmail alert when one is found.

Works for 1 thermostat, heat and cool (and HEATCOOL mode).

**Detects:** short cycle (≤10 min), rapid cycling (≥4 cycles/hour),
long run with no progress (≥90 min, <0.5°F toward setpoint), HVAC OFF
while far from setpoint, and thermostat offline.

Stdlib-only Python — no pip packages needed. See README.md for the
one-time Google Device Access setup, Gmail App Password setup, and
the crontab line.
