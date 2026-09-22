"""
Run by a Render Cron Job once a day. Calls the live web service's
reminder-check endpoint, which sends loan-due and contribution-due
reminder emails as needed.

This script itself needs no database access - it just makes one HTTP
call to the already-running web service. Needs two environment variables
set on the CRON JOB service (separate from the main web service):
    SITE_URL         - the web service's URL, e.g. https://my-sacco.onrender.com
    REMINDER_SECRET  - must match the same value set on the web service

Usage: python send_reminders.py
"""

import os
import sys
import requests

SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "")


def main():
    if not SITE_URL or not REMINDER_SECRET:
        print("SITE_URL and REMINDER_SECRET must both be set.")
        sys.exit(1)

    url = f"{SITE_URL}/admin/send-reminders"
    try:
        resp = requests.post(url, params={"secret": REMINDER_SECRET}, timeout=60)
        resp.raise_for_status()
        print("Reminder check complete:", resp.json())
    except requests.RequestException as e:
        print("Reminder check failed:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
