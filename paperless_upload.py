#!/usr/bin/env python3
"""
Upload invoice PDFs from a local Maildir to Paperless-NGX.

Searches for emails whose subject contains any of: invoice, rechnung, beleg,
receipt (case-insensitive, substring – so "Telefonrechnung" matches too).
Only emails dated 2020-01-01 or later with PDF attachments are considered.

Skips:
  - Emails with the Maildir P-flag (already forwarded, e.g. to paperless@…)
  - Emails outside the date window
  - Emails without matching keywords
  - Emails with no PDF attachments
  - Emails whose Message-ID is already in the tracking file

Configuration via .env:
  MAILDIR_PATH            – path to All Mail Maildir
  PAPERLESS_URL           – Paperless-NGX base URL, e.g. http://paperless:8000
  PAPERLESS_TOKEN         – API token (Paperless Settings → API)
  PAPERLESS_UPLOADED_IDS  – optional path to tracking file
                            (default: data/paperless_uploaded.txt)
"""

import argparse
import email
import email.header
import email.utils
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

load_dotenv()

MAILDIR_PATH = os.environ.get("MAILDIR_PATH", "")
PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "").rstrip("/")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
TRACKING_FILE = Path(os.environ.get("PAPERLESS_UPLOADED_IDS", "data/paperless_uploaded.txt"))

SINCE = datetime(2020, 1, 1, tzinfo=timezone.utc)
KEYWORDS = re.compile(r"invoice|rechnung|beleg|receipt", re.IGNORECASE)


# ── helpers ───────────────────────────────────────────────────────────────────

def _decode_header(value):
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                decoded.append(part.decode("latin-1", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def _has_forwarded_flag(filename):
    """Check for the Maildir 'P' (Passed/Forwarded) flag in the filename flags section."""
    m = re.search(r":2,([A-Za-z]*)", filename)
    return bool(m and "P" in m.group(1))


def _parse_date(msg):
    date_str = str(msg.get("Date", ""))
    if not date_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        tpl = email.utils.parsedate(date_str)
        if tpl:
            return datetime(*tpl[:6], tzinfo=timezone.utc)
        return None


def _extract_pdfs(msg):
    """Return [(filename, bytes)] for every PDF part in the message."""
    pdfs = []
    for part in msg.walk():
        ct = part.get_content_type().lower()
        fname = _decode_header(part.get_filename() or "")
        is_pdf = ct == "application/pdf" or fname.lower().endswith(".pdf")
        if not is_pdf:
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:
            continue
        if data:
            pdfs.append((fname or "attachment.pdf", data))
    return pdfs


def _build_multipart(fields, filename, file_data):
    """Build a multipart/form-data body. Returns (body_bytes, content_type_header)."""
    boundary = uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(f"--{boundary}\r\n".encode())
        out.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.append(value.encode() + b"\r\n")
    safe_name = filename.encode("ascii", errors="replace").decode()
    out.append(f"--{boundary}\r\n".encode())
    out.append(
        f'Content-Disposition: form-data; name="document"; filename="{safe_name}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode()
    )
    out.append(file_data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _upload(title, created, filename, data, dry_run):
    if dry_run:
        print(f"    [dry-run] {filename}")
        return True
    fields = {"title": title, "created": created}
    body, ct = _build_multipart(fields, filename, data)
    req = Request(f"{PAPERLESS_URL}/api/documents/post_document/", data=body, method="POST")
    req.add_header("Authorization", f"Token {PAPERLESS_TOKEN}")
    req.add_header("Content-Type", ct)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status in (200, 201, 202)
    except HTTPError as exc:
        print(f"    [ERROR] HTTP {exc.code}: {exc.reason}")
        return False
    except URLError as exc:
        print(f"    [ERROR] {exc.reason}")
        return False


# ── tracking ──────────────────────────────────────────────────────────────────

def load_tracking():
    if not TRACKING_FILE.exists():
        return set()
    return {line.strip() for line in TRACKING_FILE.read_text().splitlines() if line.strip()}


def append_tracking(message_id):
    TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TRACKING_FILE.open("a") as fh:
        fh.write(message_id + "\n")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Show matches without uploading")
    args = parser.parse_args()

    if not MAILDIR_PATH:
        sys.exit("MAILDIR_PATH is not set")
    if not args.dry_run:
        for var in ("PAPERLESS_URL", "PAPERLESS_TOKEN"):
            if not os.environ.get(var):
                sys.exit(f"{var} is not set (required for upload; use --dry-run to skip)")

    maildir = Path(MAILDIR_PATH)
    if not maildir.exists():
        sys.exit(f"Maildir not found: {maildir}")

    uploaded_ids = load_tracking()
    stats = dict(scanned=0, skip_forwarded=0, skip_date=0,
                 skip_keyword=0, skip_no_pdf=0, skip_duplicate=0,
                 uploaded=0, errors=0)

    for subdir in ("cur", "new"):
        folder = maildir / subdir
        if not folder.exists():
            continue
        for msg_file in sorted(folder.iterdir()):
            if not msg_file.is_file():
                continue
            stats["scanned"] += 1

            if _has_forwarded_flag(msg_file.name):
                stats["skip_forwarded"] += 1
                continue

            with msg_file.open("rb") as fh:
                msg = email.message_from_binary_file(fh)

            dt = _parse_date(msg)
            if not dt or dt < SINCE:
                stats["skip_date"] += 1
                continue

            subject = _decode_header(msg.get("Subject", ""))
            if not KEYWORDS.search(subject):
                stats["skip_keyword"] += 1
                continue

            pdfs = _extract_pdfs(msg)
            if not pdfs:
                stats["skip_no_pdf"] += 1
                continue

            message_id = _decode_header(msg.get("Message-ID", "")) or msg_file.name
            if message_id in uploaded_ids:
                stats["skip_duplicate"] += 1
                continue

            created = dt.strftime("%Y-%m-%d")
            print(f"{created}  {subject[:72]}")

            ok = True
            for fname, data in pdfs:
                if _upload(subject, created, fname, data, args.dry_run):
                    stats["uploaded"] += 1
                else:
                    stats["errors"] += 1
                    ok = False

            if ok and not args.dry_run:
                uploaded_ids.add(message_id)
                append_tracking(message_id)

    print()
    print(f"Scanned:         {stats['scanned']:>6}")
    print(f"Forwarded-skip:  {stats['skip_forwarded']:>6}")
    print(f"Date-skip:       {stats['skip_date']:>6}")
    print(f"Keyword-skip:    {stats['skip_keyword']:>6}")
    print(f"No-PDF-skip:     {stats['skip_no_pdf']:>6}")
    print(f"Duplicate-skip:  {stats['skip_duplicate']:>6}")
    print(f"Uploaded:        {stats['uploaded']:>6}")
    if stats["errors"]:
        print(f"Errors:          {stats['errors']:>6}")


if __name__ == "__main__":
    main()
