# Nest Thermostat Monitor — Raspberry Pi 4

> Code in this repository was generated with Muse, Meta's AI assistant,
> and reviewed and tested with mock data and a seeded short-cycling demo
> before use.

Polls your Nest thermostat every 2 minutes via the Google Smart Device
Management (SDM) API, logs temperature / setpoint / mode / HVAC status
locally to SQLite, detects likely faults, and emails you via Gmail when
one is found.

Works for **1 thermostat, heat and cool** (and HEATCOOL mode — the active
setpoint follows the HVAC status).

## What it detects

| Alert | Default rule | Why it matters |
|---|---|---|
| `SHORT_CYCLE` | A completed heat/cool run ≤ **10 min** | 3–5 min runs are a strong fault signal; wears out compressor / igniter |
| `RAPID_CYCLING` | ≥ **4 cycles** started in 60 min | Normal is ~3–4/hr at moderate load; Nest's tight ±1 °F swing can cause this |
| `LONG_RUN_NO_PROGRESS` | Running ≥ **90 min** and moved < **0.5 °F** toward setpoint | Dirty filter, blocked vents, low refrigerant, failing blower/compressor |
| `OFF_NO_PROGRESS` | Mode is HEAT/COOL but HVAC OFF ≥ **120 min** while > **3 °F** from setpoint | Power / C-wire issue, safety lockout, schedule/Eco holding it off |
| `OFFLINE` | **3** consecutive failed SDM polls, or Connectivity = OFFLINE | Pi offline, credentials revoked, Google project issue |

Alerts are deduplicated: at most one email per alert type per **60 min**
(`ALERT_COOLDOWN_MINUTES`). Every poll and every alert is logged locally
even if Gmail is not configured.

## How it works

```
cron (every 2 min) → nest_monitor.py
  → refresh OAuth token → GET SDM device traits
  → parse: ambient temp, setpoint, mode, hvac (HEATING/COOLING/OFF)
  → append to nest_data.db (SQLite, readings table)
  → reconstruct cycles from ON→OFF transitions (last 3 h)
  → if fault → Gmail SMTP alert
```

### Polling resolution — read this

Short cycling is measured from *observed transitions*. With a 2-minute
poll, a cycle length is accurate to about ±2 min — good enough to catch
≤10-min cycles. With a 5-minute poll you will miss or blur very short
cycles. That's why the crontab below uses `*/2`. Polling every 2 min is
720 SDM calls/day, well within normal SDM quotas.

SDM traits used: `Temperature.ambientTemperatureCelsius`,
`ThermostatHvac.status` (`OFF`/`HEATING`/`COOLING`),
`ThermostatMode.mode`, `ThermostatTemperatureSetpoint`
(`heatCelsius`/`coolCelsius`), `Humidity`, `Connectivity`,
`Settings.temperatureScale`. Temps are returned in Celsius and converted
to Fahrenheit for display (`TEMP_UNIT=F`).

## One-time Google setup (about 15 min + $5)

Google requires your own Device Access project. There is no way around
this — the old Works with Nest API is gone.

1. **Register for Device Access** — go to the
   [Device Access Console](https://console.nest.google.com/device-access),
   sign in with the **personal Google account that owns the thermostat**,
   accept the terms and pay the one-time **US $5** fee.
2. **Google Cloud project** — in
   [Google Cloud Console](https://console.cloud.google.com/), create a
   project (e.g. `nest-monitor`). Enable **Smart Device Management API**
   (APIs & Services → Library). Also set the OAuth consent screen to
   **External**, add yourself as a test user, and **Publish app** so it
   doesn't disconnect every 7 days.
3. **OAuth client** — APIs & Services → Credentials → Create →
   OAuth client ID → **Web application** (not Desktop — Desktop
   clients reject the `https://www.google.com` redirect this flow
   uses, with Error 400: redirect_uri_mismatch). Under
   **Authorized redirect URIs**, add exactly:
   `https://www.google.com`
   then Save. Copy the client ID and secret.
   > If you already created a Desktop client, create a new Web
   > application client instead, and use its client ID in step 4.
   > If the Device Access Console won't let you change the client
   > ID on the existing project, create a new Device Access project
   > with the new client ID and use its new Project ID.
4. **Device Access project** — back in the Device Access Console →
   Create project, paste the OAuth client ID from step 3. Copy the
   **Project ID** it assigns — a UUID like
   `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`. This is `SDM_PROJECT_ID`
   (not the Cloud project ID).
5. **Refresh token** — on any machine with a browser:
   ```bash
   python3 get_refresh_token.py --project-id <UUID> \
     --client-id <CLIENT_ID> --client-secret <CLIENT_SECRET>
   ```
   Open the URL it prints, sign in, paste the `code=` back. It prints
   the four `SDM_*` values for `.env`.

> Note: Nest Learning Thermostat 1st/2nd gen were dropped from SDM on
> 2025-10-25. All newer Nest Thermostats (including the 2020 model and
> Learning 3rd/4th gen) are supported.

## Gmail alerts setup

The script sends via Gmail SMTP with an **App Password** (not your normal
password):

1. Google Account → Security → turn on **2-Step Verification**.
2. Security → **App passwords** → create one for "Mail" / your Pi.
   Copy the 16-character password (spaces don't matter).
3. Put it in `.env` as `GMAIL_APP_PASSWORD`, with `GMAIL_USER` as the
   sending address and `ALERT_EMAIL` as the recipient.

## Install on the Raspberry Pi 4

```bash
# copy this folder to the Pi, e.g. /home/pi/nest-monitor
scp -r nest-monitor pi@<pi-ip>:/home/pi/
ssh pi@<pi-ip>

cd ~/nest-monitor
cp config.env.example .env
nano .env          # fill in SDM_* + GMAIL_* (see above)
chmod +x nest_monitor.py get_refresh_token.py

# no pip packages needed — stdlib only. Check Python:
python3 --version  # 3.9+ is fine

# Test without touching Google:
python3 nest_monitor.py --mock-file mock_thermostat.json --verbose
python3 nest_monitor.py --seed-demo        # fake short-cycling hour
python3 nest_monitor.py --mock-file mock_thermostat.json --verbose
# ^ second run should raise SHORT_CYCLE + RAPID_CYCLING (check log + inbox)

# Test Gmail:
python3 nest_monitor.py --test-alert

# Real poll (needs .env filled in):
python3 nest_monitor.py --verbose
```

Inspect the data any time:
```bash
sqlite3 nest_data.db "SELECT ts, ambient_f, setpoint_f, mode, hvac_status FROM readings ORDER BY epoch DESC LIMIT 20;"
sqlite3 nest_data.db "SELECT * FROM alerts ORDER BY epoch DESC LIMIT 20;"
sqlite3 nest_data.db "SELECT * FROM cycles ORDER BY start_epoch DESC LIMIT 20;"
tail -f nest_monitor.log
```

## Cron (every 2 minutes)

```bash
crontab -e
```

Add:
```cron
*/2 * * * * /usr/bin/python3 /home/pi/nest-monitor/nest_monitor.py >> /home/pi/nest-monitor/cron.log 2>&1
```

Check it's running:
```bash
crontab -l
tail -20 ~/nest-monitor/nest_monitor.log
grep -c "Poll OK" ~/nest-monitor/nest_monitor.log
```

## Tuning

All thresholds live in `.env` — no code changes needed:

- `SHORT_CYCLE_MINUTES=10` — lower to 7 if you get false alarms on mild days
- `MAX_CYCLES_PER_HOUR=4` — raise to 5–6 if your system legitimately cycles often
- `LONG_RUN_MINUTES=90`, `LONG_RUN_MIN_PROGRESS_F=0.5`
- `ALERT_COOLDOWN_MINUTES=60` — raise to 240 if you want fewer repeat emails
- `TEMP_UNIT=F` (or `C`)

## Files

- `nest_monitor.py` — the monitor (stdlib only)
- `get_refresh_token.py` — one-time OAuth helper
- `config.env.example` — copy to `.env`
- `mock_thermostat.json` — sample SDM payload for offline testing
- `nest_data.db` — created on first run (SQLite: `readings`, `cycles`, `alerts`, `meta`)
- `nest_monitor.log` — created on first run

## Troubleshooting

- `Missing config: SDM_...` — `.env` isn't in the script folder, or a value is blank.
- `Token refresh failed (400)` — refresh token revoked/expired. Re-run `get_refresh_token.py`. Also check the OAuth app is **Published**, not in Testing.
- `SDM GET ... failed (403)` — SDM API not enabled, or the Device Access project ID is the Cloud project ID by mistake (it must be the UUID).
- `No THERMOSTAT device found` — thermostat is on a different Google account than the OAuth login, or the Device Access project hasn't been granted access.
- No alerts but log shows faults — check `GMAIL_APP_PASSWORD` is an App Password, and that `ALERT_EMAIL` is set. Run `--test-alert`.
- `Failed to send alert email` — Pi has no internet, or Gmail blocked the login. App Passwords avoid most of this.

## Limitations

- Detection is polling-based, not event-based: a cycle shorter than the poll interval can be missed, and durations are estimates.
- The script reads status only — it never changes your thermostat.
- `.env` holds secrets. Keep it on the Pi only (`chmod 600 .env`), never commit it.
