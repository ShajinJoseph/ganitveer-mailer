"""
GanitVeer Domain Health Check
Sends 1 test email from each enabled sender domain to TEST_EMAIL.
Called by GitHub Actions domain-health-check.yml.
"""

import os
import sys
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests

def get_env(key):
    val = os.environ.get(key, "").strip()
    if not val:
        print(f"ERROR: {key} is not set")
        sys.exit(1)
    return val

def send_test_email(domain, recipient_email, run_time):
    """Send one test email from this domain. Returns (success, error, latency_ms)."""
    host = domain.get("smtp_host", "smtp.zoho.in")
    port = int(domain.get("smtp_port", 465))
    if port == 587:
        port = 465  # Railway/Zoho: use SSL on 465
    username = domain["smtp_username"]
    password = domain["smtp_password"]
    from_email = domain["from_email"]
    from_name = domain.get("from_name", "GanitVeer Team")
    domain_name = domain["domain"]

    subject = f"[TEST] GanitVeer domain health check — {domain_name}"
    body = f"""This is an automated SMTP health check from the GanitVeer school mailer.

Sender domain : {domain_name}
From          : {from_name} <{from_email}>
Time (UTC)    : {run_time}

No action is required. This message confirms that this sender domain can
authenticate with Zoho SMTP and deliver mail successfully.

---
GanitVeer: India's Mathematics Championship for Young Minds
ganitveer.com
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = recipient_email
    msg.attach(MIMEText(body, "plain"))

    t0 = time.monotonic()
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            refused = smtp.sendmail(from_email, [recipient_email], msg.as_string())
            latency_ms = int((time.monotonic() - t0) * 1000)
            if refused:
                return False, f"Recipient refused: {refused}", latency_ms
            return True, None, latency_ms
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return False, str(e), latency_ms


def main():
    railway_api_url = get_env("RAILWAY_API_URL").rstrip("/")
    cron_secret = get_env("CRON_SECRET")
    test_email = get_env("TEST_EMAIL")

    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    headers = {"X-Cron-Secret": cron_secret}

    print(f"GanitVeer Domain Health Check — {run_time}")
    print(f"Test recipient: {test_email}")
    print()

    # Fetch all enabled domains
    resp = requests.get(f"{railway_api_url}/api/gv-mailer/cron/domains", headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"ERROR: Could not fetch domains — HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)

    data = resp.json()
    domains = data.get("domains", [])
    if not domains:
        print("ERROR: No enabled sender domains found")
        sys.exit(1)

    print(f"Found {len(domains)} enabled domain(s)\n")

    results = []
    failed_count = 0

    for domain in domains:
        domain_name = domain["domain"]
        from_email = domain["from_email"]
        print(f"Testing {domain_name} ({from_email}) ...", end=" ", flush=True)

        success, error, latency_ms = send_test_email(domain, test_email, run_time)

        if success:
            print(f"PASSED ({latency_ms}ms)")
            results.append({"domain": domain_name, "from_email": from_email, "status": "PASSED", "latency_ms": latency_ms, "error": None})
        else:
            print(f"FAILED ({latency_ms}ms) — {error}")
            results.append({"domain": domain_name, "from_email": from_email, "status": "FAILED", "latency_ms": latency_ms, "error": error})
            failed_count += 1

        time.sleep(2)  # small gap between domains

    # Summary
    passed_count = len(results) - failed_count
    print()
    print("=" * 60)
    print(f"SUMMARY: {passed_count}/{len(results)} domains passed")
    print("=" * 60)

    # Write GitHub step summary if in Actions
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(f"## GanitVeer Domain Health Check — {run_time}\n\n")
            f.write(f"**Test email:** {test_email}  \n")
            f.write(f"**Result: {passed_count}/{len(results)} domains passed**\n\n")
            f.write("| Domain | From Email | Status | Latency | Error |\n")
            f.write("|--------|-----------|--------|---------|-------|\n")
            for r in results:
                status_icon = "✅" if r["status"] == "PASSED" else "❌"
                error_text = r["error"] or "—"
                f.write(f"| {r['domain']} | {r['from_email']} | {status_icon} {r['status']} | {r['latency_ms']}ms | {error_text} |\n")

    if failed_count > 0:
        print(f"\n{failed_count} domain(s) failed. Check the results above.")
        sys.exit(1)
    else:
        print("\nAll domains healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
