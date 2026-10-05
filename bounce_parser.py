#!/usr/bin/env python3
"""
GanitVeer Zoho Mail Bounce Parser

Sweeps the three GanitVeer Zoho Mail mailboxes for bounce / delivery-failure
notifications, extracts the address that actually failed, and reports it to the
Railway mailer DB via POST /api/gv-mailer/cron/mark-bounced so the mailer never
retries a dead address.

Flow per account:
  1. POST https://accounts.zoho.in/oauth/v2/token   -> access_token (refresh grant)
  2. GET  https://www.zohoapis.in/api/accounts      -> accountId (first result)
  3. GET  .../messages/search?searchKey=<key>       -> candidate messages
     (three keys: mailer-daemon, "delivery failed", undelivered)
  4. GET  .../messages/{id}/content                 -> raw MIME / bounce body
  5. regex the body for the failed recipient address

The DB side is the same Railway app github_mailer.py talks to, reached the same
way, with the same base path and header:

    {RAILWAY_API_URL}/api/gv-mailer/cron/mark-bounced    (X-Cron-Secret header)

That route is not deployed yet. A 404 is treated as "not deployed", logged once
per address, and the run continues. Nothing in here raises: one bad account, one
bad message or one bad address must never abort the sweep.

This repo is public, so Actions logs are public. Addresses are masked
(pr****@school.edu.in) in everything printed, exactly as github_mailer.py does.

Env vars:
  ZOHO_CLIENT_ID_OUTREACH  / ZOHO_CLIENT_SECRET_OUTREACH  / ZOHO_REFRESH_TOKEN_OUTREACH
      -> ganitveer@ganitveeroutreach.in
  ZOHO_CLIENT_ID_REACH     / ZOHO_CLIENT_SECRET_REACH     / ZOHO_REFRESH_TOKEN_REACH
      -> ganitveer@ganitveerreach.in
  ZOHO_CLIENT_ID_KITS      / ZOHO_CLIENT_SECRET_KITS      / ZOHO_REFRESH_TOKEN_KITS
      -> ganitveer@getsomekits.com
  RAILWAY_API_URL, CRON_SECRET

Exits 1 only when all three accounts fail to authenticate.
"""

import os
import re
import sys

import requests

HTTP_TIMEOUT = 30

# -- Zoho endpoints ----------------------------------------------------------

ZOHO_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
ZOHO_API_BASE = "https://www.zohoapis.in"

# The mailer boxes, in the order the credentials appear in the brief.
ACCOUNTS = [
    {
        "label": "outreach",
        "mailbox": "ganitveer@ganitveeroutreach.in",
        "client_id": "ZOHO_CLIENT_ID_OUTREACH",
        "client_secret": "ZOHO_CLIENT_SECRET_OUTREACH",
        "refresh_token": "ZOHO_REFRESH_TOKEN_OUTREACH",
    },
    {
        "label": "reach",
        "mailbox": "ganitveer@ganitveerreach.in",
        "client_id": "ZOHO_CLIENT_ID_REACH",
        "client_secret": "ZOHO_CLIENT_SECRET_REACH",
        "refresh_token": "ZOHO_REFRESH_TOKEN_REACH",
    },
    {
        "label": "kits",
        "mailbox": "ganitveer@getsomekits.com",
        "client_id": "ZOHO_CLIENT_ID_KITS",
        "client_secret": "ZOHO_CLIENT_SECRET_KITS",
        "refresh_token": "ZOHO_REFRESH_TOKEN_KITS",
    },
]

# "delivery failed" is sent as searchKey=delivery+failed (requests encodes the
# space in a query value as "+"), i.e. exactly the key in the brief.
SEARCH_KEYS = ["mailer-daemon", "delivery failed", "undelivered"]
SEARCH_LIMIT = 200      # Zoho caps limit at 200
MAX_SEARCH_PAGES = 5    # bounded pagination; stops early on a short/duplicate page

# Our own sender / return-path domains. Anything on these is never a real
# failure, so it is dropped before anything is reported to the DB.
OUR_DOMAINS = {
    "ganitveeroutreach.in",
    "ganitveerreach.in",
    "getsomekits.com",
    "ganitveer.com",
}

# Local parts that are the bounce machinery itself, never a failed recipient.
NON_RECIPIENT_LOCALS = {
    "mailer-daemon",
    "mailerdaemon",
    "postmaster",
    "no-reply",
    "noreply",
    "donotreply",
}

# -- Bounce-body patterns ----------------------------------------------------

BOUNCE_PATTERNS = [
    (r"Final-Recipient:.*?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
     re.IGNORECASE | re.DOTALL),
    (r"failed:?\s*<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?",
     re.IGNORECASE),
    (r"<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>:\s*\n.*?550",
     re.IGNORECASE | re.DOTALL),
    (r"X-Failed-Recipients:\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
     re.IGNORECASE),
]

# Two more very common real-world shapes where the address sits on its own line
# with no "Final-Recipient:"/"failed:" prefix in front of it, so none of the five
# patterns above reach it:
#   Gmail: "Delivery to the following recipient failed permanently:\n\n  <addr>"
#   Zoho:  "Your message to <addr> wasn't delivered."
EXTRA_PATTERNS = [
    (r"failed\s+permanently\s*:\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
     re.IGNORECASE | re.DOTALL),
    (r"to\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\s+"
     r"(?:wasn'?t|was\s+not|is\s+not|has\s+not\s+been)\s+(?:delivered|accepted)",
     re.IGNORECASE | re.DOTALL),
    (r"(?:wasn'?t|was\s+not)\s+delivered\s+to\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
     re.IGNORECASE | re.DOTALL),
]

# Fallback sweep: the generic address regex, but only on lines that actually
# carry an SMTP failure code, so ordinary addresses quoted elsewhere in the
# bounce body are not mistaken for failed recipients.
GENERAL_EMAIL_RE = re.compile(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")
SMTP_FAILURE_CODES = ("550", "551", "552", "553", "421")

# -- DB target, filled in by _load_db_config() -------------------------------

_BASE_URL = ""      # {RAILWAY_API_URL}/api/gv-mailer
_CRON_SECRET = ""


# ===========================================================================
# Small helpers
# ===========================================================================

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _mask_email(email: str) -> str:
    """Mask an address for public logs: principal@school.edu.in -> pr****@school.edu.in"""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    masked_local = local[:2] + "****" if len(local) > 2 else "****"
    return f"{masked_local}@{domain}"


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Zoho-oauthtoken {token}", "Accept": "application/json"}


def _load_db_config() -> None:
    """
    Same Railway mailer API and cron secret github_mailer.py uses:
    RAILWAY_API_URL + CRON_SECRET, header X-Cron-Secret, base /api/gv-mailer.
    GV_CRON_SECRET (the *other*, admin-app secret) is accepted only as a
    fallback so a misconfigured workflow still has one path to work.
    """
    global _BASE_URL, _CRON_SECRET
    api_url = _env("RAILWAY_API_URL").rstrip("/")
    if api_url:
        _BASE_URL = f"{api_url}/api/gv-mailer"
    _CRON_SECRET = _env("CRON_SECRET") or _env("GV_CRON_SECRET")

    if not _BASE_URL:
        print("WARNING: RAILWAY_API_URL is not set - addresses will be found but NOT marked in the DB")
    elif not _CRON_SECRET:
        print("WARNING: neither CRON_SECRET nor GV_CRON_SECRET is set - addresses will NOT be marked in the DB")


# ===========================================================================
# Zoho Mail: auth, listing, content
# ===========================================================================

def _refresh_access_token(account: dict):
    """Exchange the stored refresh token for an access token. None on any failure."""
    names = (account["client_id"], account["client_secret"], account["refresh_token"])
    values = [_env(n) for n in names]
    missing = [n for n, v in zip(names, values) if not v]
    if missing:
        print(f"  ERROR: missing env var(s): {', '.join(missing)}")
        return None

    try:
        resp = requests.post(
            ZOHO_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": values[0],
                "client_secret": values[1],
                "refresh_token": values[2],
            },
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print(f"  ERROR: token request failed: {e}")
        return None

    if resp.status_code != 200:
        print(f"  ERROR: token endpoint HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        body = resp.json()
    except Exception:
        print("  ERROR: token response was not JSON")
        return None

    token = body.get("access_token")
    if not token:
        print(f"  ERROR: no access_token in response (error={body.get('error', 'unknown')})")
        return None
    return token


def _get_account_id(token: str):
    """accountId of the first account on the token. None on any failure."""
    try:
        resp = requests.get(
            f"{ZOHO_API_BASE}/api/accounts",
            headers=_auth_headers(token),
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print(f"  ERROR: accounts request failed: {e}")
        return None

    if resp.status_code != 200:
        print(f"  ERROR: /api/accounts HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        accounts = resp.json().get("data") or []
    except Exception:
        print("  ERROR: /api/accounts response was not JSON")
        return None

    if not accounts:
        print("  ERROR: /api/accounts returned no accounts")
        return None

    account_id = accounts[0].get("accountId") or accounts[0].get("account_id")
    if not account_id:
        print("  ERROR: first account has no accountId")
        return None
    return str(account_id)


def _search_messages(token: str, account_id: str, search_key: str):
    """
    Messages matching one searchKey, de-duplicated, best effort.

    A failing search key is logged and returns whatever was collected so far:
    the other keys still run, and a mailbox with no mailer-daemon hits is not an
    error worth aborting on.
    """
    results = []
    seen = set()
    start = 0

    for _ in range(MAX_SEARCH_PAGES):
        try:
            resp = requests.get(
                f"{ZOHO_API_BASE}/api/accounts/{account_id}/messages/search",
                headers=_auth_headers(token),
                params={"searchKey": search_key, "limit": SEARCH_LIMIT, "start": start},
                timeout=HTTP_TIMEOUT,
            )
        except Exception as e:
            print(f"    WARNING: search '{search_key}' failed: {e}")
            break

        if resp.status_code != 200:
            print(f"    WARNING: search '{search_key}' HTTP {resp.status_code}: {resp.text[:160]}")
            break

        try:
            data = resp.json().get("data") or []
        except Exception:
            print(f"    WARNING: search '{search_key}' returned non-JSON")
            break

        if not isinstance(data, list):
            print(f"    WARNING: search '{search_key}' data was not a list")
            break

        fresh = 0
        for message in data:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("messageId") or message.get("message_id") or "")
            if message_id and message_id not in seen:
                seen.add(message_id)
                results.append(message)
                fresh += 1

        # Stop on a short page, or when the server ignored `start` and repeated
        # the same page - either way there is nothing new to fetch.
        if len(data) < SEARCH_LIMIT or fresh == 0:
            break
        start += len(data)

    return results


def _dig_content(node, depth: int = 0) -> str:
    """Pull the first non-empty body string out of a Zoho JSON envelope."""
    if depth > 6:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("content", "originalMessage", "originalmessage", "body", "message"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for key in ("data", "messages", "message"):
            if key in node:
                text = _dig_content(node[key], depth + 1)
                if text:
                    return text
    if isinstance(node, list):
        for item in node:
            text = _dig_content(item, depth + 1)
            if text:
                return text
    return ""


def _response_text(resp) -> str:
    """Raw bounce body from a content response, whether JSON-wrapped or plain."""
    if "json" in (resp.headers.get("Content-Type") or "").lower():
        try:
            return _dig_content(resp.json()) or ""
        except Exception:
            return resp.text or ""
    return resp.text or ""


def _get_message_content(token: str, account_id: str, message_id: str, folder_id=None) -> str:
    """
    Bounce body for one message, trying the plausible Zoho shapes in turn:
    folder-scoped content, the documented content path, then raw MIME. The
    first non-empty body wins; an unavailable shape is not an error.
    """
    urls = []
    if folder_id:
        urls.append(f"{ZOHO_API_BASE}/api/accounts/{account_id}/folders/{folder_id}/messages/{message_id}/content")
    urls.append(f"{ZOHO_API_BASE}/api/accounts/{account_id}/messages/{message_id}/content")
    urls.append(f"{ZOHO_API_BASE}/api/accounts/{account_id}/messages/{message_id}/originalmessage")

    for url in urls:
        try:
            resp = requests.get(url, headers=_auth_headers(token), timeout=HTTP_TIMEOUT)
        except Exception as e:
            print(f"    WARNING: content fetch failed for {message_id}: {e}")
            continue
        if resp.status_code != 200:
            continue
        text = _response_text(resp)
        if text and text.strip():
            return text
    return ""


# ===========================================================================
# Address extraction
# ===========================================================================

def extract_failed_addresses(content: str):
    """Failed recipient addresses in one bounce body. Returns a set."""
    found = set()
    if not content:
        return found

    for pattern, flags in BOUNCE_PATTERNS + EXTRA_PATTERNS:
        try:
            for match in re.finditer(pattern, content, flags):
                found.add(match.group(1))
        except Exception as e:
            print(f"    WARNING: pattern failed: {e}")

    for line in content.splitlines():
        # X-Failed-Recipients may list several addresses comma-separated, and the
        # pattern above only takes the first, so sweep the whole line too.
        if any(code in line for code in SMTP_FAILURE_CODES) or \
                line.lower().lstrip().startswith("x-failed-recipients:"):
            for match in GENERAL_EMAIL_RE.finditer(line):
                found.add(match.group(1))

    cleaned = set()
    for address in found:
        address = address.strip().strip(".,;:<>()[]\"'").lower()
        if "@" not in address:
            continue
        local, domain = address.split("@", 1)
        if not local or not domain or domain in OUR_DOMAINS:
            continue
        if local in NON_RECIPIENT_LOCALS:
            continue
        cleaned.add(address)
    return cleaned


# ===========================================================================
# DB: mark one address bounced
# ===========================================================================

def _mark_bounced(email: str) -> str:
    """'marked', 'skipped' (not deployed / no config) or 'failed'. Never raises."""
    if not _BASE_URL or not _CRON_SECRET:
        return "skipped"
    try:
        resp = requests.post(
            f"{_BASE_URL}/cron/mark-bounced",
            headers={"X-Cron-Secret": _CRON_SECRET, "Content-Type": "application/json"},
            json={"email": email},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print(f"    WARNING: mark-bounced request failed for {_mask_email(email)}: {e}")
        return "failed"

    if resp.status_code == 404:
        print("    mark-bounced endpoint not yet deployed, skipping")
        return "skipped"
    if resp.status_code >= 400:
        print(f"    WARNING: mark-bounced HTTP {resp.status_code} for {_mask_email(email)}: {resp.text[:160]}")
        return "failed"
    return "marked"


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    _load_db_config()

    print("GanitVeer bounce parser - sweeping 3 Zoho mailboxes")
    print(f"DB target: {_BASE_URL or '(unset)'}/cron/mark-bounced")

    per_account = []
    errors = []
    all_addresses = set()
    auth_failures = 0

    for account in ACCOUNTS:
        label = account["label"]
        stats = {
            "label": label,
            "mailbox": account["mailbox"],
            "messages": 0,
            "bounce_messages": 0,
            "addresses": set(),
            "note": "",
        }
        print(f"\n=== {label} ({account['mailbox']}) ===")

        token = _refresh_access_token(account)
        if not token:
            auth_failures += 1
            stats["note"] = "authentication failed"
            errors.append(f"{label}: authentication failed")
            per_account.append(stats)
            continue

        account_id = _get_account_id(token)
        if not account_id:
            auth_failures += 1
            stats["note"] = "account lookup failed"
            errors.append(f"{label}: account lookup failed")
            per_account.append(stats)
            continue

        try:
            candidates = {}
            for key in SEARCH_KEYS:
                for message in _search_messages(token, account_id, key):
                    message_id = str(message.get("messageId") or message.get("message_id") or "")
                    if message_id and message_id not in candidates:
                        candidates[message_id] = message
            stats["messages"] = len(candidates)

            for message_id, message in candidates.items():
                try:
                    content = _get_message_content(
                        token, account_id, message_id, message.get("folderId")
                    )
                    addresses = extract_failed_addresses(content)
                    if addresses:
                        stats["bounce_messages"] += 1
                        stats["addresses"] |= addresses
                except Exception as e:
                    errors.append(f"{label}: message {message_id}: {e}")
            print(f"  {stats['messages']} message(s) searched, "
                  f"{stats['bounce_messages']} bounce(s), "
                  f"{len(stats['addresses'])} failed address(es)")
        except Exception as e:
            stats["note"] = "scan failed"
            errors.append(f"{label}: scan failed: {e}")
            print(f"  ERROR: scan failed: {e}")

        all_addresses |= stats["addresses"]
        per_account.append(stats)

    # -- report every unique failed address to the DB ------------------------
    marked = skipped = failed_marks = 0
    if all_addresses:
        print(f"\nMarking {len(all_addresses)} unique address(es) as bounced...")
        for address in sorted(all_addresses):
            result = _mark_bounced(address)
            if result == "marked":
                marked += 1
            elif result == "skipped":
                skipped += 1
            else:
                failed_marks += 1

    # -- summary -------------------------------------------------------------
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for stats in per_account:
        note = f"  [{stats['note']}]" if stats["note"] else ""
        print(f"{stats['label']:<9} {stats['mailbox']:<32} "
              f"messages={stats['messages']:<4} bounces={stats['bounce_messages']:<4} "
              f"addresses={len(stats['addresses'])}{note}")

    print(f"\nTotal unique failed addresses: {len(all_addresses)}")
    print(f"Marked bounced in DB: {marked} | skipped (endpoint not deployed): {skipped} | "
          f"failed to mark: {failed_marks}")

    if errors:
        print("\nErrors encountered:")
        for error in errors:
            print(f"  - {error}")
    else:
        print("\nNo errors encountered.")

    if auth_failures == len(ACCOUNTS):
        print("\nFATAL: all 3 Zoho accounts failed to authenticate")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # never die on an unexpected error mid-sweep
        print(f"FATAL: unhandled error: {e}")
        sys.exit(1)
