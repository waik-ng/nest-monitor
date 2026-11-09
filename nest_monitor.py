#!/usr/bin/env python3
"""
Nest Thermostat monitor — Raspberry Pi 4, cron-driven.

Generated with Muse, Meta's AI assistant.

Polls one Nest thermostat via the Google Smart Device Management (SDM) API,
logs every poll locally to SQLite, reconstructs HVAC cycles from the
HEATING / COOLING / OFF status, and emails a Gmail alert when it sees a
likely fault:

  * SHORT_CYCLE       — a completed heat/cool run <= SHORT_CYCLE_MINUTES
                        (default 10 min). A 3-5 min run is a strong signal.
  * RAPID_CYCLING     — >= MAX_CYCLES_PER_HOUR cycles started in 60 min
                        (default 4). Normal is ~3-4 cycles/hour at 50% load.
  * LONG_RUN_NO_PROGRESS — running >= LONG_RUN_MINUTES (default 90) without
                        moving at least 0.5 F toward the setpoint.
  * NO_PROGRESS_OFF  — mode is HEAT/COOL/HEATCOOL but HVAC has been OFF for
                        >= 120 min while room temp is >3 F from setpoint.
  * OFFLINE / API_ERROR — 3 consecutive failed SDM polls, or the
                        Connectivity trait reports OFFLINE.

Works for both HEAT and COOL (and HEATCOOL): the active setpoint is picked
from the HVAC status — heatCelsius when HEATING, coolCelsius when COOLING.

Polling resolution matters: with cron every 2 min, a cycle duration is
estimated to within ~2 min. A 5-min cron will miss or blur very short
cycles. Recommended crontab: */2 * * * *

Stdlib only — no pip packages needed on the Pi. Python 3.9+.

Config: copy config.env.example to .env in the same folder (or export the
same names as environment variables). Never commit .env — it holds secrets.

Usage:
  python3 nest_monitor.py                    # normal cron run
  python3 nest_monitor.py --mock-file mock.json
  python3 nest_monitor.py --test-alert       # send a test Gmail alert
  python3 nest_monitor.py --seed-demo        # insert a fake short-cycling
                                             # hour into the DB to test alerts
  python3 nest_monitor.py --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------- config ---

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "nest_data.db"
DEFAULT_LOG = BASE_DIR / "nest_monitor.log"

SDM_BASE = "https://smartdevicemanagement.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SDM_SCOPE = "https://www.googleapis.com/auth/sdm.service"


def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ (no dotenv dep)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except ValueError:
        return default


class Config:
    def __init__(self) -> None:
        self.project_id = _env("SDM_PROJECT_ID")
        self.client_id = _env("SDM_CLIENT_ID")
        self.client_secret = _env("SDM_CLIENT_SECRET")
        self.refresh_token = _env("SDM_REFRESH_TOKEN")
        self.device_id = _env("SDM_DEVICE_ID")  # blank = auto-discover first thermostat
        self.device_name = _env("DEVICE_NAME", "Nest Thermostat")

        self.gmail_user = _env("GMAIL_USER")
        self.gmail_app_password = _env("GMAIL_APP_PASSWORD")
        self.alert_email = _env("ALERT_EMAIL") or self.gmail_user

        self.temp_unit = _env("TEMP_UNIT", "F").upper()  # F or C
        self.db_path = Path(_env("DB_PATH") or str(DEFAULT_DB))
        self.log_path = Path(_env("LOG_PATH") or str(DEFAULT_LOG))

        # Detection thresholds — tune these, don't hard-code them in the logic.
        self.short_cycle_minutes = _env_float("SHORT_CYCLE_MINUTES", 10)
        self.max_cycles_per_hour = _env_int("MAX_CYCLES_PER_HOUR", 4)
        self.long_run_minutes = _env_float("LONG_RUN_MINUTES", 90)
        self.long_run_min_progress_f = _env_float("LONG_RUN_MIN_PROGRESS_F", 0.5)
        self.off_no_progress_minutes = _env_float("OFF_NO_PROGRESS_MINUTES", 120)
        self.off_temp_gap_f = _env_float("OFF_TEMP_GAP_F", 3.0)
        self.alert_cooldown_minutes = _env_float("ALERT_COOLDOWN_MINUTES", 60)
        self.offline_after_failures = _env_int("OFFLINE_AFTER_FAILURES", 3)

    def validate(self, need_sdm: bool = True) -> list[str]:
        missing = []
        if need_sdm:
            mapping = {"SDM_PROJECT_ID": "project_id",
                       "SDM_CLIENT_ID": "client_id",
                       "SDM_CLIENT_SECRET": "client_secret",
                       "SDM_REFRESH_TOKEN": "refresh_token"}
            for env_name, attr in mapping.items():
                if not getattr(self, attr):
                    missing.append(env_name)
        return missing


# ---------------------------------------------------------------- logging ---

def setup_logging(log_path: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("nest_monitor")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        logger.warning("Could not open log file %s: %s", log_path, exc)
    return logger


# ------------------------------------------------------------------ SDM ----

def get_access_token(cfg: Config) -> str:
    """Exchange the long-lived refresh token for a 1-hour access token."""
    data = urllib.parse.urlencode({
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "refresh_token": cfg.refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Token refresh failed ({exc.code}): {body}") from exc
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Token refresh returned no access_token: {payload}")
    return token


def _sdm_get(cfg: Config, token: str, path: str) -> dict:
    url = f"{SDM_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"SDM GET {path} failed ({exc.code}): {body}") from exc


def discover_thermostat_id(cfg: Config, token: str) -> str:
    """List devices, return the first THERMOSTAT device id."""
    payload = _sdm_get(cfg, token, f"/enterprises/{cfg.project_id}/devices")
    for dev in payload.get("devices", []):
        if dev.get("type") == "sdm.devices.types.THERMOSTAT":
            name = dev.get("name", "")
            return name.split("/")[-1]
    raise RuntimeError("No THERMOSTAT device found in SDM project. "
                       "Check the thermostat is linked to the same Google account.")


def fetch_thermostat(cfg: Config, token: str) -> dict:
    device_id = cfg.device_id or discover_thermostat_id(cfg, token)
    return _sdm_get(cfg, token,
                    f"/enterprises/{cfg.project_id}/devices/{device_id}")


def parse_thermostat(payload: dict) -> dict:
    """Flatten SDM traits into a plain reading dict (temps in C and F)."""
    traits = payload.get("traits", {})
    temp = traits.get("sdm.devices.traits.Temperature", {})
    hvac = traits.get("sdm.devices.traits.ThermostatHvac", {})
    mode = traits.get("sdm.devices.traits.ThermostatMode", {})
    setpoint = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
    eco = traits.get("sdm.devices.traits.ThermostatEco", {})
    humidity = traits.get("sdm.devices.traits.Humidity", {})
    connectivity = traits.get("sdm.devices.traits.Connectivity", {})
    settings = traits.get("sdm.devices.traits.Settings", {})
    info = payload.get("traits", {}).get("sdm.devices.traits.Info", {})

    ambient_c = temp.get("ambientTemperatureCelsius")
    hvac_status = hvac.get("status", "UNKNOWN")  # OFF / HEATING / COOLING
    thermostat_mode = mode.get("mode", "UNKNOWN")  # HEAT / COOL / HEATCOOL / OFF

    # Active setpoint follows what the system is actually doing.
    if hvac_status == "HEATING":
        setpoint_c = setpoint.get("heatCelsius")
    elif hvac_status == "COOLING":
        setpoint_c = setpoint.get("coolCelsius")
    elif thermostat_mode == "HEAT":
        setpoint_c = setpoint.get("heatCelsius")
    elif thermostat_mode == "COOL":
        setpoint_c = setpoint.get("coolCelsius")
    else:
        setpoint_c = setpoint.get("heatCelsius") or setpoint.get("coolCelsius")

    def c_to_f(c):
        return round(c * 9 / 5 + 32, 1) if c is not None else None

    return {
        "ambient_c": ambient_c,
        "ambient_f": c_to_f(ambient_c),
        "setpoint_c": setpoint_c,
        "setpoint_f": c_to_f(setpoint_c),
        "mode": thermostat_mode,
        "hvac_status": hvac_status,
        "eco_mode": eco.get("mode"),
        "humidity": humidity.get("ambientHumidityPercent"),
        "connectivity": connectivity.get("status"),
        "temperature_scale": settings.get("temperatureScale"),
        "device_name": info.get("customName") or None,
        "raw": payload,
    }


# ------------------------------------------------------------------ DB -----

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,               -- ISO-8601 UTC
            epoch REAL NOT NULL,
            ambient_c REAL, ambient_f REAL,
            setpoint_c REAL, setpoint_f REAL,
            mode TEXT, hvac_status TEXT,
            eco_mode TEXT, humidity REAL,
            connectivity TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts TEXT NOT NULL, end_ts TEXT,
            start_epoch REAL NOT NULL, end_epoch REAL,
            duration_min REAL,
            hvac_status TEXT NOT NULL,      -- HEATING or COOLING
            mode TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            message TEXT NOT NULL,
            ts TEXT NOT NULL,
            epoch REAL NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT
        )""")
    conn.commit()
    return conn


def store_reading(conn: sqlite3.Connection, r: dict, now: datetime) -> None:
    conn.execute(
        "INSERT INTO readings (ts, epoch, ambient_c, ambient_f, setpoint_c,"
        " setpoint_f, mode, hvac_status, eco_mode, humidity, connectivity)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), now.timestamp(), r.get("ambient_c"), r.get("ambient_f"),
         r.get("setpoint_c"), r.get("setpoint_f"), r.get("mode"),
         r.get("hvac_status"), r.get("eco_mode"), r.get("humidity"),
         r.get("connectivity")))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "0") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                 (key, value))
    conn.commit()


def recent_readings(conn: sqlite3.Connection, hours: float = 3) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    rows = conn.execute(
        "SELECT ts, epoch, ambient_c, ambient_f, setpoint_c, setpoint_f,"
        " mode, hvac_status FROM readings WHERE epoch >= ? ORDER BY epoch ASC",
        (since,)).fetchall()
    cols = ["ts", "epoch", "ambient_c", "ambient_f",
            "setpoint_c", "setpoint_f", "mode", "hvac_status"]
    return [dict(zip(cols, row)) for row in rows]


# ------------------------------------------------------------- cycles ------

def reconstruct_cycles(readings: list[dict]) -> list[dict]:
    """Turn a time-ordered reading list into ON/OFF cycles.

    A cycle starts at the first HEATING/COOLING reading after OFF/UNKNOWN
    and ends at the first OFF reading after it. The last cycle may still
    be open (end_epoch is None). Durations are estimates: true start/end
    lie somewhere between polls.
    """
    cycles: list[dict] = []
    current: dict | None = None
    for r in readings:
        status = r["hvac_status"]
        is_on = status in ("HEATING", "COOLING")
        if is_on and current is None:
            current = {"start_epoch": r["epoch"], "start_ts": r["ts"],
                       "hvac_status": status, "mode": r["mode"],
                       "start_ambient_f": r["ambient_f"],
                       "end_epoch": None, "end_ts": None, "duration_min": None}
        elif not is_on and current is not None:
            current["end_epoch"] = r["epoch"]
            current["end_ts"] = r["ts"]
            current["duration_min"] = (r["epoch"] - current["start_epoch"]) / 60.0
            current["end_ambient_f"] = r["ambient_f"]
            cycles.append(current)
            current = None
        elif is_on and current is not None and status != current["hvac_status"]:
            # HEATING -> COOLING flip without an OFF in between: close old, open new.
            current["end_epoch"] = r["epoch"]
            current["end_ts"] = r["ts"]
            current["duration_min"] = (r["epoch"] - current["start_epoch"]) / 60.0
            current["end_ambient_f"] = r["ambient_f"]
            cycles.append(current)
            current = {"start_epoch": r["epoch"], "start_ts": r["ts"],
                       "hvac_status": status, "mode": r["mode"],
                       "start_ambient_f": r["ambient_f"],
                       "end_epoch": None, "end_ts": None, "duration_min": None}
    if current is not None:
        # Ongoing cycle: leave duration_min as None. Callers that need a
        # run time compute now - start_epoch themselves, so a stale test
        # reading can't produce an absurd duration.
        cycles.append(current)
    return cycles


def persist_completed_cycles(conn: sqlite3.Connection,
                             cycles: list[dict]) -> None:
    """Record newly completed cycles so a rerun doesn't double-count them."""
    for c in cycles:
        if c["end_epoch"] is None:
            continue
        exists = conn.execute(
            "SELECT 1 FROM cycles WHERE start_epoch=? AND hvac_status=?",
            (c["start_epoch"], c["hvac_status"])).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO cycles (start_ts, end_ts, start_epoch, end_epoch,"
                " duration_min, hvac_status, mode) VALUES (?,?,?,?,?,?,?)",
                (c["start_ts"], c["end_ts"], c["start_epoch"], c["end_epoch"],
                 c["duration_min"], c["hvac_status"], c["mode"]))
    conn.commit()


# ------------------------------------------------------------- alerts ------

def last_alert_epoch(conn: sqlite3.Connection, alert_type: str) -> float:
    # Count every recorded alert, sent or not: the cooldown is about not
    # re-raising the same fault every poll, and a failed send is still
    # visible in the log / alerts table for the user to investigate.
    row = conn.execute(
        "SELECT MAX(epoch) FROM alerts WHERE alert_type=?",
        (alert_type,)).fetchone()
    return row[0] or 0.0


def send_gmail_alert(cfg: Config, logger: logging.Logger,
                     subject: str, body: str) -> bool:
    if not cfg.gmail_user or not cfg.gmail_app_password or not cfg.alert_email:
        logger.warning("Gmail not configured (GMAIL_USER / GMAIL_APP_PASSWORD / "
                       "ALERT_EMAIL) — alert NOT sent: %s", subject)
        return False
    msg = EmailMessage()
    msg["From"] = cfg.gmail_user
    msg["To"] = cfg.alert_email
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(cfg.gmail_user, cfg.gmail_app_password)
            s.send_message(msg)
        logger.info("Alert email sent to %s: %s", cfg.alert_email, subject)
        return True
    except Exception as exc:  # noqa: BLE001 — log and continue; don't crash cron
        logger.error("Failed to send alert email: %s", exc)
        return False


def raise_alert(conn: sqlite3.Connection, cfg: Config, logger: logging.Logger,
                alert_type: str, subject: str, body: str,
                now: datetime) -> None:
    """Record + email an alert, respecting the per-type cooldown."""
    cooldown_s = cfg.alert_cooldown_minutes * 60
    if now.timestamp() - last_alert_epoch(conn, alert_type) < cooldown_s:
        logger.info("Alert %s suppressed by cooldown (%.0f min): %s",
                    alert_type, cfg.alert_cooldown_minutes, subject)
        conn.execute(
            "INSERT INTO alerts (alert_type, message, ts, epoch, sent)"
            " VALUES (?,?,?,?,0)",
            (alert_type, subject + " | " + body, now.isoformat(),
             now.timestamp()))
        conn.commit()
        return
    sent = send_gmail_alert(cfg, logger, subject, body)
    conn.execute(
        "INSERT INTO alerts (alert_type, message, ts, epoch, sent)"
        " VALUES (?,?,?,?,?)",
        (alert_type, subject + " | " + body, now.isoformat(),
         now.timestamp(), 1 if sent else 0))
    conn.commit()
    logger.warning("ALERT %s: %s", alert_type, subject)


def fmt_temp(r: dict, cfg: Config) -> str:
    if cfg.temp_unit == "F":
        return (f"{r.get('ambient_f')} F (setpoint {r.get('setpoint_f')} F)"
                if r.get("ambient_f") is not None else "temp unknown")
    return (f"{r.get('ambient_c')} C (setpoint {r.get('setpoint_c')} C)"
            if r.get("ambient_c") is not None else "temp unknown")


def detect_faults(conn: sqlite3.Connection, cfg: Config,
                  logger: logging.Logger, readings: list[dict],
                  now: datetime) -> list[str]:
    """Run all detectors over recent readings. Returns alert types raised."""
    raised: list[str] = []
    if len(readings) < 2:
        return raised

    cycles = reconstruct_cycles(readings)
    persist_completed_cycles(conn, cycles)
    completed = [c for c in cycles if c["end_epoch"] is not None]
    ongoing = [c for c in cycles if c["end_epoch"] is None]
    latest = readings[-1]

    # --- 1. Short cycle: the cycle that just ended was too brief ---------
    # Only judge the most recent completed cycle, and only if it ended
    # on this poll (i.e. the latest reading is OFF after being ON).
    if completed and latest["hvac_status"] == "OFF":
        last = completed[-1]
        # Ended within the last ~2 polls?
        if last["end_epoch"] and (now.timestamp() - last["end_epoch"]) <= 2.5 * 300:
            if last["duration_min"] is not None and \
                    last["duration_min"] <= cfg.short_cycle_minutes:
                subject = (f"[Nest] Short cycle detected: "
                           f"{last['hvac_status'].title()} ran only "
                           f"~{last['duration_min']:.1f} min")
                body = (
                    f"Thermostat: {cfg.device_name}\n"
                    f"Time: {now.isoformat()}\n"
                    f"Cycle: {last['hvac_status']} for ~{last['duration_min']:.1f} min "
                    f"(threshold: <= {cfg.short_cycle_minutes:.0f} min; "
                    f"duration is an estimate, accurate to about one poll interval)\n"
                    f"Mode: {last['mode']} | {fmt_temp(latest, cfg)}\n\n"
                    "Short cycling wears out compressors / igniters and usually "
                    "means an oversized system, a bad sensor/placement, low "
                    "refrigerant (cooling), or a failing component. If this repeats, "
                    "consider an HVAC check — and check the filter first.\n"
                )
                raise_alert(conn, cfg, logger, "SHORT_CYCLE", subject, body, now)
                raised.append("SHORT_CYCLE")

    # --- 2. Rapid cycling: too many cycle starts in the last hour -------
    hour_ago = now.timestamp() - 3600
    starts_last_hour = sum(1 for c in cycles if c["start_epoch"] >= hour_ago)
    if starts_last_hour >= cfg.max_cycles_per_hour:
        # Only alert on a fresh crossing: the latest cycle started recently.
        if cycles and cycles[-1]["start_epoch"] >= hour_ago:
            subject = (f"[Nest] Rapid cycling: {starts_last_hour} cycles "
                       f"in the last hour")
            body = (
                f"Thermostat: {cfg.device_name}\n"
                f"Time: {now.isoformat()}\n"
                f"Cycles started in last 60 min: {starts_last_hour} "
                f"(threshold: >= {cfg.max_cycles_per_hour})\n"
                f"Mode: {latest['mode']} | HVAC: {latest['hvac_status']} | "
                f"{fmt_temp(latest, cfg)}\n\n"
                "Normal is roughly 3-4 cycles/hour at moderate load. "
                "Repeated rapid cycling points to the same causes as short "
                "cycling — see the SHORT_CYCLE note.\n"
            )
            raise_alert(conn, cfg, logger, "RAPID_CYCLING", subject, body, now)
            raised.append("RAPID_CYCLING")

    # --- 3. Long run with no progress toward setpoint --------------------
    if ongoing:
        cur = ongoing[-1]
        run_min = (now.timestamp() - cur["start_epoch"]) / 60.0
        if run_min >= cfg.long_run_minutes and latest.get("ambient_f") is not None \
                and cur.get("start_ambient_f") is not None:
            if cur["hvac_status"] == "HEATING":
                progress = latest["ambient_f"] - cur["start_ambient_f"]
            else:  # COOLING
                progress = cur["start_ambient_f"] - latest["ambient_f"]
            if progress < cfg.long_run_min_progress_f:
                subject = (f"[Nest] Long {cur['hvac_status'].title()} run with "
                           f"no progress: {run_min:.0f} min, only "
                           f"{progress:.1f} F toward setpoint")
                body = (
                    f"Thermostat: {cfg.device_name}\n"
                    f"Time: {now.isoformat()}\n"
                    f"Running: {cur['hvac_status']} for {run_min:.0f} min "
                    f"(threshold: >= {cfg.long_run_minutes:.0f} min)\n"
                    f"Temp moved only {progress:.1f} F toward setpoint in that time.\n"
                    f"Mode: {latest['mode']} | {fmt_temp(latest, cfg)}\n\n"
                    "Possible causes: dirty filter, blocked vents, low "
                    "refrigerant, failing blower/compressor, or extreme outdoor "
                    "temperatures. Check filter and vents first.\n"
                )
                raise_alert(conn, cfg, logger, "LONG_RUN_NO_PROGRESS",
                            subject, body, now)
                raised.append("LONG_RUN_NO_PROGRESS")

    # --- 4. OFF for a long time while far from setpoint -------------------
    if latest["hvac_status"] == "OFF" and latest["mode"] in ("HEAT", "COOL", "HEATCOOL"):
        # Find when it last turned off.
        off_since = None
        for r in reversed(readings):
            if r["hvac_status"] in ("HEATING", "COOLING"):
                break
            off_since = r["epoch"]
        if off_since is not None:
            off_min = (now.timestamp() - off_since) / 60.0
            gap = None
            if latest.get("ambient_f") is not None and latest.get("setpoint_f") is not None:
                gap = abs(latest["ambient_f"] - latest["setpoint_f"])
            if off_min >= cfg.off_no_progress_minutes and gap is not None \
                    and gap >= cfg.off_temp_gap_f:
                subject = (f"[Nest] HVAC OFF for {off_min:.0f} min despite "
                           f"{gap:.1f} F gap to setpoint")
                body = (
                    f"Thermostat: {cfg.device_name}\n"
                    f"Time: {now.isoformat()}\n"
                    f"Mode: {latest['mode']} but HVAC has been OFF for "
                    f"{off_min:.0f} min while room temp is {gap:.1f} F from "
                    f"setpoint ({fmt_temp(latest, cfg)}).\n\n"
                    "Possible causes: thermostat lost power / C-wire issue, "
                    "system fault / safety lockout, schedule or Eco holding it "
                    "off, or the setpoint changed recently.\n"
                )
                raise_alert(conn, cfg, logger, "OFF_NO_PROGRESS", subject, body, now)
                raised.append("OFF_NO_PROGRESS")

    return raised


# -------------------------------------------------------------- failure ----

def record_failure(conn: sqlite3.Connection, cfg: Config,
                   logger: logging.Logger, error: str, now: datetime) -> None:
    count = int(get_meta(conn, "consecutive_failures", "0")) + 1
    set_meta(conn, "consecutive_failures", str(count))
    logger.error("SDM poll failed (%d in a row): %s", count, error)
    if count >= cfg.offline_after_failures:
        subject = f"[Nest] Thermostat unreachable: {count} failed polls in a row"
        body = (f"Thermostat: {cfg.device_name}\nTime: {now.isoformat()}\n"
                f"Consecutive failures: {count}\nLast error: {error}\n\n"
                "Check the Pi's internet, the SDM credentials (refresh token "
                "may have been revoked), and the Google Cloud project status.")
        raise_alert(conn, cfg, logger, "OFFLINE", subject, body, now)


def clear_failures(conn: sqlite3.Connection) -> None:
    set_meta(conn, "consecutive_failures", "0")


# ------------------------------------------------------------------ main ---

def run_poll(cfg: Config, logger: logging.Logger,
             mock_file: Path | None = None) -> int:
    now = datetime.now(timezone.utc)
    conn = init_db(cfg.db_path)

    if mock_file:
        payload = json.loads(mock_file.read_text())
        reading = parse_thermostat(payload)
        logger.info("MOCK poll: %s", {k: v for k, v in reading.items() if k != "raw"})
    else:
        missing = cfg.validate()
        if missing:
            logger.error("Missing config: %s. Copy config.env.example to .env "
                         "and fill in the SDM + Gmail values.", ", ".join(missing))
            return 2
        try:
            token = get_access_token(cfg)
            payload = fetch_thermostat(cfg, token)
            reading = parse_thermostat(payload)
        except Exception as exc:  # noqa: BLE001 — cron must not crash loudly
            record_failure(conn, cfg, logger, str(exc), now)
            return 1
        clear_failures(conn)

        if reading.get("connectivity") == "OFFLINE":
            record_failure(conn, cfg, logger,
                           "Connectivity trait reports OFFLINE", now)
            # Still log the reading, then return — the OFFLINE alert covers it.
            store_reading(conn, reading, now)
            return 1

    if reading.get("device_name"):
        cfg.device_name = reading["device_name"]
    store_reading(conn, reading, now)
    logger.info("Poll OK: mode=%s hvac=%s %s humidity=%s%% connectivity=%s",
                reading["mode"], reading["hvac_status"], fmt_temp(reading, cfg),
                reading.get("humidity"), reading.get("connectivity"))

    readings = recent_readings(conn, hours=3)
    raised = detect_faults(conn, cfg, logger, readings, now)
    if raised:
        logger.warning("Faults raised this poll: %s", ", ".join(raised))
    return 0


def seed_demo(conn_path: Path, logger: logging.Logger) -> None:
    """Insert one fake hour: six 6-min heat cycles with 4-min gaps.

    Lets you verify SHORT_CYCLE + RAPID_CYCLING + Gmail without waiting
    for a real fault. Run: python3 nest_monitor.py --seed-demo
    then: python3 nest_monitor.py --mock-file mock_thermostat.json
    """
    conn = init_db(conn_path)
    now = datetime.now(timezone.utc)
    # Build readings every 2 min for 70 min ending 8 min ago: ON 6 min,
    # OFF 4 min, x6, ending OFF — so the next real/mock poll starts clean
    # and doesn't close a half-formed trailing cycle.
    rows = []
    start = now - timedelta(minutes=78)
    t = start
    end = now - timedelta(minutes=8)
    while t <= end:
        minute_in_block = int((t - start).total_seconds() // 60) % 10
        hvac = "HEATING" if minute_in_block < 6 else "OFF"
        rows.append((t.isoformat(), t.timestamp(), 20.5, 68.9, 21.1, 70.0,
                     "HEAT", hvac, None, 42.0, "ONLINE"))
        t += timedelta(minutes=2)
    conn.executemany(
        "INSERT INTO readings (ts, epoch, ambient_c, ambient_f, setpoint_c,"
        " setpoint_f, mode, hvac_status, eco_mode, humidity, connectivity)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    logger.info("Seeded %d demo readings (6 short heat cycles in ~1h) into %s",
                len(rows), conn_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nest Thermostat monitor (SDM API)")
    ap.add_argument("--mock-file", type=Path,
                    help="JSON file with a raw SDM device payload, for testing")
    ap.add_argument("--test-alert", action="store_true",
                    help="Send a test Gmail alert and exit")
    ap.add_argument("--seed-demo", action="store_true",
                    help="Insert fake short-cycling data into the DB and exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config()
    logger = setup_logging(cfg.log_path, verbose=args.verbose)

    if args.seed_demo:
        seed_demo(cfg.db_path, logger)
        return 0

    if args.test_alert:
        sent = send_gmail_alert(
            cfg, logger, "[Nest] Test alert — thermostat monitor",
            "This is a test alert from nest_monitor.py on your Raspberry Pi.\n"
            "If you got this, Gmail alerting is wired up correctly.")
        return 0 if sent else 1

    return run_poll(cfg, logger, mock_file=args.mock_file)


if __name__ == "__main__":
    raise SystemExit(main())
