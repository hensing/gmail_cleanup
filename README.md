# gmail-cleanup

**LLM-powered Gmail inbox triage before IMAP migration.**

A command-line tool that uses [Gemini 2.5 Flash](https://ai.google.dev/) to intelligently classify your Gmail inbox — keeping invoices and order confirmations, trashing notifications and delivery updates, and letting you review everything interactively before a single email is touched.

> Simple header-based rules don't work because `noreply@amazon.de` can mean both "your order shipped" (trash) and "your invoice is ready" (keep). The LLM understands the difference.

**Author:** Dr. Henning Dickten ([@hensing](https://github.com/hensing))

---

## Features

- **mbsync-first** – uses mbsync for incremental Gmail sync to a local Maildir; `fetch` reads headers from disk, no repeated IMAP connections
- **Full backup** – local Maildir is a complete offline copy of your inbox; mbsync cleans up after execute
- **Contact-aware** – builds an address book from your Sent folder; emails from known contacts are automatically preserved
- **Cluster review** – groups emails by sender domain for fast batch decisions (review 200 clusters instead of 10,000 individual emails)
- **Filter & regex** – filter the review by category or subject regex; bulk-assign a category to all emails matching a pattern
- **Whitelist** – mark individual emails or entire sender clusters as permanently safe; they are never deleted
- **Dry-run by default** – `execute` shows a preview without touching Gmail unless you explicitly confirm
- **Audit log** – every executed action is written to `data/actions.log`
- **Docker** – fully containerised; `./mail <subcommand>` wraps `docker compose run`

---

## Email Categories

| Category | Action | Examples |
|---|---|---|
| `TRASH` | → `[Gmail]/Trash` | delivery tracking, account alerts, promotional emails, app notifications |
| `NEWSLETTER` | → `[Gmail]/Trash` | newsletters, digests, mailing lists |
| `INVOICE` | → label `Invoice` | invoices, bills, payment receipts |
| `ORDER` | → label `OrderConfirmation` | order confirmations, booking confirmations |
| `KEEP` | no action | personal emails, known contacts, important documents |

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | Required to run the tool |
| mbsync (isync) | For syncing Gmail to local Maildir. `brew install isync` / `apt install isync` |
| Gmail App Password | Requires 2FA enabled. [Generate one here](https://myaccount.google.com/apppasswords) |
| Google AI API key | Free tier at [Google AI Studio](https://aistudio.google.com/apikey) |

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/hensing/gmail-cleanup.git
cd gmail-cleanup
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```env
GMAIL_USER=your.email@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
GMAIL_ALIASES=your.email@gmail.com,alias@yourdomain.com
GEMINI_API_KEY=AIza...

# Path to the mbsync All-Mail folder inside the container
MAILDIR_PATH=/data/maildir/All Mail
```

### 3. Configure mbsync

```bash
cp .mbsyncrc.example ~/.mbsyncrc
```

Edit `~/.mbsyncrc` — fill in your Gmail address and App Password path. The key settings are:

```ini
AltMap yes    # embeds IMAP UIDs in local filenames — required
Sync Pull     # one-directional: remote → local only
Expunge Near  # remove local copies when they disappear from Gmail (after Trash)
```

### 4. Build the Docker image

```bash
docker compose build
chmod +x mail
```

---

## Workflow

### Step 0 – Initial sync with mbsync

Downloads all emails from `[Gmail]/All Mail` to `./data/maildir/`. This is your backup — a complete offline copy.

```bash
mbsync -a
```

Subsequent runs are incremental (only new emails are downloaded). After `execute`, re-running mbsync removes local copies of trashed emails.

### Step 1 – Fetch email headers

Reads headers from the local Maildir into the SQLite database. No IMAP connection needed.

```bash
./mail fetch

# Limit to first 500 entries (useful for a test run)
./mail fetch --limit 500

# Force IMAP mode (no Maildir — legacy fallback)
./mail fetch --maildir="" --build-contacts
```

> **Tip:** Run `--build-contacts` at least once (in IMAP mode) before classifying. Known contacts are auto-classified as KEEP without any API call.

### Step 2 – Classify with Gemini

Reads unclassified emails from the local database and sends them to Gemini in batches. No IMAP connection needed.

```bash
./mail classify

# Re-classify everything (keeps manual overrides)
./mail classify --reclassify
```

After classification, sender clusters are built automatically.

### Step 3 – Interactive review

Review and adjust classifications before anything is deleted.

```bash
./mail review
```

**Cluster view:**

```
=== CLUSTER VIEW  (page 1/5, 100 clusters)  filter=TRASH
 Nr │ Domain / Sender              │ Mails │ Category       │ Distribution                  │ Contact
──────────────────────────────────────────────────────────────────────────────────────────────────────
   1 │ amazon.de                   │   342 │ ORDER(95%)     │ ORDER:325 TRASH:17            │
   2 │ newsletter.paypal.com        │    89 │ TRASH(100%)    │ TRASH:89                      │
   3 │ noreply@db.de               │    12 │ INVOICE(83%)   │ INVOICE:10 KEEP:2             │
   4 │ anna.mueller@gmail.com       │     8 │ KEEP(100%)     │ KEEP:8                        │ ✓ (12x)
```

**Cluster view commands:**

| Command | Description |
|---|---|
| `a <nr>` | Accept cluster (mark as reviewed) |
| `w <nr>` | Whitelist cluster (all emails → KEEP, never deleted) |
| `c <nr> CAT` | Override cluster category |
| `d <nr>` | Drill down into individual emails |
| `f [CAT]` | Filter cluster list by category (`f TRASH`); `f` alone clears |
| `rx CAT <pattern>` | Apply category to all emails whose subject matches regex (global) |
| `n` / `p` | Next / previous page |
| `s` | Show summary statistics |
| `q` | Quit |

**Drill-down view commands:**

| Command | Description |
|---|---|
| `w <uid>` | Whitelist this email |
| `wa` | Whitelist all emails in this cluster |
| `ws` | Permanently whitelist sender address (add to contacts) |
| `c <uid> CAT` | Override this email's category |
| `f [CAT\|regex]` | Filter by category (`f TRASH`) or subject regex (`f Your order.*`); `f` clears |
| `rx CAT <pattern>` | Apply category to emails in this cluster whose subject matches regex |
| `n` / `p` | Next / previous page |
| `b` | Back to cluster view |

**Regex examples:**

```
# In cluster view — applies globally to all emails
rx KEEP Your order .*
rx TRASH Your shipment .*

# In drill-down — applies only within the current cluster
rx KEEP New message from .*
```

### Step 4 – Execute

Apply classification decisions to Gmail.

```bash
# Preview only (default — shows what would happen, no changes made)
./mail execute

# Actually execute (moves emails to Trash / adds labels)
./mail execute --no-dry-run
```

You will be asked to type `yes` before any changes are made. Trashed emails remain recoverable in `[Gmail]/Trash` for 30 days.

### Step 5 – Drop

Remove executed emails from the local database.

```bash
./mail drop
```

This is a hard delete from the local DB only — the Maildir is not touched. Run `mbsync -a` afterwards to let mbsync clean up the local Maildir copies (they are no longer present on Gmail after being trashed).

### Step 6 – Next round

```bash
mbsync -a          # sync new emails and clean up trashed local copies
./mail fetch
./mail classify
./mail review
./mail execute --no-dry-run
./mail drop
```

---

## Other subcommands

```bash
# Remove duplicate emails (same Message-ID) from the local DB
./mail dedupe
```

---

## Important: Gmail IMAP label logic

Gmail uses labels internally; IMAP presents them as folders.

| Gmail | IMAP folder |
|---|---|
| All Mail | `[Gmail]/All Mail` |
| Trash | `[Gmail]/Trash` |
| Sent | `[Gmail]/Sent Mail` |
| User labels | Top-level IMAP folders (e.g. `Invoice`) |

The tool always works from `[Gmail]/All Mail` as the source. Adding a Gmail label means copying the message to the corresponding IMAP folder.

**Trashed emails are moved to `[Gmail]/Trash` (recoverable for 30 days), not permanently deleted.**

---

## Data stored locally

| Path | Contents |
|---|---|
| `data/emails.db` | SQLite: headers, classifications, clusters, whitelist, contacts |
| `data/actions.log` | Tab-separated audit log of every executed action |
| `data/maildir/` | Full email bodies synced by mbsync (Maildir format) |

All paths under `data/` are excluded from git via `.gitignore`.

---

## Cost estimate

With Gemini 2.5 Flash and header-only analysis (no email body):

- ~200 emails per API call
- Roughly 500–1000 tokens per call
- 10,000 emails ≈ 50 API calls ≈ $0.01–$0.05 total

Emails from known contacts skip the API entirely, reducing costs further.

---

## Security notes

- Gmail App Password is used for IMAP connections only; never logged or stored in the database
- The `.env` file is mounted read-only into the container and excluded from git
- Gemini only receives email headers (From, Subject, Date, List-Unsubscribe, Reply-To) — not email bodies
- All IMAP operations use TLS (port 993)
- The local Maildir contains full email bodies — secure accordingly (encrypt disk / restrict permissions)

---

## License

MIT
