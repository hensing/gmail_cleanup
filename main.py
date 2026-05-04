"""
gmail-cleanup – LLM-powered Gmail inbox triage before IMAP migration.

Usage:
    python main.py fetch [--limit N] [--build-contacts]
    python main.py classify [--limit N] [--reclassify]
    python main.py review
    python main.py execute [--dry-run]

Author: Dr. Henning Dickten (@hensing)
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from config import Category
from db import DB
from imap_client import GmailIMAP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _require_env(*names: str) -> dict:
    """Return env vars or exit with a clear error message."""
    values = {}
    missing = []
    for name in names:
        val = os.environ.get(name)
        if not val:
            missing.append(name)
        else:
            values[name] = val
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in the values.")
        sys.exit(1)
    return values


def _get_db() -> DB:
    db_path = os.environ.get("DB_PATH", "data/emails.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    return DB(db_path)


def _get_log_path() -> str:
    return os.environ.get("LOG_PATH", "data/actions.log")


def _write_action_log(entries: list[dict]):
    """Append executed actions to the audit log."""
    log_path = _get_log_path()
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        ts = datetime.now(timezone.utc).isoformat()
        for e in entries:
            f.write(
                f"{ts}\t{e['uid']}\t{e['effective_category']}\t"
                f"{e.get('from_addr', '')}\t{e.get('subject', '')[:100]}\n"
            )


# ── subcommand: fetch ─────────────────────────────────────────────────────────


def cmd_fetch(args):
    """
    Fetch email headers and store them locally in SQLite.

    Two modes:
    - Maildir mode (default when MAILDIR_PATH is set or --maildir is passed):
      reads headers directly from an mbsync-managed Maildir. No IMAP connection
      needed. Requires mbsync configured with AltMap yes.
    - IMAP mode (fallback): connects to Gmail directly, downloads headers from
      [Gmail]/All Mail. Requires GMAIL_USER and GMAIL_APP_PASSWORD.

    Already-known UIDs are always skipped, so re-running is safe.
    --build-contacts requires IMAP mode (pass --maildir="" to force IMAP).
    """
    db = _get_db()
    existing = db.get_existing_uids()
    maildir = args.maildir if args.maildir is not None else os.environ.get("MAILDIR_PATH", "")

    if maildir:
        from maildir_reader import read_headers
        print(f"Reading headers from Maildir: {maildir}")
        print(f"  {len(existing)} emails already in DB, skipping those")
        emails = read_headers(maildir, existing_uids=existing)
        if args.build_contacts:
            print("  [WARN] --build-contacts requires IMAP mode, skipping.")
    else:
        env = _require_env("GMAIL_USER", "GMAIL_APP_PASSWORD")
        own_addresses = set(
            (os.environ.get("GMAIL_ALIASES", "") + "," + env["GMAIL_USER"]).split(",")
        )
        own_addresses = {a.strip() for a in own_addresses if a.strip()}

        print(f"Connecting to Gmail as {env['GMAIL_USER']}…")
        with GmailIMAP(env["GMAIL_USER"], env["GMAIL_APP_PASSWORD"]) as imap:

            if args.build_contacts:
                print("Building contact list from Sent Mail…")
                contacts = imap.build_contacts(own_addresses)
                db.save_contacts(contacts)
                print(f"  {len(contacts)} contacts saved (total: {db.count_contacts()})")

            print(f"Fetching headers from [Gmail]/All Mail…")
            print(f"  {len(existing)} emails already in DB, skipping those")

            def progress(n):
                print(f"  {n} fetched so far…", end="\r", flush=True)

            emails = imap.fetch_headers(
                existing_uids=existing,
                limit=args.limit,
                on_batch=progress,
            )

    # Coerce any non-SQLite-compatible types (e.g. email.header.Header objects)
    emails = [
        {k: (str(v) if v is not None and not isinstance(v, (str, int, float, bytes)) else v)
         for k, v in e.items()}
        for e in emails
    ]

    if emails:
        ts = datetime.now(timezone.utc).isoformat()
        for e in emails:
            e["fetched_at"] = ts
        db.save_emails(emails)

    print(f"\nDone. {len(emails)} new emails saved (total: {db.count_emails()}).")
    db.close()


# ── subcommand: dedupe ───────────────────────────────────────────────────────


def cmd_dedupe(args):
    """Remove duplicate emails that share the same Message-ID.

    No Gmail connection needed — operates entirely on the local database.
    Keeps the best copy per Message-ID (non-Sent preferred, more labels wins).
    """
    db = _get_db()
    total_before = db.count_emails()
    removed = db.deduplicate_by_message_id()
    total_after = db.count_emails()
    print(f"Deduplicated: {removed} emails removed ({total_before} → {total_after}).")
    db.close()


# ── subcommand: classify ──────────────────────────────────────────────────────


def cmd_classify(args):
    """
    Classify emails using Gemini 2.5 Flash.

    Reads unclassified emails from the local database (no IMAP connection needed).
    Emails from known contacts are classified as KEEP without an API call.
    The remaining emails are sent to Gemini in batches of 25.

    After classification, the clusters table is rebuilt automatically.
    """
    env = _require_env("GEMINI_API_KEY")
    db = _get_db()

    from classifier import classify_emails

    total = db.count_emails()
    classified = db.count_classified()
    print(f"Database: {total} emails, {classified} already classified")

    if args.reclassify:
        print("--reclassify: clearing non-overridden classifications…")

    n = classify_emails(
        db=db,
        api_key=env["GEMINI_API_KEY"],
        limit=args.limit,
        reclassify=args.reclassify,
        model=os.environ.get("GEMINI_MODEL"),
    )

    print(f"\nClassified {n} emails. Rebuilding clusters…")
    db.build_clusters()
    clusters = db.get_clusters()
    print(f"Done. {len(clusters)} clusters built.")

    db.close()


# ── subcommand: review ────────────────────────────────────────────────────────


def cmd_review(args):
    """
    Interactive review of clustered email classifications.

    Shows emails grouped by sender domain in a table. You can accept, whitelist,
    or override categories at the cluster level, then drill into individual emails
    for finer control. Whitelisted emails are never deleted.
    """
    db = _get_db()
    from review import run_review
    run_review(db)
    db.close()


# ── subcommand: drop ─────────────────────────────────────────────────────────


def cmd_drop(args):
    """
    Remove executed emails from the local database.

    Hard-deletes all emails that were processed by 'execute --no-dry-run'.
    The local Maildir is not touched — mbsync cleans it up on the next sync
    once those messages are no longer present on Gmail.
    """
    db = _get_db()
    n = db.drop_executed_emails()
    if n:
        db.build_clusters()
        print(f"Dropped {n} emails from local DB. Clusters rebuilt.")
    else:
        print("Nothing to drop. Run 'execute --no-dry-run' first.")
    db.close()


# ── subcommand: execute ───────────────────────────────────────────────────────


def cmd_execute(args):
    """
    Apply classification decisions to Gmail (trash / label emails).

    Dry-run mode is the default – pass --no-dry-run to actually modify Gmail.
    Whitelisted emails are always skipped. All executed actions are appended
    to the audit log at data/actions.log.
    """
    env = _require_env("GMAIL_USER", "GMAIL_APP_PASSWORD")
    db = _get_db()

    actions = db.get_planned_actions()
    if not actions:
        print("No classified emails found. Run 'classify' first.")
        db.close()
        return

    # Group by effective category
    by_cat: dict[str, list[dict]] = {}
    for a in actions:
        cat = a["effective_category"]
        by_cat.setdefault(cat, []).append(a)

    print("\n=== EXECUTE PLAN ===")
    for cat in [c.value for c in Category]:
        count = len(by_cat.get(cat, []))
        if count:
            action_label = {
                "TRASH": "→ [Gmail]/Trash",
                "NEWSLETTER": "→ label 'Newsletter'",
                "INVOICE": "→ label 'Invoice'",
                "ORDER": "→ label 'OrderConfirmation'",
                "KEEP": "(no action)",
            }[cat]
            print(f"  {cat:<12} {count:>6} emails  {action_label}")

    total_to_act = sum(
        len(v) for k, v in by_cat.items() if k not in ("KEEP",)
    )
    print(f"\n  Total with action: {total_to_act}")

    is_dry_run = not args.no_dry_run
    if is_dry_run:
        print("\n[DRY RUN] No changes will be made. Use --no-dry-run to execute.")
        db.close()
        return

    confirm = input("\nProceed? Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        db.close()
        return

    trash_uids = [a["uid"] for a in by_cat.get("TRASH", [])]
    newsletter_uids = [a["uid"] for a in by_cat.get("NEWSLETTER", [])]
    invoice_uids = [a["uid"] for a in by_cat.get("INVOICE", [])]
    order_uids = [a["uid"] for a in by_cat.get("ORDER", [])]

    executed: list[dict] = []

    print(f"\nConnecting to Gmail as {env['GMAIL_USER']}…")
    with GmailIMAP(env["GMAIL_USER"], env["GMAIL_APP_PASSWORD"]) as imap:

        if trash_uids:
            print(f"Moving {len(trash_uids)} emails to Trash…")
            imap.move_to_trash(trash_uids)
            for uid in trash_uids:
                a = next(x for x in actions if x["uid"] == uid)
                executed.append(a)

        if newsletter_uids:
            print(f"Labeling {len(newsletter_uids)} emails as 'Newsletter'…")
            imap.add_label(newsletter_uids, "Newsletter")
            for uid in newsletter_uids:
                a = next(x for x in actions if x["uid"] == uid)
                executed.append(a)

        if invoice_uids:
            print(f"Labeling {len(invoice_uids)} emails as 'Invoice'…")
            imap.add_label(invoice_uids, "Invoice")
            for uid in invoice_uids:
                a = next(x for x in actions if x["uid"] == uid)
                executed.append(a)

        if order_uids:
            print(f"Labeling {len(order_uids)} emails as 'OrderConfirmation'…")
            imap.add_label(order_uids, "OrderConfirmation")
            for uid in order_uids:
                a = next(x for x in actions if x["uid"] == uid)
                executed.append(a)

    _write_action_log(executed)
    db.mark_executed([a["uid"] for a in executed])
    print(f"\nDone. {len(executed)} actions executed and logged to {_get_log_path()}.")
    print("Run 'drop' to remove these emails from the local database.")
    db.close()


# ── CLI entry point ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="LLM-powered Gmail cleanup tool. Use before IMAP migration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflow (mbsync mode):
  0. mbsync -a                                 # Sync Gmail → local Maildir
  1. ./mail fetch                              # Read headers from Maildir (MAILDIR_PATH)
  2. ./mail classify                           # Classify with Gemini
  3. ./mail review                             # Interactive review + whitelist
  4. ./mail execute                            # Dry-run preview
  5. ./mail execute --no-dry-run              # Apply changes to Gmail
  6. ./mail drop                              # Remove executed emails from local DB
  7. mbsync -a                                 # Clean up local Maildir copies

Workflow (IMAP mode, no mbsync):
  1. ./mail fetch --maildir=""               # Fetch headers via IMAP
  2–6. same as above
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch
    p_fetch = sub.add_parser("fetch", help="Import email headers (Maildir or IMAP)")
    p_fetch.add_argument("--limit", type=int, default=None, help="Max new emails to fetch (IMAP mode only)")
    p_fetch.add_argument(
        "--maildir", default=None,
        help="Maildir path to read from (overrides MAILDIR_PATH env var; pass empty string to force IMAP)",
    )
    p_fetch.add_argument(
        "--build-contacts",
        action="store_true",
        help="Also scan Sent Mail to build address book (IMAP mode only)",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    # classify
    p_cls = sub.add_parser("classify", help="Classify emails with Gemini")
    p_cls.add_argument("--limit", type=int, default=None, help="Max emails to classify")
    p_cls.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-classify already-classified emails (skips manually overridden ones)",
    )
    p_cls.set_defaults(func=cmd_classify)

    # dedupe
    p_dd = sub.add_parser("dedupe", help="Remove duplicate emails with the same Message-ID")
    p_dd.set_defaults(func=cmd_dedupe)

    # review
    p_rev = sub.add_parser("review", help="Interactive cluster review + whitelisting")
    p_rev.set_defaults(func=cmd_review)

    # drop
    p_drop = sub.add_parser("drop", help="Remove executed emails from local DB")
    p_drop.set_defaults(func=cmd_drop)

    # execute
    p_exec = sub.add_parser("execute", help="Apply classifications to Gmail")
    p_exec.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually execute changes (default is dry-run preview)",
    )
    p_exec.set_defaults(func=cmd_execute)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
