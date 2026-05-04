"""
Gmail IMAP client for gmail-cleanup.

All network operations live here: fetching email headers, building the contact
list from Sent Mail, and executing trash/label actions.

Gmail-specific notes:
- Headers are fetched from [Gmail]/All Mail so every email is captured once,
  regardless of which labels it currently has.
- Gmail UIDs are stable within a folder; UIDs fetched from All Mail can be
  reused in later execute runs.
- Adding a Gmail label via IMAP = copying the message to the corresponding
  IMAP folder (e.g. "Invoice"). Gmail deduplicates the storage.
- Trashing a message = copying it to [Gmail]/Trash. Gmail removes it from
  all other labels. Emails are recoverable for 30 days.
"""

import email
import email.header
import email.utils
import json
import time
from typing import Callable, Optional

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from config import (
    ALL_MAIL_FOLDER,
    FETCH_BATCH_SIZE,
    IMAP_HOST,
    IMAP_PORT,
    SENT_FOLDER,
    TRASH_FOLDER,
)


def _decode_header(value: Optional[str]) -> str:
    """Decode an RFC 2047-encoded email header into a plain string."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except Exception:
                decoded.append(part.decode("latin-1", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def _parse_from(from_header: str) -> tuple[str, str, str]:
    """Parse a From header into (display_name, email_addr, domain).

    Examples:
        '"Amazon.de" <noreply@amazon.de>' → ('Amazon.de', 'noreply@amazon.de', 'amazon.de')
        'alice@example.com'               → ('', 'alice@example.com', 'example.com')
    """
    name, addr = email.utils.parseaddr(from_header or "")
    addr = addr.lower().strip()
    name = _decode_header(name)
    domain = addr.split("@")[1] if "@" in addr else addr
    return name, addr, domain


def _chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# IMAP fetch data items. BODY.PEEK does not mark messages as read.
_HEADER_FETCH_ITEMS = [
    b"BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE REPLY-TO MESSAGE-ID X-ORIGINAL-SENDER X-ORIGINAL-FROM)]",
    b"X-GM-LABELS",
]

_SENT_FETCH_ITEMS = [
    b"BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE)]",
]


class GmailIMAP:
    """Context manager for Gmail IMAP connections.

    Usage::

        with GmailIMAP(user, app_password) as imap:
            emails = imap.fetch_headers(existing_uids)
    """

    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self.client: Optional[IMAPClient] = None

    def __enter__(self) -> "GmailIMAP":
        self.client = IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True)
        self.client.login(self.user, self.password)
        self._all_mail_folder = self._find_special_folder(b"\\All", ALL_MAIL_FOLDER)
        self._sent_folder = self._find_special_folder(b"\\Sent", SENT_FOLDER)
        self._trash_folder = self._find_special_folder(b"\\Trash", TRASH_FOLDER)
        return self

    def _find_special_folder(self, attribute: bytes, fallback: str) -> str:
        """Return the IMAP folder name for a Gmail special-use attribute.

        Gmail localises folder names (e.g. '[Gmail]/Alle Nachrichten' for DE
        accounts), but always advertises the RFC 6154 special-use attributes.
        Falls back to the config constant when no match is found.
        """
        for flags, delimiter, name in self.client.list_folders():
            if attribute in flags:
                return name
        return fallback

    def __exit__(self, *args):
        if self.client:
            try:
                self.client.logout()
            except Exception:
                pass

    # ── read operations ───────────────────────────────────────────────────────

    def fetch_headers(
        self,
        existing_uids: set,
        limit: Optional[int] = None,
        on_batch: Optional[Callable[[int], None]] = None,
    ) -> list[dict]:
        """Download email headers from [Gmail]/All Mail.

        Skips UIDs already present in existing_uids. The on_batch callback is
        called after each network batch with the running total fetched so far
        (useful for progress reporting).

        Returns a list of dicts suitable for DB.save_emails().
        """
        self.client.select_folder(self._all_mail_folder, readonly=True)
        all_uids = self.client.search(["ALL"])

        new_uids = [u for u in all_uids if u not in existing_uids]
        if limit:
            new_uids = new_uids[:limit]

        if not new_uids:
            return []

        emails = []
        for batch in _chunks(new_uids, FETCH_BATCH_SIZE):
            try:
                data = self.client.fetch(batch, _HEADER_FETCH_ITEMS)
            except IMAPClientError as e:
                print(f"  [WARN] Batch fetch error: {e}, skipping batch")
                continue

            for uid, msg_data in data.items():
                raw_headers = msg_data.get(
                    b"BODY[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE REPLY-TO MESSAGE-ID X-ORIGINAL-SENDER X-ORIGINAL-FROM)]",
                    b"",
                )
                labels_raw = msg_data.get(b"X-GM-LABELS", ())

                msg = email.message_from_bytes(raw_headers)

                # Google Groups rewrites From to the group address and puts the
                # real sender in X-Original-From (full) / X-Original-Sender (addr only).
                orig_from = _decode_header(msg.get("X-Original-From")) or \
                            _decode_header(msg.get("X-Original-Sender"))
                from_name, from_addr, from_domain = _parse_from(orig_from or msg.get("From", ""))

                # Decode Gmail labels from bytes to strings
                labels = [
                    lbl.decode("utf-8", errors="replace") if isinstance(lbl, bytes) else str(lbl)
                    for lbl in labels_raw
                ]

                emails.append({
                    "uid": uid,
                    "message_id": _decode_header(msg.get("Message-ID"))[:500],
                    "from_addr": from_addr,
                    "from_name": from_name,
                    "from_domain": from_domain,
                    "subject": _decode_header(msg.get("Subject", ""))[:500],
                    "date": msg.get("Date", ""),
                    "list_unsub": _decode_header(msg.get("List-Unsubscribe")),
                    "reply_to": _decode_header(msg.get("Reply-To")),
                    "current_labels": json.dumps(labels),
                    "fetched_at": None,  # caller fills this in before saving
                })

            if on_batch:
                on_batch(len(emails))

        return emails

    def build_contacts(self, own_addresses: set[str]) -> list[dict]:
        """Extract recipient addresses from [Gmail]/Sent Mail.

        Only processes messages where the From header matches one of
        own_addresses. This filters out emails that appear in Sent because
        of Gmail Groups or catch-all aliases — those are received, not sent.

        Returns a list of dicts suitable for DB.save_contacts().
        """
        own_addresses_lower = {a.lower() for a in own_addresses}

        self.client.select_folder(self._sent_folder, readonly=True)
        all_uids = self.client.search(["ALL"])

        if not all_uids:
            return []

        contacts: dict[str, dict] = {}

        for batch in _chunks(all_uids, FETCH_BATCH_SIZE):
            try:
                data = self.client.fetch(batch, _SENT_FETCH_ITEMS)
            except IMAPClientError as e:
                print(f"  [WARN] Batch fetch error: {e}, skipping batch")
                continue

            for uid, msg_data in data.items():
                raw = msg_data.get(b"BODY[HEADER.FIELDS (FROM TO CC DATE)]", b"")
                msg = email.message_from_bytes(raw)

                # Skip if this message wasn't actually sent by us
                _, from_addr, _ = _parse_from(msg.get("From", ""))
                if from_addr not in own_addresses_lower:
                    continue

                date = msg.get("Date", "")

                for header_name in ("To", "Cc"):
                    header_value = msg.get(header_name, "")
                    if not header_value:
                        continue
                    for name, addr in email.utils.getaddresses([header_value]):
                        addr = addr.lower().strip()
                        if not addr or addr in own_addresses_lower:
                            continue
                        name = _decode_header(name)
                        if addr in contacts:
                            contacts[addr]["sent_count"] += 1
                            contacts[addr]["last_seen"] = date
                        else:
                            contacts[addr] = {
                                "email": addr,
                                "display_name": name,
                                "sent_count": 1,
                                "first_seen": date,
                                "last_seen": date,
                            }

        return list(contacts.values())

    # ── write operations ──────────────────────────────────────────────────────

    def move_to_trash(self, uids: list[int], batch_size: int = 50):
        """Copy messages to [Gmail]/Trash.

        In Gmail's IMAP model, copying to Trash adds the \\Trash label, which
        effectively removes the message from all other views. Emails remain
        recoverable for 30 days.
        """
        if not uids:
            return
        self.client.select_folder(self._all_mail_folder)
        for batch in _chunks(uids, batch_size):
            self.client.copy(batch, self._trash_folder)
            time.sleep(0.1)  # avoid hitting Gmail rate limits

    def add_label(self, uids: list[int], label_name: str, batch_size: int = 50):
        """Apply a Gmail label to a list of messages by copying them to the label folder.

        If the folder (= label) does not exist yet, it is created first.
        """
        if not uids:
            return
        self._ensure_folder(label_name)
        self.client.select_folder(self._all_mail_folder)
        for batch in _chunks(uids, batch_size):
            self.client.copy(batch, label_name)
            time.sleep(0.1)

    def _ensure_folder(self, folder_name: str):
        """Create the IMAP folder if it does not already exist."""
        try:
            existing = [f[2] for f in self.client.list_folders()]
            if folder_name not in existing:
                self.client.create_folder(folder_name)
        except IMAPClientError:
            pass  # folder already exists or creation failed — COPY will error if truly broken

    # Public alias used by main.py
    ensure_folder = _ensure_folder
