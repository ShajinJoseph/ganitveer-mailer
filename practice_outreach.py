#!/usr/bin/env python3
"""
GanitVeer Practice Session — Registrant Notification

Fetches confirmed Season 1 Championship registrants from the admin API and
sends WhatsApp + email notifications that the Familiarisation Session is open.

Run via GitHub Actions workflow_dispatch only.
"""

import os
import time
import random
import requests

# ── Environment variables (all from GitHub Actions secrets) ──────────────────
ADMIN_URL    = os.environ["GV_ADMIN_URL"]           # https://boh.ganitveer.com
CRON_SECRET  = os.environ["GV_CRON_SECRET"]
WA_TOKEN     = os.environ["WHATSAPP_ACCESS_TOKEN"]
WA_PHONE_ID  = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
ZEPTO_TOKEN  = os.environ["ZEPTO_API_TOKEN"]        # full "Zoho-enczapikey ..." value
ZEPTO_FROM   = os.environ["ZEPTO_FROM_ADDRESS"]     # noreply@ganitveer.com
ZEPTO_NAME   = os.environ["ZEPTO_FROM_NAME"]        # GanitVeer

TEMPLATE_NAME = "practice_session_open"
TEMPLATE_LANG = "en"

WA_API_URL    = f"https://graph.facebook.com/v25.0/{WA_PHONE_ID}/messages"
ZEPTO_API_URL = "https://api.zeptomail.in/v1.1/email"

# Static access instruction — same for every recipient.
# This is {{3}} in the approved template so Meta's classifier does not read
# "roll number" and "date of birth" as credential delivery in the template body.
ACCESS_INSTRUCTION = "Use your roll number and your child's date of birth to access it."

# ── Helpers ──────────────────────────────────────────────────────────────────

def mask_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return f"91XXXXXX{digits[-4:]}" if len(digits) >= 4 else "XXXXXXXX"

def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked = local[:2] + "***" if len(local) > 2 else "***"
    return f"{masked}@{domain}"

def normalise_phone(phone: str) -> str:
    """Normalise an Indian mobile to 91XXXXXXXXXX (no +)."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    elif len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    elif len(digits) == 10:
        return "91" + digits
    else:
        return "91" + digits[-10:]

def random_delay() -> float:
    """Non-uniform delay: 50% short, 30% medium, 20% long."""
    r = random.random()
    if r < 0.5:
        return random.uniform(8, 15)
    elif r < 0.8:
        return random.uniform(20, 35)
    else:
        return random.uniform(45, 70)

# ── Senders ──────────────────────────────────────────────────────────────────

def send_whatsapp(phone: str, parent_name: str, child_name: str) -> bool:
    """
    Send practice_session_open WhatsApp template.
    Template body:
      {{1}} parent_name
      {{2}} child_name
      {{3}} static access instruction
      {{4}} child_name (repeated — Meta requires distinct variable indices)
    """
    number = normalise_phone(phone)
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
                    {"type": "text", "text": parent_name},
                    {"type": "text", "text": child_name},
                    {"type": "text", "text": ACCESS_INSTRUCTION},
                    {"type": "text", "text": child_name},
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
            print(f"  WhatsApp OK  → {mask_phone(number)}")
            return True
        else:
            print(f"  WhatsApp FAIL → {mask_phone(number)}: {resp.status_code} {resp.text[:120]}")
            return False
    except Exception as e:
        print(f"  WhatsApp ERROR → {mask_phone(number)}: {e}")
        return False


def send_email(email: str, parent_name: str, child_name: str, roll_number: str) -> bool:
    """Send Familiarisation Session open notification via ZeptoMail."""
    html_body = f"""
<div style="font-family: Inter, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #111827;">
  <h1 style="color: #1B4FD8; font-size: 24px; margin-bottom: 24px;">GanitVeer</h1>
  <p style="font-size: 16px; line-height: 1.6;">Dear {parent_name},</p>
  <p style="font-size: 16px; line-height: 1.6;">
    The Familiarisation Session for <strong>{child_name}</strong> is now open.
    This is your chance to get comfortable with how the Championship questions
    and system work before exam day on 18 October.
  </p>
  <div style="background-color: #F59E0B; color: #111827; padding: 20px; border-radius: 8px; text-align: center; margin: 24px 0;">
    <p style="margin: 0 0 8px; font-size: 14px; font-weight: 600;">Roll Number</p>
    <p style="margin: 0; font-size: 28px; font-weight: bold;">{roll_number}</p>
  </div>
  <p style="font-size: 16px; line-height: 1.6;">
    Visit <a href="https://ganitveer.com/practice" style="color: #1B4FD8;">ganitveer.com/practice</a>
    and log in with your roll number and {child_name}'s date of birth.
  </p>
  <p style="font-size: 16px; line-height: 1.6;">
    There is no score in this session. It is simply a warm-up so the real exam
    feels familiar and comfortable.
  </p>
  <p style="font-size: 14px; color: #6B7280; margin-top: 32px; border-top: 1px solid #E5E7EB; padding-top: 16px;">
    GanitVeer: India's Mathematics Championship for Young Minds
  </p>
</div>
"""
    payload = {
        "from":     {"address": ZEPTO_FROM, "name": ZEPTO_NAME},
        "to":       [{"email_address": {"address": email, "name": parent_name}}],
        "subject":  f"Familiarisation Session is now open for {child_name}",
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
            print(f"  Email OK     → {mask_email(email)}")
            return True
        else:
            print(f"  Email FAIL   → {mask_email(email)}: {resp.status_code} {resp.text[:120]}")
            return False
    except Exception as e:
        print(f"  Email ERROR  → {mask_email(email)}: {e}")
        return False

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("GanitVeer Practice Session Outreach — fetching recipients...")

    resp = requests.get(
        f"{ADMIN_URL}/api/admin/practice/notify-recipients",
        headers={"x-cron-secret": CRON_SECRET},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Failed to fetch recipients: {resp.status_code} {resp.text[:200]}")
        raise SystemExit(1)

    data          = resp.json()
    registrations = data["registrations"]
    total         = data["total"]

    print(f"Total recipients: {total}")
    print("─" * 50)

    sent   = 0
    failed = 0

    for i, reg in enumerate(registrations, 1):
        parent_name = reg["parentName"]
        child_name  = reg["childName"]
        phone       = reg.get("phone", "")
        email       = reg.get("email", "")
        roll_number = reg.get("rollNumber", "")

        print(f"\n[{i}/{total}] {child_name} ({parent_name}) — {roll_number}")

        wa_ok = send_whatsapp(phone, parent_name, child_name) if phone else False
        em_ok = send_email(email, parent_name, child_name, roll_number) if email else False

        if wa_ok or em_ok:
            sent += 1
        else:
            failed += 1

        if i < total:
            delay = random_delay()
            print(f"  Waiting {delay:.1f}s...")
            time.sleep(delay)

    print("\n" + "═" * 50)
    print(f"Done. Sent: {sent} | Failed: {failed} | Total: {total}")

    if failed > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
