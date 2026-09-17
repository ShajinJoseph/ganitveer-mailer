"""Standalone GanitVeer school mailer, run by GitHub Actions.

Railway blocks all outbound SMTP, so the actual sending happens here, on a
GitHub Actions runner (which permits outbound SMTP). The Railway backend only
serves the work queue and records results via the /api/gv-mailer/cron/*
endpoints, all gated by the X-Cron-Secret header.

Flow per run:
  1. GET  /cron/pending-schools   -> today's batch (schools with no outreach row)
  2. GET  /cron/active-domain     -> least-used sender domain (with SMTP creds)
  3. for each school, up to the domain's remaining daily cap:
       - render an email from the 10 inline templates
       - send via smtplib SMTP over SSL (port 465)
       - POST /cron/mark-sent  or  /cron/mark-failed
       - sleep random 50-70s between sends
       - stop once the domain's daily cap is reached

This file deliberately has NO imports from the Railway app: the template logic
is copied inline so the runner needs only `requests` (stdlib covers the rest).

Env vars:
  RAILWAY_API_URL, CRON_SECRET,
  SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, FROM_EMAIL, FROM_NAME
  (SMTP_PORT is no longer used: the connection is SSL, hardcoded to port 465.)
"""

import os
import random
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

HTTP_TIMEOUT = 30
SMTP_TIMEOUT = 30


# ===========================================================================
# Inline email templates (copied from ganitveer_mailer/gv_mailer_email_gen.py;
# intentionally NOT imported so this script stays self-contained).
# ===========================================================================

_GREETING = "Dear Principal,"

_CORE = """GanitVeer is not another olympiad. This is one short paper, around 20 to 30 minutes of actual testing inside a 60 minute window, that children sit from home. It is not a coaching programme and is not tied to any school syllabus. The paper is scored by skill area, not as a single lump mark. Every child receives a section-wise skill report, so the gap is visible early, while it is still easy to light the spark.

Season 1 is on Sunday, 18 October 2026 at 5:00 PM IST.

What every child receives
Every child who sits the exam receives an individual skill report, scored section by section, with a short note on what to practise next. Every registered child is also posted a hands-on puzzle gift to their home address, and receives a personalised certificate by email. These go to every participant, regardless of score.

Prizes
A fixed prize pool of ₹1,00,000, awarded on merit, whether few children register or many.
- GanitVeer Champion, 1 child, ₹50,000
- GanitVeer Achiever, 5 children, ₹10,000 each

Winners are decided by marks, with fixed tie-breakers. There is no lucky draw. TDS applies as per Indian Income Tax law where applicable.

Privacy
Results are never published on a public leaderboard. We do not rank children in public or compare one child with another.

GanitVeer Allies
Occasionally a large number of children from one school take part in a season entirely on their own. Where that happens, we recognise the school as a GanitVeer Ally and send an award made to keep and display. Ally status cannot be arranged or purchased, and we pay no commission or incentive to anyone for registrations. The full terms are at ganitveer.com/allies.

How parents register
Parents register directly at ganitveer.com/register. No school permission and no school account are required. The fee is ₹590 inclusive of GST. If we cancel the exam, every registration is refunded in full.

How the school can help
If you find this worth sharing, one note to parents of children aged 5 to 10 is enough. I have attached a poster you are welcome to share if you wish.

About us
GanitVeer is operated by IgniVeer Ventures LLP, a registered company based in Bengaluru. Terms, refund policy, and privacy policy are at ganitveer.com. This is our first season. The design is simple: one paper, one fixed set of prizes, awarded only on merit."""

_SIGNATURE = """Warm regards,
Shajin Joseph
Founder, GanitVeer
IgniVeer Ventures LLP
contact@ganitveer.com
ganitveer.com

To stop receiving emails from us, click here to unsubscribe: {unsubscribe_url}"""

_SUBJECTS = [
    "GanitVeer 2026: a home-based maths championship for children aged 5 to 10",
    "GanitVeer 2026: a national maths championship for children aged 5 to 10, sat from home",
    "GanitVeer 2026: national online maths championship for children aged 5 to 10",
    "For parents of children aged 5 to 10: GanitVeer National Mathematics Championship 2026",
    "GanitVeer 2026: one short online maths paper for children aged 5 to 10",
    "National Mathematics Championship 2026 for children aged 5 to 10, from GanitVeer",
    "GanitVeer 2026: national maths championship for primary school children",
    "GanitVeer 2026: Season 1 on Sunday, 18 October, for children aged 5 to 10",
    "GanitVeer 2026: a maths championship children aged 5 to 10 sit from home",
    "GanitVeer 2026: a skill-based maths championship for children aged 5 to 10",
]

_OPENINGS = [
    "I am Shajin Joseph, founder of GanitVeer, a national online mathematics championship for children aged 5 to 10. I am writing to {school_name} only to ask whether you would be willing to share this with parents in your primary section. There is nothing for your staff to collect, administer, or invigilate. Parents register on their own.",
    "My name is Shajin Joseph and I founded GanitVeer, a national online mathematics championship for children aged 5 to 10. I am reaching out to {school_name} with one request: that you share it with parents in your primary section if you find it worth sharing. There is nothing for your staff to collect, administer, or invigilate.",
    "I run GanitVeer, a national online mathematics championship for children aged 5 to 10, and I am writing to {school_name} to ask only one thing: whether you would pass this on to parents in your primary section. There is nothing for your staff to collect, administer, or invigilate. Parents register on their own.",
    "I am writing to the primary team at {school_name} about GanitVeer, a national online mathematics championship for children aged 5 to 10. All I am asking is that you share it with parents in your primary section if you feel it is worth their attention. There is nothing for your staff to collect, administer, or invigilate.",
    "Thank you for reading this note. I am Shajin Joseph, founder of GanitVeer, a national online mathematics championship for children aged 5 to 10. I am writing to {school_name} to ask only that you share it with parents in your primary section. There is nothing for your staff to collect, administer, or invigilate.",
    "I hope this note finds you well. I am Shajin Joseph, founder of GanitVeer, a national online mathematics championship for children aged 5 to 10. I am writing to ask whether {school_name} might share this with parents in your primary section. There is nothing for your staff to collect, administer, or invigilate.",
    "GanitVeer is a national online mathematics championship for children aged 5 to 10. I am Shajin Joseph, its founder, and I am contacting {school_name} for one reason only: to ask that you share it with parents in your primary section if you find it worthwhile. There is nothing for your staff to collect, administer, or invigilate.",
    "I founded GanitVeer, a national online mathematics championship for children aged 5 to 10, and I am writing to {school_name} to ask whether you would share it with parents in your primary section. There is nothing for your staff to collect, administer, or invigilate. Parents register entirely on their own.",
    "A short note from Shajin Joseph, founder of GanitVeer, a national online mathematics championship for children aged 5 to 10. I am writing to {school_name} to ask only that you share it with parents in your primary section if you find it worth passing on. There is nothing for your staff to collect, administer, or invigilate.",
    "I am Shajin Joseph, reaching out to {school_name} on behalf of GanitVeer, a national online mathematics championship for children aged 5 to 10. This is purely a request to share it with parents in your primary section. There is nothing for your staff to collect, administer, or invigilate. Parents register on their own.",
]

_CLOSINGS = [
    "I am happy to answer questions, or to send the parent note on request.",
    "If anything here is unclear, I would be glad to explain, and I can send a ready made parent note whenever you wish.",
    "Please do write back with any questions. I am also happy to send a parent note you can forward directly.",
    "I would welcome any questions you have, and I can send a short parent note on request.",
    "If it helps, I can send a parent note ready to forward, and I am happy to answer anything you would like to know.",
    "Do reach out if you have questions. A ready to share parent note is available whenever you need it.",
    "I am glad to answer anything, and a prepared parent note is yours for the asking.",
    "Should you have questions, I will answer them gladly, and I can provide a parent note ready to send.",
    "Feel free to reply with questions, and let me know if you would like the parent note to forward.",
    "I am here to help with any questions, and a parent note is ready to send the moment you ask.",
]


def _build_body(opening: str, closing: str) -> str:
    return "\n\n".join([_GREETING, opening, _CORE, closing, _SIGNATURE])


EMAIL_TEMPLATES = [
    {"subject": subject, "body": _build_body(opening, closing)}
    for subject, opening, closing in zip(_SUBJECTS, _OPENINGS, _CLOSINGS)
]


def get_random_template(school_name: str, recipient_email: str = "") -> dict:
    """Pick one of the ten templates at random and substitute the school name
    and the per-recipient unsubscribe URL."""
    unsubscribe_url = f"https://ganitveer.com/unsubscribe?email={recipient_email}"
    template = random.choice(EMAIL_TEMPLATES)
    body = template["body"].replace("{school_name}", school_name)
    body = body.replace("{unsubscribe_url}", unsubscribe_url)
    return {"subject": template["subject"], "body": body}


# ===========================================================================
# Config
# ===========================================================================

def _require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        print(f"[github-mailer] FATAL: required env var {name} is not set")
        sys.exit(1)
    return value


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _mask_email(email: str) -> str:
    """Mask email for public logs: principal@school.edu.in -> pr****@school.edu.in"""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked_local = local[:2] + "****" if len(local) > 2 else "****"
    return f"{masked_local}@{domain}"


# ===========================================================================
# SMTP send
# ===========================================================================

def send_smtp(smtp, from_email, from_name, to_email, subject, body) -> None:
    """Send one email via SMTP SSL with the GanitVeer PDFs attached."""
    import pathlib
    from email.mime.application import MIMEApplication
    # MIMEMultipart and MIMEText are imported at module top.

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach the GanitVeer PDFs. Each is skipped independently if missing, so
    # a single absent file never blocks the email or the other attachment.
    attachments_dir = pathlib.Path(__file__).parent / "assets"
    for pdf_name in (
        "GanitVeer_Allies_Institutions.pdf",
        "GanitVeer_For_Parents.pdf",
    ):
        pdf_path = attachments_dir / pdf_name
        if pdf_path.exists():
            with open(pdf_path, "rb") as f:
                attachment = MIMEApplication(f.read(), _subtype="pdf")
                attachment.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=pdf_name
                )
                msg.attach(attachment)
            print(f"[github-mailer] attached {pdf_name} ({pdf_path.stat().st_size // 1024}KB)")
        else:
            print(f"[github-mailer] WARNING: attachment not found at {pdf_path}")

    with smtplib.SMTP_SSL(smtp["host"], 465, timeout=SMTP_TIMEOUT) as server:
        server.login(smtp["username"], smtp["password"])
        server.sendmail(from_email, [to_email], msg.as_string())


# ===========================================================================
# Main
# ===========================================================================

def _smtp_config(domain: dict):
    """Resolve SMTP creds + From identity: prefer the domain record, fall back
    to env vars for any field the API did not provide."""
    smtp = {
        "host":     domain.get("smtp_host") or _require_env("SMTP_HOST"),
        "port":     int(domain.get("smtp_port") or _env("SMTP_PORT", "587") or 587),
        "username": domain.get("smtp_username") or _require_env("SMTP_USERNAME"),
        "password": domain.get("smtp_password") or _require_env("SMTP_PASSWORD"),
    }
    from_email = domain.get("from_email") or _require_env("FROM_EMAIL")
    from_name = domain.get("from_name") or _env("FROM_NAME", "GanitVeer Team") or "GanitVeer Team"
    return smtp, from_email, from_name


def _fetch_pending(base, headers, limit=None, shard=None, shard_count=None):
    params = {}
    if limit:
        params["limit"] = limit
    if shard is not None and shard_count is not None:
        params["shard"] = shard
        params["shard_count"] = shard_count
    resp = requests.get(f"{base}/cron/pending-schools", headers=headers,
                        params=params or None, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json() or []


def _send_batch(base, headers, batch, domain, did_smtp):
    """Send to every school in `batch` using `domain`. A non-uniform 45-180s
    delay is applied before each send after the first; `did_smtp` is threaded
    through so a multi-domain run keeps one global cadence. Returns
    (sent, failed, did_smtp)."""
    smtp, from_email, from_name = _smtp_config(domain)
    domain_id = domain["id"]
    sent = 0
    failed = 0
    for school in batch:
        school_id = school.get("id")
        to_email = (school.get("email") or "").strip().split(",")[0].strip()
        school_name = school.get("school_name") or "your school"

        if not to_email or "@" not in to_email:
            print(f"[github-mailer] school {school_id}: invalid email, marking failed")
            _mark_failed(base, headers, school_id, "invalid or missing email")
            failed += 1
            continue

        if did_smtp:
            # Non-uniform delay between sends - reduces server load
            delay = random.choice([
                random.uniform(45, 75),    # short pause - 50% of sends
                random.uniform(45, 75),
                random.uniform(45, 75),
                random.uniform(45, 75),
                random.uniform(45, 75),
                random.uniform(75, 120),   # medium pause - 30% of sends
                random.uniform(75, 120),
                random.uniform(75, 120),
                random.uniform(120, 180),  # long pause - 20% of sends
                random.uniform(120, 180),
            ])
            print(f"[github-mailer] sleeping {delay:.1f}s before next send")
            time.sleep(delay)

        template = get_random_template(school_name, to_email)
        try:
            send_smtp(smtp, from_email, from_name, to_email,
                      template["subject"], template["body"])
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"[github-mailer] school {school_id} ({_mask_email(to_email)}): send FAILED: {err}")
            _mark_failed(base, headers, school_id, err)
            failed += 1
        else:
            print(f"[github-mailer] school {school_id} ({_mask_email(to_email)}): sent")
            _mark_sent(base, headers, school_id, domain_id, template["subject"])
            sent += 1
        did_smtp = True

    return sent, failed, did_smtp


def _run_single_domain(base, headers, domain_name) -> int:
    """One matrix job: send only for `domain_name`, up to its remaining cap."""
    try:
        resp = requests.get(f"{base}/cron/domain-by-name", headers=headers,
                            params={"domain": domain_name}, timeout=HTTP_TIMEOUT)
        if resp.status_code == 404:
            print(f"[github-mailer] domain {domain_name} not found or disabled - nothing to do")
            return 0
        resp.raise_for_status()
        domain = resp.json()
    except Exception as e:
        print(f"[github-mailer] FATAL: could not fetch domain {domain_name}: {e}")
        return 1
    if not domain:
        print(f"[github-mailer] domain {domain_name} not found - nothing to do")
        return 0

    daily_cap = int(domain.get("daily_cap") or 0)
    today_sends = int(domain.get("today_sends") or 0)
    remaining = max(0, daily_cap - today_sends)
    if remaining <= 0:
        print(f"[github-mailer] domain {domain_name} at daily cap ({today_sends}/{daily_cap}) - nothing to do")
        return 0

    # Deterministic partition: this job only takes schools where
    # (id % shard_count) == shard, so parallel domain jobs never overlap.
    shard_raw = _env("MAILER_SHARD")
    shard_count_raw = _env("MAILER_SHARD_COUNT")
    shard = int(shard_raw) if shard_raw else None
    shard_count = int(shard_count_raw) if shard_count_raw else None

    try:
        pending = _fetch_pending(base, headers, limit=remaining,
                                 shard=shard, shard_count=shard_count)
    except Exception as e:
        print(f"[github-mailer] FATAL: could not fetch pending schools: {e}")
        return 1
    if not pending:
        print("[github-mailer] No pending schools. Nothing to do.")
        return 0

    shard_note = f", shard {shard}/{shard_count}" if shard is not None and shard_count else ""
    print(f"[github-mailer] domain {domain_name}: sending {len(pending)} (remaining cap {remaining}{shard_note}).")
    sent, failed, _ = _send_batch(base, headers, pending, domain, did_smtp=False)
    print(f"[github-mailer] Done ({domain_name}). sent={sent} failed={failed}")
    return 0


def _run_all_domains(base, headers) -> int:
    """Backwards-compatible path (no MAILER_DOMAIN): fetch all pending once, then
    loop the least-used domain until schools run out or every domain is capped."""
    try:
        pending = _fetch_pending(base, headers)
    except Exception as e:
        print(f"[github-mailer] FATAL: could not fetch pending schools: {e}")
        return 1
    if not pending:
        print("[github-mailer] No pending schools. Nothing to do.")
        return 0

    print(f"[github-mailer] {len(pending)} pending schools to process.")
    sent = 0
    failed = 0
    did_smtp = False
    while pending:
        try:
            resp = requests.get(f"{base}/cron/active-domain", headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            domain = resp.json()
        except Exception as e:
            print(f"[github-mailer] FATAL: could not fetch active domain: {e}")
            return 1
        if not domain:
            print("[github-mailer] all domains at daily cap")
            break
        daily_cap = int(domain.get("daily_cap") or 0)
        today_sends = int(domain.get("today_sends") or 0)
        remaining = max(0, daily_cap - today_sends)
        if remaining <= 0:
            print("[github-mailer] all domains at daily cap")
            break
        batch = pending[:remaining]
        print(f"[github-mailer] domain {domain.get('domain')} remaining cap {remaining}; "
              f"sending {len(batch)} of {len(pending)} remaining.")
        s, f, did_smtp = _send_batch(base, headers, batch, domain, did_smtp)
        sent += s
        failed += f
        pending = pending[len(batch):]
        if not pending:
            break

    print(f"[github-mailer] Done. total sent={sent} failed={failed}")
    return 0


def main() -> int:
    api_url = _require_env("RAILWAY_API_URL").rstrip("/")
    cron_secret = _require_env("CRON_SECRET")
    headers = {"X-Cron-Secret": cron_secret}
    base = f"{api_url}/api/gv-mailer"

    mailer_domain = _env("MAILER_DOMAIN")
    if mailer_domain:
        print(f"[github-mailer] MAILER_DOMAIN={mailer_domain} - single-domain mode")
        return _run_single_domain(base, headers, mailer_domain)
    print("[github-mailer] no MAILER_DOMAIN - multi-domain (active-domain) mode")
    return _run_all_domains(base, headers)


def _mark_sent(base, headers, school_id, domain_id, subject) -> None:
    try:
        r = requests.post(
            f"{base}/cron/mark-sent", headers=headers, timeout=HTTP_TIMEOUT,
            json={"school_id": school_id, "domain_id": domain_id, "subject": subject},
        )
        r.raise_for_status()
    except Exception as e:
        # The email already went out; log loudly but do not fail the run.
        print(f"[github-mailer] WARNING: mark-sent failed for school {school_id}: {e}")


def _mark_failed(base, headers, school_id, error) -> None:
    try:
        r = requests.post(
            f"{base}/cron/mark-failed", headers=headers, timeout=HTTP_TIMEOUT,
            json={"school_id": school_id, "error": error[:500]},
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[github-mailer] WARNING: mark-failed failed for school {school_id}: {e}")


if __name__ == "__main__":
    sys.exit(main())
