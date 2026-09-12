#!/usr/bin/env python3
"""
GanitVeer PlayTime — Championship Registrant Outreach
Fetches confirmed Championship registrants from the admin API and sends
WhatsApp + email notifications about the upcoming PlayTime session.
Run via GitHub Actions workflow_dispatch only.
"""

import os
import time
import random
import json
import requests

# ── Environment variables (all from GitHub Actions secrets) ──────────────────
ADMIN_URL = os.environ["GV_ADMIN_URL"]          # https://boh.ganitveer.com
CRON_SECRET = os.environ["GV_CRON_SECRET"]
WA_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]
WA_PHONE_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
ZEPTO_TOKEN = os.environ["ZEPTO_API_TOKEN"]      # full "Zoho-enczapikey ..." value
ZEPTO_FROM = os.environ["ZEPTO_FROM_ADDRESS"]    # noreply@ganitveer.com
ZEPTO_NAME = os.environ["ZEPTO_FROM_NAME"]       # GanitVeer

TEMPLATE_NAME = "playtime_championship_invite"
TEMPLATE_LANG = "en"
WA_API_URL = f"https://graph.facebook.com/v25.0/{WA_PHONE_ID}/messages"
ZEPTO_API_URL = "https://api.zeptomail.in/v1.1/email"

# ── Helpers ──────────────────────────────────────────────────────────────────

def first_word(name: str) -> str:
    return (name or "").split()[0] if name else ""

def random_delay() -> float:
    """Non-uniform delay: 50% short, 30% medium, 20% long."""
    r = random.random()
    if r < 0.5:
        return random.uniform(8, 15)
    elif r < 0.8:
        return random.uniform(20, 35)
    else:
        return random.uniform(45, 70)

def send_whatsapp(phone: str, parent_first: str, child_first: str, playtime_date: str) -> bool:
    """Send playtime_championship_invite WhatsApp template."""
    # Normalise phone to 91XXXXXXXXXX
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        number = digits
    elif len(digits) == 11 and digits.startswith("0"):
        number = "91" + digits[1:]
    elif len(digits) == 10:
        number = "91" + digits
    else:
        number = "91" + digits[-10:]

    payload = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": parent_first},
                    {"type": "text", "text": child_first},
                    {"type": "text", "text": "GanitVeer PlayTime"},
                    {"type": "text", "text": playtime_date},
                ]
            }]
        }
    }
    try:
        resp = requests.post(
            WA_API_URL,
            headers={
                "Authorization": f"Bearer {WA_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"  WhatsApp OK → {number}")
            return True
        else:
            print(f"  WhatsApp FAIL → {number}: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  WhatsApp ERROR → {number}: {e}")
        return False

def send_email(email: str, parent_first: str, child_first: str, playtime_date: str) -> bool:
    """Send PlayTime notification email via ZeptoMail REST API."""
    html_body = f"""
<p>Dear {parent_first},</p>
<p>This is a reminder that {child_first}'s GanitVeer PlayTime session is coming up.</p>
<p><strong>Date:</strong> {playtime_date}<br>
<strong>Time:</strong> Open until 11:30 PM IST</p>
<p>Visit <a href="https://ganitveer.com/playtime">ganitveer.com/playtime</a> to register and begin.</p>
<p>Team GanitVeer</p>
"""
    payload = {
        "from": {"address": ZEPTO_FROM, "name": ZEPTO_NAME},
        "to": [{"email_address": {"address": email, "name": parent_first}}],
        "subject": "GanitVeer PlayTime — join us this Sunday",
        "htmlbody": html_body,
    }
    try:
        resp = requests.post(
            ZEPTO_API_URL,
            headers={
                "Authorization": ZEPTO_TOKEN,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  Email OK → {email}")
            return True
        else:
            print(f"  Email FAIL → {email}: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  Email ERROR → {email}: {e}")
        return False

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("GanitVeer PlayTime Outreach — fetching recipients...")

    # Fetch recipients from admin API
    resp = requests.get(
        f"{ADMIN_URL}/api/admin/playtime/championship-invite",
        headers={"x-cron-secret": CRON_SECRET},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Failed to fetch recipients: {resp.status_code} {resp.text}")
        raise SystemExit(1)

    data = resp.json()
    registrations = data["registrations"]
    playtime_date = data["playtimeDate"]
    total = data["total"]

    print(f"PlayTime date: {playtime_date}")
    print(f"Total recipients: {total}")
    print("─" * 50)

    sent = 0
    failed = 0

    for i, reg in enumerate(registrations, 1):
        parent_first = first_word(reg["parentName"])
        child_first = first_word(reg["childName"])
        phone = reg.get("phone", "")
        email = reg.get("email", "")

        print(f"\n[{i}/{total}] {child_first} — parent: {parent_first}")

        wa_ok = send_whatsapp(phone, parent_first, child_first, playtime_date) if phone else False
        em_ok = send_email(email, parent_first, child_first, playtime_date) if email else False

        if wa_ok or em_ok:
            sent += 1
        else:
            failed += 1

        # Non-uniform delay (skip after last recipient)
        if i < total:
            delay = random_delay()
            print(f"  Waiting {delay:.1f}s...")
            time.sleep(delay)

    print("\n" + "═" * 50)
    print(f"Done. Sent: {sent} | Failed: {failed} | Total: {total}")

    if failed > 0:
        raise SystemExit(1)  # Marks workflow as failed so you notice

if __name__ == "__main__":
    main()
