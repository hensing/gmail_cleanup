"""
Configuration constants, enums, and the Gemini classification prompt.

Edit GEMINI_MODEL if you want to use a different model version.
PERSONAL_DOMAINS controls whether emails are clustered by address (personal)
or by domain (automated senders).
"""

from enum import Enum

# Gemini model to use for classification.
# Flash is recommended: fast, cheap, and accurate enough for header-based classification.
GEMINI_MODEL = "gemini-2.5-flash"

# Gmail IMAP connection settings
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Gmail IMAP folder names (these are fixed by Google)
ALL_MAIL_FOLDER = "[Gmail]/All Mail"
SENT_FOLDER = "[Gmail]/Sent Mail"
TRASH_FOLDER = "[Gmail]/Trash"

# How many headers to request from the IMAP server per network round-trip
FETCH_BATCH_SIZE = 150

# How many emails to send to Gemini in a single API call.
# Larger batches save API calls but increase token usage per call.
CLASSIFY_BATCH_SIZE = 200

# Email domains that belong to real people (personal webmail providers).
# Emails from these domains are clustered by full address rather than by domain,
# so "alice@gmail.com" and "bob@gmail.com" appear as separate clusters.
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.de",
    "hotmail.com", "hotmail.de",
    "outlook.com", "outlook.de",
    "live.com", "live.de",
    "web.de", "gmx.de", "gmx.net", "gmx.at",
    "t-online.de", "freenet.de",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "pm.me",
    "posteo.de", "mailbox.org",
    "dickten.info",  # own domain — aliases cluster by full address, not domain
}


class Category(str, Enum):
    """Email classification categories.

    TRASH and NEWSLETTER are both routed to [Gmail]/Trash.
    INVOICE and ORDER receive Gmail labels for later reference.
    KEEP emails are left untouched.
    """
    TRASH = "TRASH"
    NEWSLETTER = "NEWSLETTER"
    INVOICE = "INVOICE"
    ORDER = "ORDER"
    KEEP = "KEEP"


# System prompt for Gemini. Injected before the batch of email headers.
# The response schema (defined in classifier.py) enforces valid JSON output.
CLASSIFY_SYSTEM_PROMPT = """You are an email classifier helping clean up a Gmail inbox before IMAP migration.

Classify each email into exactly one category based on the From address, Subject, Date, and headers provided.

CATEGORIES:
- TRASH: delivery tracking updates, shipping status updates, system notifications, account activity alerts (login, password change), promotional/marketing emails, social media notifications, app notifications, reminders, calendar invites from services, "your order has shipped" shipping-only updates
- NEWSLETTER: newsletters, digests, mailing lists, blog updates, content emails — even if subscribed or informative
- INVOICE: invoices, bills, payment receipts, subscription charges, any email with a monetary amount that serves as proof of payment or charge
- ORDER: order confirmations (primary confirmation of a purchase, no payment receipt content), booking confirmations
- KEEP: personal emails from real humans, important business correspondence, contracts, legal documents, account credentials (initial setup), anything valuable not covered above

RULES:
- List-Unsubscribe header present → strongly lean towards NEWSLETTER or TRASH
- "noreply", "no-reply", "donotreply" in From → likely TRASH unless content suggests INVOICE or ORDER
- If email contains payment amount / invoice number → INVOICE (takes priority over ORDER)
- Shipping update for already-confirmed order → TRASH
- When uncertain between TRASH and NEWSLETTER → NEWSLETTER
- When uncertain between INVOICE and ORDER → INVOICE
- sent_count_hint: if provided, higher value means you've corresponded with this person before → lean KEEP

Return ONLY a valid JSON array, no markdown, no explanation:
[{"uid": 123, "category": "TRASH", "reason": "Shipping status update"}, ...]"""
