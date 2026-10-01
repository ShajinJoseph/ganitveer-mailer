#!/usr/bin/env python3
"""
GanitVeer Championship Date Update - Registrant Outreach

Fetches confirmed Season 1 registrants from the admin API, then asks the admin
endpoint to send each one the championship date update (WhatsApp + email).

This script only drives the loop. The copy, the templates and both sends live
server-side in POST /api/championship-date-notify, which handles ONE registrant
per call. That is what keeps each serverless invocation short enough to finish;
the pacing between registrations lives here, on the runner, where a long blast
is allowed to take as long as it takes.

Run via GitHub Actions workflow_dispatch only.
"""

import os
import sys
import time
import random
import requests

# -- Environment variables (all from GitHub Actions secrets) -----------------
ADMIN_URL   = os.environ["GV_ADMIN_URL"]      # https://boh.ganitveer.com
CRON_SECRET = os.environ["GV_CRON_SECRET"]

RECIPIENTS_PATH = "/api/admin/championship-date-recipients"
NOTIFY_PATH     = "/api/championship-date-notify"

# The endpoint does both sends for one registrant, so this covers two provider
# calls rather than one. It is still far inside the route's own 300s ceiling.
REQUEST_TIMEOUT = 120

# -- Helpers -----------------------------------------------------------------

def random_delay() -> float:
    """Non-uniform delay: 50% short, 30% medium, 20% long."""
    r = random.random()
    if r < 0.5:
        return random.uniform(8, 15)
    elif r < 0.8:
        return random.uniform(20, 35)
    else:
        return random.uniform(45, 70)


def notify_one(registration_id: str) -> bool:
    """
    Ask the admin endpoint to send to a single registrant.

    Returns True only when the endpoint reports a clean send. Any transport
    failure, non-200, unparseable body, or a response carrying errors is a
    failure and never raises, so one bad registrant cannot abort the blast.
    """
    try:
        resp = requests.post(
            f"{ADMIN_URL}{NOTIFY_PATH}",
            headers={
                "x-cron-secret": CRON_SECRET,
                "Content-Type": "application/json",
            },
            json={"registrationId": registration_id},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        print(f"    ERROR: request failed: {e}")
        return False

    if resp.status_code != 200:
        print(f"    ERROR: HTTP {resp.status_code}")
        return False

    try:
        body = resp.json()
    except Exception:
        print("    ERROR: response was not JSON")
        return False

    sent = body.get("sent", 0)
    errors = body.get("errors", 0)
    if sent == 1 and not errors:
        return True

    print(f"    ERROR: endpoint reported sent={sent} errors={errors}")
    return False

# -- Main --------------------------------------------------------------------

def main():
    print("GanitVeer Championship Date Update - fetching recipients...")

    resp = requests.get(
        f"{ADMIN_URL}{RECIPIENTS_PATH}",
        headers={"x-cron-secret": CRON_SECRET},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Failed to fetch recipients: {resp.status_code} {resp.text[:200]}")
        raise SystemExit(1)

    data = resp.json()
    registrations = data["registrations"]
    total = data["total"]

    print(f"Total recipients: {total}")
    print("-" * 50)

    sent_count = 0
    failed = 0

    for i, reg in enumerate(registrations, 1):
        parent_name = reg.get("parent_name", "")
        child_name = reg.get("child_name", "")
        registration_id = reg.get("id", "")

        # First names only, matching the other outreach scripts: this repo is
        # public, so Actions logs are public, and full names do not belong in
        # them. The id is logged only on failure, where it is needed to retry.
        parent_first = parent_name.split()[0] if parent_name else ""
        child_first = child_name.split()[0] if child_name else ""

        if not registration_id:
            failed += 1
            print(f"\n[{i}/{total}] SKIPPED: record has no id")
            continue

        print(f"\n[{i}/{total}] Sending to {parent_first} ({child_first})...")

        if notify_one(registration_id):
            sent_count += 1
            print(f"  Sent to {parent_first} ({child_first})")
        else:
            failed += 1
            print(f"  FAILED for {parent_first} ({child_first}) [id {registration_id}]")

        if i < total:
            delay = random_delay()
            print(f"  Waiting {delay:.1f}s...")
            time.sleep(delay)

    print("\n" + "=" * 50)
    print(f"Done. Sent: {sent_count} | Failed: {failed} | Total: {total}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
