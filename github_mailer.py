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

_CORE = """Through animated stories and video questions, children explore mathematical ideas, notice patterns and reason through playful challenges. No preparation is needed, and the questions are not tied to a particular school syllabus. Our aim is to make children feel curious about maths and appreciated for taking part.

For parents, the experience offers an individual skill report that helps them understand their child's strengths and areas to explore further.

We would appreciate you taking a look at the experience and considering whether it would interest your school's families:
https://ganitveer.com

What children receive
* An individual skill report for every child who participates.
* A hands-on puzzle gift delivered to every registered child's home.
* A personalised participation certificate by email.

Participation details
* Date: Sunday, 29 November 2026
* Time: 5:00 PM IST
* Format: Online, from home, using a phone, tablet or computer
* Duration: Around 20–30 minutes, with up to 60 minutes allowed
* Registration fee: ₹590 inclusive of GST

A fixed prize pool of ₹1,00,000 recognises six children: ₹50,000 for one GanitVeer Champion and ₹10,000 each for five GanitVeer Achievers. Every participant is appreciated, and no public rank list is published.

Parents register directly through the website. School staff do not need to collect fees, manage registrations or invigilate."""

_SIGNATURE = """Warm regards,
Shajin Joseph
Founder, GanitVeer
IgniVeer Ventures LLP, Bengaluru
contact@ganitveer.com
https://ganitveer.com

To stop receiving emails from us, unsubscribe here: {unsubscribe_url}"""

_SUBJECTS = [
    "GanitVeer: a joyful maths experience for children aged 5 to 10",
    "GanitVeer: a thinking experience in maths for children aged 5 to 10",
    "GanitVeer: an individual skill report for every child aged 5 to 10",
    "GanitVeer: a national online maths championship for children aged 5 to 10",
    "GanitVeer: maths children aged 5 to 10 can enjoy from home",
    "GanitVeer: playful maths challenges for children aged 5 to 10",
    "GanitVeer: a maths experience where every child aged 5 to 10 is appreciated",
    "GanitVeer: a home-based maths championship for children aged 5 to 10",
    "GanitVeer: no preparation needed, a maths experience for children aged 5 to 10",
    "GanitVeer: an invitation for children aged 5 to 10 to think and explore",
]

_OPENINGS = [
    "We would like to invite children aged 5 to 10 at {school_name} to GanitVeer, a national online mathematics championship designed as a joyful thinking experience.",
    "We would like to introduce {school_name} to GanitVeer, a national online mathematics championship for children aged 5 to 10, where each child receives an individual skill report that helps parents understand their strengths and the areas they might explore next.",
    "It is a pleasure to write to {school_name} about GanitVeer, a national online mathematics championship created for children aged 5 to 10 to enjoy maths as a joyful thinking experience.",
    "We would be delighted to welcome children aged 5 to 10 from {school_name} to GanitVeer, a national online mathematics championship that children take part in from the comfort of home.",
    "We are writing to {school_name} to share GanitVeer, a national online mathematics championship designed so that children aged 5 to 10 can explore mathematical ideas with curiosity and enjoyment.",
    "We would like to tell you about GanitVeer, a national online mathematics championship for children aged 5 to 10, and to ask whether {school_name} might consider sharing this opportunity with its families.",
    "Every child deserves to feel curious about maths, and GanitVeer was created with that in mind. We would like to invite children aged 5 to 10 at {school_name} to take part in this national online mathematics championship.",
    "We are writing to {school_name} to introduce GanitVeer, a national online mathematics championship for children aged 5 to 10 that is designed to be enjoyed at home, with no preparation needed.",
    "Children aged 5 to 10 at {school_name} are warmly invited to GanitVeer, a national online mathematics championship where every participant is appreciated and every child is recognised for taking part.",
    "We would like to share GanitVeer with you and your team at {school_name}. It is a national online mathematics championship that invites children aged 5 to 10 to explore mathematical ideas, notice patterns and enjoy thinking through playful challenges.",
]

_CLOSINGS = [
    "If you find the experience worthwhile, would you share the attached parent information with families of children aged 5 to 10? We have also attached details of GanitVeer Allies, our school recognition programme.\n\nWe would be happy to answer any questions and would welcome your thoughts.",
    "If GanitVeer feels like a good fit for your families, we would be grateful if you would share the attached parent information with them. We would also be glad to answer any questions you may have.",
    "Should you feel this would be valuable for your school's families, we would warmly welcome your help in sharing the attached parent information with them. Any questions you have are most welcome.",
    "If you think this might interest your school's families, would you consider passing the attached parent information on to them? We would be happy to talk through anything you would like to know.",
    "We would be grateful if you would consider sharing the attached parent information with families of children aged 5 to 10, if it feels right for your school. The attached details of GanitVeer Allies describe our school recognition programme, and we are always glad to answer questions.",
    "If this seems worthwhile to you, sharing the attached parent information with your families would mean a great deal to us. Please do write back with any questions.",
    "If you feel this could be of interest to your school community, we would be grateful if you could share the attached parent information with families of children aged 5 to 10. We would welcome the chance to answer any questions.",
    "We would be delighted if you chose to share the attached parent information with your families, should it feel like a good fit. Details of GanitVeer Allies, our school recognition programme, are attached as well, and any questions you may have are warmly welcome.",
    "If you find this worth passing on, we would be grateful for your help in sharing the attached parent information with families of children aged 5 to 10. We would be happy to answer any questions, and we would value your thoughts.",
    "Should you feel able to share the attached parent information with your families, we would be most grateful. The attached GanitVeer Allies details set out our school recognition programme, and we would be pleased to answer any questions or hear your thoughts.",
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
