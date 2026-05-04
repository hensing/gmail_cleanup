"""
Maildir reader for gmail-cleanup.

Reads email headers from an mbsync-managed Maildir folder and returns the same
dict structure as imap_client.GmailIMAP.fetch_headers(), so the rest of the
pipeline (classify, review, execute) is unaffected.

Requires mbsync configured with AltMap yes, which embeds the IMAP UID as a
numeric prefix in every filename: {uid}_{timestamp}.{pid}.{host}:2,{flags}
"""

import email
import json
import os
from typing import Optional

from imap_client import _decode_header, _parse_from


def _uid_from_filename(name: str) -> Optional[int]:
    """Extract the IMAP UID from an mbsync AltMap filename.

    AltMap filenames start with '{uid}_'. Returns None for any file that does
    not follow this convention (e.g. mbsync state files).
    """
    prefix = name.split("_")[0]
    try:
        return int(prefix)
    except ValueError:
        return None


def _read_headers_only(path: str) -> bytes:
    """Read bytes up to and including the first blank line (header/body separator).

    This avoids loading full message bodies into memory — headers are all we need.
    """
    buf = bytearray()
    with open(path, "rb") as f:
        for line in f:
            buf += line
            if line in (b"\n", b"\r\n"):
                break
    return bytes(buf)


def read_headers(maildir_path: str, existing_uids: set[int]) -> list[dict]:
    """Scan a Maildir folder and return headers for all new (unseen) UIDs.

    Checks both cur/ and new/ subdirectories. Skips UIDs already in
    existing_uids. Returns a list of dicts compatible with DB.save_emails().
    The fetched_at field is left as None — the caller (main.py) sets it.
    """
    results = []
    skipped = 0
    errors = 0

    for subdir in ("cur", "new"):
        folder = os.path.join(maildir_path, subdir)
        if not os.path.isdir(folder):
            continue

        for name in os.listdir(folder):
            uid = _uid_from_filename(name)
            if uid is None:
                continue
            if uid in existing_uids:
                skipped += 1
                continue

            path = os.path.join(folder, name)
            try:
                raw = _read_headers_only(path)
                msg = email.message_from_bytes(raw)
            except Exception as e:
                errors += 1
                print(f"  [WARN] Could not parse {name}: {e}")
                continue

            orig_from = _decode_header(msg.get("X-Original-From")) or \
                        _decode_header(msg.get("X-Original-Sender"))
            from_name, from_addr, from_domain = _parse_from(
                orig_from or msg.get("From", "")
            )

            results.append({
                "uid": uid,
                "message_id": _decode_header(msg.get("Message-ID", ""))[:500],
                "from_addr": from_addr,
                "from_name": from_name,
                "from_domain": from_domain,
                "subject": _decode_header(msg.get("Subject", ""))[:500],
                "date": msg.get("Date", ""),
                "list_unsub": _decode_header(msg.get("List-Unsubscribe", "")),
                "reply_to": _decode_header(msg.get("Reply-To", "")),
                "current_labels": "[]",
                "fetched_at": None,
            })

    if errors:
        print(f"  [WARN] {errors} files could not be parsed.")

    return results
