"""Broker email ingester.

One-shot IMAP poller. Pulls new mail from a whitelist of broker/listing-platform
domains, extracts deal fields via heuristic regex, downloads OM attachments,
and appends rows to ingested_deals.csv for manual review before promotion to
the underwriter's deals.csv.

Usage:
    python broker_ingester.py

Schedule it (e.g. every 15 min) via cron:
    */15 * * * * cd /path/to/repo && /usr/bin/python broker_ingester.py >> ingest.log 2>&1

Required env vars (see .env.example):
    IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASSWORD
Optional:
    IMAP_FOLDER (default: INBOX), BROKER_DOMAINS (comma-separated override)
"""

import csv
import email
import imaplib
import json
import os
import re
import socket
import time
from email.header import decode_header
from email.message import Message
from pathlib import Path

from bs4 import BeautifulSoup

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


REPO_DIR = Path(__file__).parent
STATE_DIR = REPO_DIR / "state"
STATE_PATH = STATE_DIR / "ingest_state.json"
INBOX_DIR = REPO_DIR / "inbox"
OUTPUT_CSV = REPO_DIR / "ingested_deals.csv"
STATE_DIR.mkdir(exist_ok=True)
INBOX_DIR.mkdir(exist_ok=True)

# Seed list of broker / listing-platform sender domains. Extend via the
# BROKER_DOMAINS env var (comma-separated) or by editing this list directly.
DEFAULT_BROKER_DOMAINS = {
    # National multifamily brokerages
    "ipausa.com", "marcusmillichap.com", "cbre.com", "jll.com",
    "cushwake.com", "cushmanwakefield.com", "nmrk.com", "ngkf.com",
    "berkadia.com", "walkerdunlop.com", "northmarq.com", "colliers.com",
    "matthews.com", "eastdilsecured.com", "avisonyoung.com",
    # AZ specialist
    "abimf.com",
    # SoCal specialists / regionals
    "steppcommercial.com", "mogharebigroup.com", "kidder.com",
    "lee-associates.com",
    # Listing platforms
    "crexi.com", "loopnet.com", "ten-x.com", "realinsight.com", "brevitas.com",
}

CSV_FIELDS = [
    "Property Name", "# Units", "NOI", "Price", "Cap Rate",
    "City", "State", "County",
    "Broker", "Sender", "Subject", "Listing URL",
    "Attachments", "Received At", "IMAP UID", "Needs Review",
]


# --------------------------------------------------------------------------
# State (last-processed UID per folder)
# --------------------------------------------------------------------------

def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# IMAP fetch
# --------------------------------------------------------------------------

def _require_env(name):
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(
            f"❌ Required env var {name!r} is missing or empty. "
            f"In GitHub Actions, set it under Settings → Secrets and variables → Actions. "
            f"Locally, set it in your .env file (see .env.example)."
        )
    # Catch the common copy-paste mistake of pasting the whole "KEY=value" line
    # as the secret value (would produce a confusing DNS / auth error otherwise).
    if val.startswith(f"{name}="):
        raise SystemExit(
            f"❌ Env var {name!r} value starts with {name!r} — looks like you pasted the "
            f"whole 'KEY=VALUE' line into the secret. Paste only the value (e.g. 'imap.gmail.com')."
        )
    return val


def connect_imap():
    host = _require_env("IMAP_HOST")
    port = int(os.environ.get("IMAP_PORT", 993))
    user = _require_env("IMAP_USER")
    password = _require_env("IMAP_PASSWORD")
    try:
        conn = imaplib.IMAP4_SSL(host, port)
    except socket.gaierror as e:
        # If the value is masked in logs, these character-level stats give
        # us enough to diagnose without leaking the (non-secret) hostname.
        bad_chars = sorted({c for c in host if not (c.isalnum() or c in ".-")})
        raise SystemExit(
            f"❌ DNS resolution failed for IMAP_HOST={host!r} (port {port}): {e}.\n"
            f"   length={len(host)} bad_chars={bad_chars!r} "
            f"starts_with={host[:4]!r} ends_with={host[-4:]!r}\n"
            f"   The value should be a bare hostname like 'imap.gmail.com' — "
            f"no scheme (https://), no port, no quotes, no leading/trailing whitespace."
        )
    conn.login(user, password)
    return conn


def fetch_new_uids(conn, folder, last_uid):
    conn.select(f'"{folder}"', readonly=True)
    # UID FETCH last_uid+1:* — empty result if no new mail
    start = (last_uid + 1) if last_uid else 1
    typ, data = conn.uid("search", None, f"UID {start}:*")
    if typ != "OK" or not data or not data[0]:
        return []
    uids = [int(x) for x in data[0].split()]
    # IMAP returns the most recent message even when nothing >= start matches;
    # filter explicitly.
    return [u for u in uids if u >= start]


def fetch_message(conn, uid):
    typ, data = conn.uid("fetch", str(uid), "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        return None
    return email.message_from_bytes(data[0][1])


# --------------------------------------------------------------------------
# Message parsing
# --------------------------------------------------------------------------

def decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def sender_domain(msg: Message):
    raw = msg.get("From", "")
    m = re.search(r"[\w\.-]+@([\w\.-]+)", raw)
    return m.group(1).lower() if m else ""


def is_broker_email(msg: Message, allowed_domains):
    domain = sender_domain(msg)
    if not domain:
        return False
    # Match exact or subdomain (e.g., mail.cbre.com matches cbre.com)
    return any(domain == d or domain.endswith("." + d) for d in allowed_domains)


def extract_body_text(msg: Message):
    """Return best-effort plain-text body, stripping HTML when needed."""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                parts.append(_decode_part(part))
            elif ctype == "text/html":
                parts.append(_html_to_text(_decode_part(part)))
        return "\n".join(p for p in parts if p)
    payload = _decode_part(msg)
    if msg.get_content_type() == "text/html":
        return _html_to_text(payload)
    return payload


def _decode_part(part):
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _html_to_text(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n")


def save_attachments(msg: Message, dest_dir: Path):
    saved = []
    if not msg.is_multipart():
        return saved
    dest_dir.mkdir(parents=True, exist_ok=True)
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disp:
            continue
        filename = decode_str(part.get_filename() or "")
        if not filename:
            continue
        safe = re.sub(r"[^\w\.\-]+", "_", filename)[:120]
        out = dest_dir / safe
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        out.write_bytes(payload)
        saved.append(str(out.relative_to(REPO_DIR)))
    return saved


# --------------------------------------------------------------------------
# Heuristic deal-field extraction
# --------------------------------------------------------------------------

UNIT_RE = re.compile(r"(\d{2,4})\s*[-\s]?units?\b", re.IGNORECASE)
PRICE_RE = re.compile(
    r"(?:price|asking|offered\s*at)[^\n$]{0,40}\$\s*([\d,]+(?:\.\d+)?)\s*(M|MM|million|K|thousand)?",
    re.IGNORECASE,
)
NOI_RE = re.compile(
    r"NOI[^\n$]{0,40}\$\s*([\d,]+(?:\.\d+)?)\s*(M|MM|million|K|thousand)?",
    re.IGNORECASE,
)
CAP_RE = re.compile(r"cap(?:\s*rate)?[:\s]+(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z\.\- ]{2,30}),\s*(AZ|CA)\b")
URL_RE = re.compile(
    r"https?://(?:www\.)?(?:crexi\.com|loopnet\.com|ten-x\.com|realinsight\.com|brevitas\.com)/\S+",
    re.IGNORECASE,
)
SUBJECT_PREFIXES = re.compile(
    r"^(re:|fwd?:|new\s+(listing|offering)|for\s+sale|now\s+available|just\s+listed|"
    r"offering\s+memorandum|exclusive\s+listing|featured\s+offering)\s*[:\-–]?\s*",
    re.IGNORECASE,
)


def _normalize_money(value, suffix):
    n = float(value.replace(",", ""))
    if suffix:
        s = suffix.lower()
        if s in {"m", "mm", "million"}:
            n *= 1_000_000
        elif s in {"k", "thousand"}:
            n *= 1_000
    return n


def extract_deal_fields_regex(subject, body):
    haystack = f"{subject}\n{body}"

    fields = {
        "Property Name": SUBJECT_PREFIXES.sub("", subject).strip() or "",
        "# Units": "",
        "NOI": "",
        "Price": "",
        "Cap Rate": "",
        "City": "",
        "State": "",
        "County": "",
        "Listing URL": "",
    }

    if (m := UNIT_RE.search(haystack)):
        fields["# Units"] = m.group(1)
    if (m := PRICE_RE.search(haystack)):
        fields["Price"] = f"{_normalize_money(m.group(1), m.group(2)):.0f}"
    if (m := NOI_RE.search(haystack)):
        fields["NOI"] = f"{_normalize_money(m.group(1), m.group(2)):.0f}"
    if (m := CAP_RE.search(haystack)):
        fields["Cap Rate"] = m.group(1)
    if (m := CITY_STATE_RE.search(haystack)):
        fields["City"], fields["State"] = m.group(1).strip(), m.group(2)
    if (m := URL_RE.search(haystack)):
        fields["Listing URL"] = m.group(0)

    return fields


# --------------------------------------------------------------------------
# LLM-based extraction (Claude Haiku 4.5)
# --------------------------------------------------------------------------

LLM_MODEL = "claude-haiku-4-5"
LLM_MAX_BODY_CHARS = 12000  # truncate runaway threads; real OMs come as attachments

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "Property Name": {"type": ["string", "null"]},
        "# Units":       {"type": ["integer", "null"]},
        "NOI":           {"type": ["number", "null"]},
        "Price":         {"type": ["number", "null"]},
        "Cap Rate":      {"type": ["number", "null"]},
        "City":          {"type": ["string", "null"]},
        "State":         {"type": ["string", "null"]},
        "County":        {"type": ["string", "null"]},
        "Listing URL":   {"type": ["string", "null"]},
    },
    "required": [
        "Property Name", "# Units", "NOI", "Price", "Cap Rate",
        "City", "State", "County", "Listing URL",
    ],
    "additionalProperties": False,
}

EXTRACTION_SYSTEM_PROMPT = """\
You extract structured deal data from commercial real-estate broker emails focused
on US multifamily (apartment) investments. Given the Subject line and Body, return
a single JSON object matching the provided schema.

# Field guide

- "Property Name": apartment community name. Strip marketing prefixes like
  "New Listing:", "For Sale:", "Now Available:", "Just Listed:", "Featured
  Offering:", "Offering Memorandum:", "Exclusive Listing:". E.g. "The Vintage
  at Tempe Apartments".
- "# Units": integer, total unit count. Phrases: "184 units", "184-unit",
  "184 apartment homes", "Units: 184".
- "NOI": annual Net Operating Income in USD as a NUMBER (no $, no commas).
  Watch shortforms: "$1.2M NOI" -> 1200000. "T-12 NOI", "Trailing NOI",
  "Year 1 NOI", "Stabilized NOI" all count. DISTINGUISH from gross income
  or revenue.
- "Price": asking / list / guidance price in USD as a number. Phrases:
  "Asking $52,500,000", "Price: $52.5M", "Offered at $52MM". Convert M/MM
  /million -> x1,000,000; K/thousand -> x1,000.
- "Cap Rate": stated cap rate as a percentage NUMBER (not decimal). E.g.
  "5.5% cap" -> 5.5. "Going-in cap", "stabilized cap", "in-place cap" all
  count.
- "City": primary city. E.g. "Phoenix", "Tempe", "Long Beach".
- "State": uppercase two-letter US state code. E.g. "AZ", "CA".
- "County": county name WITHOUT the word "County". E.g. "Maricopa", "Los
  Angeles", "Riverside". Often only in address blocks or property
  highlights -- may be absent.
- "Listing URL": single most-direct listing detail URL. Prefer CREXi,
  LoopNet, Ten-X, or broker-hosted offering pages. SKIP unsubscribe links,
  email-tracking redirects, and broker home pages.

# Rules

1. Use null when a value is missing or ambiguous -- DO NOT guess.
2. JSON numbers are numeric (no $, commas, or suffixes).
3. If the email lists a PORTFOLIO of multiple properties, extract only the
   HEADLINE property -- do not aggregate units, NOIs, or prices across the
   portfolio.
4. If the email is a market report, research piece, or newsletter digest
   (not a specific deal), return null for every field.
5. Trust the body more than the subject. Subjects are marketing-heavy.

# Examples

## Example 1
Subject: NEW LISTING: The Vintage at Tempe | 184 Units | Tempe, AZ
Body: We are pleased to present The Vintage at Tempe Apartments, a 184-unit
garden-style community in Tempe, Arizona (Maricopa County). Asking price:
$52,500,000. Year 1 NOI: $2,415,000. Going-in cap rate: 4.60%. View the OM
at https://www.crexi.com/properties/123456/vintage-tempe.

Output:
{"Property Name": "The Vintage at Tempe Apartments", "# Units": 184, "NOI": 2415000, "Price": 52500000, "Cap Rate": 4.6, "City": "Tempe", "State": "AZ", "County": "Maricopa", "Listing URL": "https://www.crexi.com/properties/123456/vintage-tempe"}

## Example 2
Subject: For Sale - 76 Units in Long Beach
Body: 76-unit value-add opportunity in Long Beach (LA County). In-place
NOI of approximately $1.1M. Pricing guidance available upon execution of
CA. Contact broker for OM.

Output:
{"Property Name": "76 Units in Long Beach", "# Units": 76, "NOI": 1100000, "Price": null, "Cap Rate": null, "City": "Long Beach", "State": "CA", "County": "Los Angeles", "Listing URL": null}

## Example 3
Subject: Q1 2026 Phoenix Multifamily Market Report
Body: Phoenix MSA Q1 multifamily fundamentals showed continued moderation,
with average rent growth at 2.3% YoY and vacancy ticking to 8.4%...

Output:
{"Property Name": null, "# Units": null, "NOI": null, "Price": null, "Cap Rate": null, "City": null, "State": null, "County": null, "Listing URL": null}

## Example 4
Subject: Exclusive: 312-Unit Sunbelt Portfolio | 2 Assets in Phoenix MSA
Body: Marcus & Millichap is pleased to offer a 2-property, 312-unit
Phoenix-area portfolio. Headline asset: Desert Vista Apartments (Mesa,
AZ -- 184 units, $48M asking, $2.1M NOI, 4.4% cap). Sister asset: Cactus
Run (Glendale, AZ -- 128 units). Portfolio offered together or
individually.

Output:
{"Property Name": "Desert Vista Apartments", "# Units": 184, "NOI": 2100000, "Price": 48000000, "Cap Rate": 4.4, "City": "Mesa", "State": "AZ", "County": "Maricopa", "Listing URL": null}
"""

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    if not _ANTHROPIC_AVAILABLE or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _stringify_field(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def extract_deal_fields_llm(subject, body):
    """Returns dict matching the regex extractor's shape, or None if unavailable."""
    client = _get_anthropic_client()
    if client is None:
        return None
    try:
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": EXTRACTION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            output_config={
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}
            },
            messages=[{
                "role": "user",
                "content": f"Subject: {subject}\n\nBody:\n{body[:LLM_MAX_BODY_CHARS]}",
            }],
        )
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        data = json.loads(text)
        return {k: _stringify_field(v) for k, v in data.items()}
    except Exception as e:
        print(f"⚠️  LLM extraction failed ({e}); falling back to regex.")
        return None


def extract_deal_fields(subject, body):
    """Try LLM extraction first; fall back to regex if unavailable or it fails."""
    llm_fields = extract_deal_fields_llm(subject, body)
    if llm_fields is not None:
        return llm_fields
    return extract_deal_fields_regex(subject, body)


# --------------------------------------------------------------------------
# CSV writer
# --------------------------------------------------------------------------

def append_row(row):
    new_file = not OUTPUT_CSV.exists()
    with OUTPUT_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def ingest_inbox():
    folder = os.environ.get("IMAP_FOLDER", "INBOX")
    custom = os.environ.get("BROKER_DOMAINS", "")
    allowed = {d.strip().lower() for d in custom.split(",") if d.strip()} or DEFAULT_BROKER_DOMAINS

    state = load_state()
    last_uid = state.get(folder, 0)

    conn = connect_imap()
    try:
        uids = fetch_new_uids(conn, folder, last_uid)
        print(f"📬 {len(uids)} new message(s) since UID {last_uid} in {folder}")

        ingested = 0
        for uid in uids:
            msg = fetch_message(conn, uid)
            if msg is None:
                continue
            if not is_broker_email(msg, allowed):
                continue

            subject = decode_str(msg.get("Subject", ""))
            sender = decode_str(msg.get("From", ""))
            received = msg.get("Date", "")
            body = extract_body_text(msg)

            attach_dir = INBOX_DIR / f"uid_{uid}"
            attachments = save_attachments(msg, attach_dir)

            fields = extract_deal_fields(subject, body)
            needs_review = not (fields["# Units"] and (fields["Price"] or fields["NOI"]))

            row = {
                **fields,
                "Broker": sender_domain(msg),
                "Sender": sender,
                "Subject": subject,
                "Attachments": "; ".join(attachments),
                "Received At": received,
                "IMAP UID": uid,
                "Needs Review": "YES" if needs_review else "",
            }
            append_row(row)
            ingested += 1
            print(f"  ✓ UID {uid}: {fields['Property Name'][:60]}"
                  f" | units={fields['# Units'] or '?'} price={fields['Price'] or '?'}")

        if uids:
            state[folder] = max(uids)
            save_state(state)
        print(f"✅ Ingested {ingested} broker email(s); state saved.")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


if __name__ == "__main__":
    ingest_inbox()
