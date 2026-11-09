#!/bin/bash
# Quick install on Raspberry Pi 4 — run from inside the nest-monitor folder.
# Assumes this folder is already at ~/nest-monitor.
set -e
cd "$(dirname "$0")"
chmod +x nest_monitor.py get_refresh_token.py
if [ ! -f .env ]; then
  cp config.env.example .env
  echo "Created .env from example — edit it now: nano .env"
else
  echo ".env already exists — leaving it alone."
fi
chmod 600 .env 2>/dev/null || true
python3 --version
python3 nest_monitor.py --mock-file mock_thermostat.json
echo ""
echo "Mock poll OK. Next:"
echo "  1) nano .env  (fill in SDM_* + GMAIL_*)"
echo "  2) python3 nest_monitor.py --test-alert"
echo "  3) python3 nest_monitor.py --verbose"
echo "  4) crontab -e and add:"
echo "     */2 * * * * /usr/bin/python3 /home/pi/nest-monitor/nest_monitor.py >> /home/pi/nest-monitor/cron.log 2>&1"
