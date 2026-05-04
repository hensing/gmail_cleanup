"""
SQLite database layer for gmail-cleanup.

All persistent state lives here: email headers, Gemini classifications,
sender clusters, the whitelist, and the contact address book.

The DB class is the single access point. Pass a DB instance to classifier.py,
review.py, and main.py — they all read/write through it.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class DB:
    """Thin wrapper around a SQLite connection.

    Opens (and creates) the database at db_path. All schema migrations are
    applied via IF NOT EXISTS, so the class is safe to instantiate on an
    existing database.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            -- Raw email metadata fetched from [Gmail]/All Mail
            CREATE TABLE IF NOT EXISTS emails (
                uid            INTEGER PRIMARY KEY,  -- Gmail IMAP UID (folder-specific to All Mail)
                message_id     TEXT,
                from_addr      TEXT,
                from_name      TEXT,
                from_domain    TEXT,   -- domain part of from_addr; used as primary cluster key
                subject        TEXT,
                date           TEXT,
                list_unsub     TEXT,   -- List-Unsubscribe header value; presence signals marketing
                reply_to       TEXT,
                current_labels TEXT,   -- JSON array of Gmail label names at fetch time
                fetched_at     TEXT
            );

            -- Gemini (or contact-based) classification per email.
            -- overridden=1 means the user manually changed the category in review.
            CREATE TABLE IF NOT EXISTS classifications (
                uid           INTEGER PRIMARY KEY REFERENCES emails(uid),
                category      TEXT,   -- TRASH | NEWSLETTER | INVOICE | ORDER | KEEP
                reason        TEXT,   -- human-readable justification from Gemini
                classified_at TEXT,
                overridden    INTEGER DEFAULT 0
            );

            -- Sender clusters built after each classify run.
            -- Emails from personal domains are clustered by full address;
            -- automated senders are clustered by domain.
            CREATE TABLE IF NOT EXISTS clusters (
                cluster_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                from_domain   TEXT,
                from_addr     TEXT,               -- NULL for domain-level clusters
                mail_count    INTEGER,
                dominant_cat  TEXT,               -- most common category in this cluster
                cat_counts    TEXT,               -- JSON: {"TRASH": 5, "INVOICE": 2, ...}
                reviewed      INTEGER DEFAULT 0,  -- set to 1 after user accepts in review
                override_cat  TEXT                -- user-imposed category override
            );

            -- Emails or clusters the user has explicitly marked safe.
            -- Whitelisted items are never moved to Trash regardless of classification.
            -- Either uid or cluster_id is set, not both.
            CREATE TABLE IF NOT EXISTS whitelist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uid         INTEGER REFERENCES emails(uid),
                cluster_id  INTEGER REFERENCES clusters(cluster_id),
                added_at    TEXT,
                note        TEXT
            );

            -- Address book built from [Gmail]/Sent Mail.
            -- Any address you have written to is a "contact" and auto-classified as KEEP.
            CREATE TABLE IF NOT EXISTS contacts (
                email        TEXT PRIMARY KEY,
                display_name TEXT,
                sent_count   INTEGER DEFAULT 0,
                first_seen   TEXT,
                last_seen    TEXT
            );

            -- Indexes for common query patterns
            CREATE INDEX IF NOT EXISTS idx_emails_from_domain ON emails(from_domain);
            CREATE INDEX IF NOT EXISTS idx_emails_from_addr   ON emails(from_addr);
            CREATE INDEX IF NOT EXISTS idx_cls_category       ON classifications(category);
        """)
        self.conn.commit()

        # Migrations for columns added after initial release
        for stmt in [
            "ALTER TABLE classifications ADD COLUMN executed_at TEXT",
        ]:
            try:
                self.conn.execute(stmt)
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    def close(self):
        self.conn.close()

    # ── emails ───────────────────────────────────────────────────────────────

    def get_existing_uids(self) -> set:
        """Return the set of UIDs already in the database (used to skip re-fetching)."""
        rows = self.conn.execute("SELECT uid FROM emails").fetchall()
        return {r["uid"] for r in rows}

    def save_emails(self, emails: list[dict]):
        """Bulk-insert email rows. Existing UIDs are silently skipped (INSERT OR IGNORE)."""
        self.conn.executemany(
            """INSERT OR IGNORE INTO emails
               (uid, message_id, from_addr, from_name, from_domain,
                subject, date, list_unsub, reply_to, current_labels, fetched_at)
               VALUES (:uid, :message_id, :from_addr, :from_name, :from_domain,
                       :subject, :date, :list_unsub, :reply_to, :current_labels, :fetched_at)""",
            emails,
        )
        self.conn.commit()

    def count_emails(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    def deduplicate_by_message_id(self) -> int:
        """Remove duplicate emails that share the same Message-ID header.

        Google Groups forwarding creates two IMAP messages for the same email:
        one with \\Sent label (forwarding action) and one with \\Inbox (received
        copy). Both share the same Message-ID but have different UIDs.

        Keeps the "best" copy per Message-ID: prefers non-Sent-only emails,
        then more labels. Returns the number of deleted rows.
        """
        dupes = self.conn.execute("""
            SELECT message_id FROM emails
            WHERE message_id != ''
            GROUP BY message_id HAVING COUNT(*) > 1
        """).fetchall()

        deleted = 0
        for (msg_id,) in dupes:
            rows = self.conn.execute(
                "SELECT uid, current_labels FROM emails WHERE message_id = ?", (msg_id,)
            ).fetchall()

            def _score(row):
                labels = json.loads(row["current_labels"] or "[]")
                sent_only = labels == ["\\Sent"]
                return (not sent_only, len(labels))

            ranked = sorted(rows, key=_score, reverse=True)
            to_delete = [r["uid"] for r in ranked[1:]]
            if to_delete:
                ph = ",".join("?" * len(to_delete))
                self.conn.execute(f"DELETE FROM classifications WHERE uid IN ({ph})", to_delete)
                self.conn.execute(f"DELETE FROM whitelist WHERE uid IN ({ph})", to_delete)
                self.conn.execute(f"DELETE FROM emails WHERE uid IN ({ph})", to_delete)
                deleted += len(to_delete)

        self.conn.commit()
        return deleted

    # ── classifications ──────────────────────────────────────────────────────

    def get_unclassified(self, limit: Optional[int] = None) -> list[dict]:
        """Return emails that have no classification yet, ordered by UID."""
        q = """
            SELECT e.uid, e.from_addr, e.from_name, e.from_domain,
                   e.subject, e.date, e.list_unsub, e.reply_to
            FROM emails e
            LEFT JOIN classifications c ON e.uid = c.uid
            WHERE c.uid IS NULL
            ORDER BY e.uid
        """
        if limit:
            q += f" LIMIT {limit}"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def save_classifications(self, classifications: list[dict]):
        """Upsert classification rows. existing rows are replaced."""
        self.conn.executemany(
            """INSERT OR REPLACE INTO classifications
               (uid, category, reason, classified_at, overridden)
               VALUES (:uid, :category, :reason, :classified_at, :overridden)""",
            [
                {
                    "uid": c["uid"],
                    "category": c["category"],
                    "reason": c.get("reason", ""),
                    "classified_at": _now(),
                    "overridden": c.get("overridden", 0),
                }
                for c in classifications
            ],
        )
        self.conn.commit()

    def override_email(self, uid: int, category: str):
        """Manually override an individual email's category (sets overridden=1)."""
        self.conn.execute(
            "UPDATE classifications SET category=?, overridden=1 WHERE uid=?",
            (category, uid),
        )
        self.conn.commit()

    def count_classified(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]

    # ── clusters ─────────────────────────────────────────────────────────────

    def build_clusters(self):
        """Rebuild the clusters table from the current classifications.

        Clustering strategy:
        - Personal email domains (gmail.com, yahoo.de, …): cluster by full from_addr
        - All other domains: cluster by from_domain

        This prevents thousands of individual personal contacts from collapsing
        into a single giant "gmail.com" cluster.
        """
        from config import PERSONAL_DOMAINS

        self.conn.execute("DELETE FROM clusters")

        # Count category occurrences per (domain, addr) pair
        rows = self.conn.execute("""
            SELECT e.from_domain, e.from_addr,
                   c.category, COUNT(*) as cnt
            FROM emails e
            JOIN classifications c ON e.uid = c.uid
            GROUP BY e.from_domain, e.from_addr, c.category
        """).fetchall()

        agg: dict[tuple, dict] = {}
        for row in rows:
            domain = row["from_domain"] or ""
            addr = row["from_addr"] or ""

            if domain.lower() in PERSONAL_DOMAINS:
                key = ("addr", addr)
                cluster_domain = domain
                cluster_addr = addr
            else:
                key = ("domain", domain)
                cluster_domain = domain
                cluster_addr = None

            if key not in agg:
                agg[key] = {
                    "from_domain": cluster_domain,
                    "from_addr": cluster_addr,
                    "cat_counts": {},
                    "total": 0,
                }
            agg[key]["cat_counts"][row["category"]] = (
                agg[key]["cat_counts"].get(row["category"], 0) + row["cnt"]
            )
            agg[key]["total"] += row["cnt"]

        for data in agg.values():
            dominant = max(data["cat_counts"], key=data["cat_counts"].get)
            self.conn.execute(
                """INSERT INTO clusters (from_domain, from_addr, mail_count, dominant_cat, cat_counts)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    data["from_domain"],
                    data["from_addr"],
                    data["total"],
                    dominant,
                    json.dumps(data["cat_counts"]),
                ),
            )

        self.conn.commit()

    def get_clusters(self, only_unreviewed: bool = False) -> list[dict]:
        """Return all clusters sorted by mail count descending."""
        q = "SELECT * FROM clusters"
        if only_unreviewed:
            q += " WHERE reviewed=0"
        q += " ORDER BY mail_count DESC"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def get_cluster_emails(self, cluster_id: int) -> list[dict]:
        """Return all classified emails belonging to a specific cluster."""
        cluster = self.conn.execute(
            "SELECT * FROM clusters WHERE cluster_id=?", (cluster_id,)
        ).fetchone()
        if not cluster:
            return []

        # Match by address for personal-domain clusters, by domain for the rest
        if cluster["from_addr"]:
            condition = "e.from_addr = ?"
            param = cluster["from_addr"]
        else:
            condition = "e.from_domain = ?"
            param = cluster["from_domain"]

        rows = self.conn.execute(
            f"""SELECT e.uid, e.from_addr, e.from_name, e.subject, e.date,
                       c.category, c.reason, c.overridden
                FROM emails e
                JOIN classifications c ON e.uid = c.uid
                WHERE {condition}
                ORDER BY e.date DESC""",
            (param,),
        ).fetchall()
        return [dict(r) for r in rows]

    def override_cluster(self, cluster_id: int, category: str):
        """Override the category for all emails in a cluster.

        Also sets override_cat on the cluster row so the review UI reflects it.
        Individual email rows are updated with overridden=1.
        """
        cluster = self.conn.execute(
            "SELECT * FROM clusters WHERE cluster_id=?", (cluster_id,)
        ).fetchone()
        if not cluster:
            return

        self.conn.execute(
            "UPDATE clusters SET override_cat=?, reviewed=1 WHERE cluster_id=?",
            (category, cluster_id),
        )

        if cluster["from_addr"]:
            condition = "e.from_addr = ?"
            param = cluster["from_addr"]
        else:
            condition = "e.from_domain = ?"
            param = cluster["from_domain"]

        self.conn.execute(
            f"""UPDATE classifications SET category=?, overridden=1
                WHERE uid IN (SELECT uid FROM emails e WHERE {condition})""",
            (category, param),
        )
        self.conn.commit()

    def mark_cluster_reviewed(self, cluster_id: int):
        """Mark a cluster as reviewed without changing its dominant category."""
        self.conn.execute(
            "UPDATE clusters SET reviewed=1 WHERE cluster_id=?", (cluster_id,)
        )
        self.conn.commit()

    # ── whitelist ─────────────────────────────────────────────────────────────

    def add_to_whitelist(
        self,
        uid: Optional[int] = None,
        cluster_id: Optional[int] = None,
        note: str = "",
    ):
        """Add an email or an entire cluster to the whitelist.

        Whitelisted items are forced to KEEP during execute, regardless of their
        classification. The classification table is also updated immediately so
        the review UI reflects the change.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO whitelist (uid, cluster_id, added_at, note) VALUES (?,?,?,?)",
            (uid, cluster_id, _now(), note),
        )
        if uid:
            self.conn.execute(
                "UPDATE classifications SET category='KEEP', overridden=1 WHERE uid=?",
                (uid,),
            )
        if cluster_id:
            self.override_cluster(cluster_id, "KEEP")
        self.conn.commit()

    def whitelist_sender(self, from_addr: str):
        """Permanently whitelist a sender address.

        Adds the address to contacts (auto-KEEP in future classify runs) and
        immediately overrides all existing classifications from that address to KEEP.
        """
        self.conn.execute(
            """INSERT OR IGNORE INTO contacts (email, display_name, sent_count, first_seen, last_seen)
               VALUES (?, '', 0, ?, ?)""",
            (from_addr, _now(), _now()),
        )
        self.conn.execute(
            """UPDATE classifications SET category='KEEP', overridden=1
               WHERE uid IN (SELECT uid FROM emails WHERE from_addr=?)""",
            (from_addr,),
        )
        self.conn.commit()

    def is_uid_whitelisted(self, uid: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM whitelist WHERE uid=?", (uid,)
        ).fetchone()
        return row is not None

    def get_whitelisted_uids(self) -> set:
        rows = self.conn.execute(
            "SELECT uid FROM whitelist WHERE uid IS NOT NULL"
        ).fetchall()
        return {r["uid"] for r in rows}

    # ── contacts ─────────────────────────────────────────────────────────────

    def save_contacts(self, contacts: list[dict]):
        """Upsert contact rows. sent_count is accumulated across multiple runs."""
        for c in contacts:
            existing = self.conn.execute(
                "SELECT sent_count FROM contacts WHERE email=?", (c["email"],)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE contacts SET sent_count=sent_count+?, last_seen=? WHERE email=?",
                    (c["sent_count"], c["last_seen"], c["email"]),
                )
            else:
                self.conn.execute(
                    """INSERT INTO contacts (email, display_name, sent_count, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        c["email"],
                        c.get("display_name", ""),
                        c["sent_count"],
                        c["first_seen"],
                        c["last_seen"],
                    ),
                )
        self.conn.commit()

    def is_contact(self, email_addr: str) -> bool:
        """Return True if this address appears in the contacts table."""
        row = self.conn.execute(
            "SELECT 1 FROM contacts WHERE email=?", (email_addr.lower(),)
        ).fetchone()
        return row is not None

    def get_contact_sent_count(self, email_addr: str) -> int:
        """Return how many times this address appeared as a recipient in Sent Mail."""
        row = self.conn.execute(
            "SELECT sent_count FROM contacts WHERE email=?", (email_addr.lower(),)
        ).fetchone()
        return row["sent_count"] if row else 0

    def count_contacts(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

    # ── execute planning ──────────────────────────────────────────────────────

    def get_planned_actions(self) -> list[dict]:
        """Return the effective action for every classified email.

        The effective category for each email is:
        1. KEEP — if the UID is in the whitelist
        2. The classification category (which may already reflect cluster/review overrides)

        Emails without a classification are excluded.
        """
        whitelisted = self.get_whitelisted_uids()

        rows = self.conn.execute("""
            SELECT e.uid, e.from_addr, e.subject, c.category
            FROM emails e
            JOIN classifications c ON e.uid = c.uid
        """).fetchall()

        actions = []
        for row in rows:
            uid = row["uid"]
            cat = row["category"]
            if uid in whitelisted:
                cat = "KEEP"
            actions.append({
                "uid": uid,
                "from_addr": row["from_addr"],
                "subject": row["subject"],
                "effective_category": cat,
            })
        return actions

    def mark_executed(self, uids: list[int]):
        """Mark UIDs as executed after a successful execute --no-dry-run run."""
        if not uids:
            return
        ph = ",".join("?" * len(uids))
        self.conn.execute(
            f"UPDATE classifications SET executed_at=? WHERE uid IN ({ph})",
            [_now(), *uids],
        )
        self.conn.commit()

    def drop_executed_emails(self) -> int:
        """Hard-delete all emails that have been executed from the local DB.

        Removes rows from emails, classifications, and whitelist. Does not
        touch the Maildir — mbsync handles cleanup on the next sync.
        Returns the number of emails deleted.
        """
        rows = self.conn.execute(
            "SELECT uid FROM classifications WHERE executed_at IS NOT NULL"
        ).fetchall()
        uids = [r["uid"] for r in rows]
        if not uids:
            return 0
        ph = ",".join("?" * len(uids))
        self.conn.execute(f"DELETE FROM whitelist WHERE uid IN ({ph})", uids)
        self.conn.execute(f"DELETE FROM classifications WHERE uid IN ({ph})", uids)
        self.conn.execute(f"DELETE FROM emails WHERE uid IN ({ph})", uids)
        self.conn.commit()
        return len(uids)

    def get_all_emails_for_regex(self) -> list[dict]:
        """Return uid and subject for all classified emails (used for global subject-regex matching)."""
        rows = self.conn.execute(
            "SELECT e.uid, e.subject FROM emails e JOIN classifications c ON e.uid = c.uid"
        ).fetchall()
        return [dict(r) for r in rows]

    def override_emails_batch(self, uids: list[int], category: str):
        """Override category for multiple emails in bulk (sets overridden=1 for each)."""
        if not uids:
            return
        ph = ",".join("?" * len(uids))
        self.conn.execute(
            f"UPDATE classifications SET category=?, overridden=1 WHERE uid IN ({ph})",
            [category, *uids],
        )
        self.conn.commit()

    def get_stats(self) -> dict:
        """Return aggregate statistics for the summary view."""
        cat_counts = {}
        rows = self.conn.execute(
            "SELECT category, COUNT(*) as cnt FROM classifications GROUP BY category"
        ).fetchall()
        for r in rows:
            cat_counts[r["category"]] = r["cnt"]

        return {
            "total_emails": self.count_emails(),
            "total_classified": self.count_classified(),
            "total_contacts": self.count_contacts(),
            "categories": cat_counts,
        }
